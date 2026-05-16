#!/usr/bin/env -S python3 -B
"""Library status grid — a GitHub-contribution-style overview of where the
re-encoding effort stands across the whole media volume.

Each show gets one horizontal strip:
  - seasons within a show are visually separated by a single-space gap
  - each season is drawn in its own palette hue
  - each episode is a 2-column cell, dimmed by how non-conformant the file is
    (0 violations = bright, 3 = darkest)
Movies render as a final strip, one cell per movie.

Caches ffprobe results in `.library-probe-cache.json` next to the script,
keyed by (size, mtime_ns, blake2b(first_64KB)) — first run is slow, repeats
are near-instant. Use --no-cache to force a full re-probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from encode import (
    PALETTE, VIDEO_EXTS, RESET,
    fg, bg, dim,
    ffprobe, fmt_bytes, _natural_key,
    HISTORY_PATH,
)

CACHE_PATH = Path(__file__).resolve().parent / ".library-probe-cache.json"

# Score → how much to blend the hue toward DARK_FLOOR. 0.0 = vivid palette
# color, 1.0 = just the floor. Multiplicative dimming crushed colors to
# black-ish at the high-score end; lerping toward a near-black-but-not-zero
# floor keeps every cell visibly hued.
DARK_FLOOR = (30, 30, 35)
SCORE_BLEND = {0: 0.0, 1: 0.35, 2: 0.60, 3: 0.80}

# Probe workers — ffprobe is mostly subprocess-spawn + small I/O, so threads
# parallelise well. Keep modest to avoid hammering an external volume.
PROBE_WORKERS = 6

HEAD_BYTES = 64 * 1024  # bytes hashed for the change-detection fingerprint

BOLD = "\033[1m"

# Per-season row prefix layout: 4-space indent + "S01" + 2 spaces + "  3/12  "
# = 4 + 3 + 2 + 7 + 2 = 18 columns before the first cell.
SEASON_PREFIX_COLS = 18


@dataclass
class Entry:
    path: Path
    show: str           # show title (or "Movies")
    season: int         # 1-based; 0 for movies (single bucket)
    kind: str           # "series" or "movie"
    score: int          # 0..3, count of compliance rules violated
    size: int           # bytes on disk


def non_conformance_score(streams: list[dict], main: dict) -> int:
    """Count of `is_compliant` rules a file breaks (0..3). Mirrors the rules
    in encode.py:229 — keep in sync if the encoding policy changes."""
    score = 0
    if (main.get("codec_name") or "").lower() != "hevc":
        score += 1
    if (main.get("height") or 0) > 720:
        score += 1
    ukr = eng = other = 0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        if lang == "ukr":
            ukr += 1
        elif lang == "eng":
            eng += 1
        else:
            other += 1
    if other > 0 or ukr > 1 or eng > 1:
        score += 1
    return score


def head_hash(path: Path) -> str:
    """First-64KB blake2b digest, truncated to 16 hex chars. Cheap fingerprint
    that catches in-place edits even when mtime is preserved (e.g. rsync -t)."""
    with path.open("rb") as f:
        data = f.read(HEAD_BYTES)
    return hashlib.blake2b(data).hexdigest()[:16]


# Season folder names we recognise: "Season 01", "Season 1", "S01", "Series 4".
import re
_SEASON_RE = re.compile(r"^(?:season|series|s)\s*0*(\d+)$", re.IGNORECASE)


def classify(path: Path) -> Optional[tuple[str, int, str]]:
    """Return (show, season, kind) for a video file under a media library, or
    None if it's outside the series/movies trees we visualise.

    Rules:
      - .../series/<Show>/<Season NN>/<file>  → (Show, NN, "series")
      - .../series/<Show>/<file>              → (Show,  1, "series")
      - .../movies/<anything>/.../<file>      → ("Movies", 0, "movie"), with the
        movie's "group" being the top-level folder name under movies/
    """
    parts = path.resolve().parts
    for i, p in enumerate(parts):
        if p == "series" and i + 1 < len(parts) - 1:
            show = parts[i + 1]
            # The parent of the file may or may not be a Season folder.
            parent = path.parent.name
            m = _SEASON_RE.match(parent)
            season = int(m.group(1)) if m else 1
            return show, season, "series"
        if p == "movies" and i + 1 < len(parts) - 1:
            return "Movies", 0, "movie"
    return None


def movie_group(path: Path) -> str:
    """Top-level folder name under .../movies/ — used to assign palette hues
    so the Movies row isn't monochromatic."""
    parts = path.resolve().parts
    for i, p in enumerate(parts):
        if p == "movies" and i + 1 < len(parts):
            return parts[i + 1]
    return path.parent.name


# ---- cache -----------------------------------------------------------------

def load_cache() -> dict:
    try:
        with CACHE_PATH.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cache, f, indent=0, separators=(",", ":"))
    tmp.replace(CACHE_PATH)


def probe_one(path: Path) -> Optional[dict]:
    """ffprobe + score one file. Returns {score, codec, height, head, size,
    mtime_ns} or None if the file can't be probed."""
    try:
        info = ffprobe(path)
    except Exception:
        return None
    streams = info.get("streams", [])
    main = next(
        (s for s in streams
         if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if main is None:
        return None
    st = path.stat()
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "head": head_hash(path),
        "score": non_conformance_score(streams, main),
        "codec": (main.get("codec_name") or "").lower(),
        "height": int(main.get("height") or 0),
    }


def gather_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        n = p.name
        if n.endswith(".hevc.mkv") or n.endswith(".hevc.tmp.mkv") or n.endswith(".hevc.mkv.tmp"):
            continue
        out.append(p)
    return out


def scan(root: Path, use_cache: bool = True) -> list[Entry]:
    cache: dict = load_cache() if use_cache else {}
    files = gather_files(root)
    entries: list[Entry] = []

    # Decide which files need probing. A cache hit requires every component of
    # the fingerprint to match — stat tuple AND head hash. We compute the head
    # hash up front for cache candidates so re-runs validate quickly.
    misses: list[Path] = []
    for p in files:
        key = str(p.resolve())
        cached = cache.get(key)
        if not cached:
            misses.append(p)
            continue
        try:
            st = p.stat()
        except OSError:
            misses.append(p)
            continue
        if cached.get("size") != st.st_size or cached.get("mtime_ns") != st.st_mtime_ns:
            misses.append(p)
            continue
        try:
            if cached.get("head") != head_hash(p):
                misses.append(p)
                continue
        except OSError:
            misses.append(p)
            continue
        # cache hit — use cached values

    n = len(misses)
    if n:
        print(f"probing {n} file(s)…", file=sys.stderr, flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            futs = {pool.submit(probe_one, p): p for p in misses}
            for fut in as_completed(futs):
                p = futs[fut]
                done += 1
                res = fut.result()
                if res is not None:
                    cache[str(p.resolve())] = res
                if sys.stderr.isatty() and (done % 10 == 0 or done == n):
                    print(f"\r  {done}/{n}", end="", file=sys.stderr, flush=True)
        if sys.stderr.isatty():
            print("", file=sys.stderr, flush=True)
        save_cache(cache)

    # Now build Entry list from cache (which now has every probed file).
    for p in files:
        key = str(p.resolve())
        cached = cache.get(key)
        if not cached:
            continue  # probe failed
        cls = classify(p)
        if cls is None:
            continue
        show, season, kind = cls
        entries.append(Entry(
            path=p, show=show, season=season, kind=kind,
            score=int(cached.get("score", 3)),
            size=int(cached.get("size", 0)),
        ))
    return entries


# ---- rendering -------------------------------------------------------------

CELL_WIDTH = 2  # columns per cell ("  " on a colored background)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(av * (1 - t) + bv * t) for av, bv in zip(a, b))  # type: ignore[return-value]


def shade(rgb: tuple[int, int, int], score: int) -> tuple[int, int, int]:
    t = SCORE_BLEND.get(score, SCORE_BLEND[3])
    return _lerp(rgb, DARK_FLOOR, t)


def cell(rgb: tuple[int, int, int], score: int) -> str:
    return bg(shade(rgb, score)) + " " * CELL_WIDTH + RESET


def lifetime_stats() -> dict:
    """Read encode.py's run-history dotfile and return its lifetime totals,
    or an empty dict if the file doesn't exist."""
    try:
        with HISTORY_PATH.open() as f:
            return json.load(f).get("lifetime", {}) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def render(entries: list[Entry]) -> None:
    cols = shutil.get_terminal_size((120, 24)).columns
    cells_per_row = max(8, (cols - SEASON_PREFIX_COLS) // CELL_WIDTH)

    series = [e for e in entries if e.kind == "series"]
    movies = [e for e in entries if e.kind == "movie"]

    shows: dict[str, list[Entry]] = {}
    for e in series:
        shows.setdefault(e.show, []).append(e)

    # ---- header ----------------------------------------------------------
    s_done = sum(1 for e in series if e.score == 0)
    s_total = len(series)
    m_done = sum(1 for e in movies if e.score == 0)
    m_total = len(movies)
    total = s_total + m_total
    done = s_done + m_done
    pct = (done / total * 100) if total else 0.0
    total_size = sum(e.size for e in entries)

    print()
    print(f"  {BOLD}Library status{RESET}   {done}/{total} compliant ({pct:.1f}%)"
          f"   ·   episodes {s_done}/{s_total}   ·   movies {m_done}/{m_total}")

    # Second header line: disk usage now + lifetime savings reported by
    # encode.py's history file. The "saved" figure is cumulative across all
    # prior encode runs (.encode-history.json), so it survives Ctrl-C and
    # restarts. Skipped silently if the history file doesn't exist yet.
    life = lifetime_stats()
    parts = [f"{fmt_bytes(total_size)} on disk"]
    saved = int(life.get("saved_bytes") or 0)
    orig = int(life.get("original_bytes") or 0)
    encoded = int(life.get("files_encoded") or 0)
    if saved > 0 and orig > 0:
        ratio = saved / orig * 100
        parts.append(f"saved {fmt_bytes(saved)} across {encoded} prior encodes ({ratio:.0f}%)")
    print(f"  {BOLD}Disk{RESET}             {'   ·   '.join(parts)}")
    print()

    label_color = (130, 130, 130)  # season labels / counts: dim gray

    # ---- per-show panels -------------------------------------------------
    for show in sorted(shows.keys(), key=_natural_key):
        eps = shows[show]
        sd = sum(1 for e in eps if e.score == 0)
        st = len(eps)
        sz = sum(e.size for e in eps)
        # Show header — bold name, plain count, dim size.
        print(f"  {BOLD}{show}{RESET}   {sd}/{st}   "
              f"{fg(label_color)}{fmt_bytes(sz)}{RESET}")

        by_season: dict[int, list[Entry]] = {}
        for e in eps:
            by_season.setdefault(e.season, []).append(e)

        # Hue rotates by position in this show's season list, not the absolute
        # season number — a show with only S03+S04 still gets the first two
        # palette colors instead of skipping.
        for idx, sn in enumerate(sorted(by_season.keys())):
            color = PALETTE[idx % len(PALETTE)]
            seps = sorted(by_season[sn], key=lambda x: _natural_key(x.path.name))
            ssd = sum(1 for e in seps if e.score == 0)
            sst = len(seps)
            label = f"S{sn:02d}"
            count = f"{ssd:>3}/{sst:<3}"
            prefix = (f"    {fg(label_color)}{label}  {count}{RESET}  ")
            cells_list = [cell(color, e.score) for e in seps]
            _print_wrapped(prefix, cells_list, cells_per_row)
        print()

    # ---- movies block ----------------------------------------------------
    if movies:
        # Group movies by their top-level folder under movies/, then render
        # one row per group. One cell per file. Hue cycles per group so
        # adjacent rows are visually distinct.
        groups: dict[str, list[Entry]] = {}
        for m in movies:
            groups.setdefault(movie_group(m.path), []).append(m)

        movies_size = sum(e.size for e in movies)
        print(f"  {BOLD}Movies{RESET}   {m_done}/{m_total}   "
              f"{fg(label_color)}{fmt_bytes(movies_size)}{RESET}")
        for idx, g in enumerate(sorted(groups.keys(), key=_natural_key)):
            color = PALETTE[idx % len(PALETTE)]
            ms = sorted(groups[g], key=lambda e: _natural_key(e.path.name))
            gd = sum(1 for e in ms if e.score == 0)
            gt = len(ms)
            # Movie group names can be long — truncate to keep alignment.
            name = g if len(g) <= 50 else g[:49] + "…"
            count = f"{gd:>3}/{gt:<3}"
            prefix = (f"    {fg(label_color)}{name:<50} {count}{RESET}  ")
            cells_list = [cell(color, e.score) for e in ms]
            _print_wrapped(prefix, cells_list,
                           cells_per_row=max(4, cells_per_row - 34))
        print()

    # ---- footer / legend -------------------------------------------------
    # Legend uses neutral mid-gray so the shading scale itself is the focus
    # rather than any one season's hue. Same blend toward DARK_FLOOR as the
    # cells use, so what you see here is exactly how a real cell would step.
    legend_base = (180, 180, 180)
    swatches = "  ".join(
        bg(shade(legend_base, s)) + "  " + RESET + f" {label}"
        for s, label in [(0, "compliant"), (1, "1 violation"),
                         (2, "2 violations"), (3, "3 violations")]
    )
    print(f"  legend: {swatches}")
    print()


def _print_wrapped(prefix: str, cells: list[str], cells_per_row: int) -> None:
    """Print `prefix` + cell strip. Long strips wrap with continuation lines
    indented under the cell column (i.e. invisible-width of `prefix`)."""
    if not cells:
        print(prefix)
        return
    indent = " " * _visible_width(prefix)
    out: list[str] = [prefix]
    slot = 0
    for c in cells:
        if slot >= cells_per_row:
            print("".join(out))
            out = [indent]
            slot = 0
        out.append(c)
        slot += 1
    print("".join(out))


def _visible_width(s: str) -> int:
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


# ---- main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Library encoding-status grid (GitHub-style cells).",
    )
    ap.add_argument("directory",
                    help="media library root to walk, containing series/ and movies/")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the probe cache and re-probe every file")
    args = ap.parse_args()

    if not shutil.which("ffprobe"):
        print("ffprobe not found in PATH", file=sys.stderr)
        return 1

    root = Path(args.directory)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    entries = scan(root, use_cache=not args.no_cache)
    if not entries:
        print("no video files found (or none under series/movies)", file=sys.stderr)
        return 0

    render(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
