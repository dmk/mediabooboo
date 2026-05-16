#!/usr/bin/env -S python3 -B
"""Re-encode video files per ENCODING.md with a fancy progress bar.

Usage: encode.py [--replace] [--dry-run] <directory>

Walks <directory> recursively and re-encodes every video file:
  - Video: libx265, CRF 23, preset medium (main stream only).
           Sources taller than 720p are scaled with scale=-2:720; otherwise
           the original resolution is kept.
  - Cover art (attached_pic) video streams: copied through.
  - Audio: one Ukrainian + one English, picked by (channels desc, bitrate desc).
  - Subtitles: all kept (copied).
  - Attachments: all kept (copied).
  - Chapters + global tags + per-stream language/title tags: preserved.

Without --replace, output goes to <name>.hevc.mkv next to the source.
With --replace, the source file is overwritten on success (atomic rename).

Renders one progress bar across the whole run, segmented by parent directory
(season). Each segment uses a vibrant Google-ish color when processed, dimmed
when still pending.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Vibrant, pleasant palette inspired by the Google logo, extended to 9 hues so
# 9 X-Files seasons each get a distinct colour. Cycles if a directory has more
# groups than colours.
PALETTE = [
    (66, 133, 244),    # Google blue
    (234, 67, 53),     # Google red
    (251, 188, 5),     # Google yellow
    (52, 168, 83),     # Google green
    (255, 109, 0),     # vivid orange
    (171, 71, 188),    # purple
    (0, 188, 212),     # cyan
    (236, 64, 122),    # pink
    (124, 179, 66),    # lime
]

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".webm"}

# Persistent stats live next to the script so they share a fate with the media
# volume itself rather than the host's home directory.
HISTORY_PATH = Path(__file__).resolve().parent / ".encode-history.json"
MAX_RUN_HISTORY = 50

RESET = "\033[0m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_LINE = "\033[2K"
CURSOR_UP_1 = "\033[1A"


def fg(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\033[38;2;{r};{g};{b}m"


def bg(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\033[48;2;{r};{g};{b}m"


def dim(rgb: tuple[int, int, int], factor: float = 0.22) -> tuple[int, int, int]:
    return tuple(max(0, int(c * factor)) for c in rgb)  # type: ignore[return-value]


# How many display rows the ffmpeg command occupies. Held constant so the bar
# block always covers the same vertical real-estate (no layout shift).
CMD_LINE_COUNT = 6
FG_TEXT_BRIGHT = "\033[38;2;240;240;240m"
# Near-black, used for text sitting on a vivid season-color background so the
# foreground stays readable across the whole 9-hue palette (white-on-yellow
# is the worst-case otherwise).
FG_TEXT_DARK = "\033[38;2;15;15;15m"
FG_DIM = "\033[38;2;110;110;110m"

# Dim syntax-highlight palette for the ffmpeg command display — same hues as
# a normal scheme but knocked down to ~40% so the cmd block recedes below the
# bar and status line. Hue still readable, but clearly background.
TOKEN_COLORS = {
    "program": fg(dim((220, 220, 130), 0.45)),  # dim yellow
    "flag":    fg(dim((110, 160, 200), 0.50)),  # dim blue
    "value":   fg(dim((140, 180, 140), 0.50)),  # dim green
    "path":    fg(dim((200, 160, 110), 0.50)),  # dim tan
    "plain":   fg(dim((110, 110, 110), 0.55)),  # dim grey
}

# A "token" is (text, kind) — text is the literal characters, kind selects the
# colour. A "line" is a list of tokens rendered space-separated. The first
# token of an indented line carries its own leading "  " inside `text` so the
# space-join doesn't introduce an extra space at column 0.
Token = tuple[str, str]


def format_cmd(cmd: list[str]) -> list[list[Token]]:
    """Split an ffmpeg invocation into classified display lines."""
    if not cmd:
        return []
    program = cmd[0]
    rest = cmd[1:]
    lines: list[list[Token]] = []

    if "-i" in rest:
        i_idx = rest.index("-i")
        pre_input = rest[:i_idx]
        input_path: Optional[str] = rest[i_idx + 1]
        post_input = rest[i_idx + 2:]
    else:
        pre_input = rest
        input_path = None
        post_input = []

    output = post_input.pop() if post_input else None

    line: list[Token] = [(program, "program")]
    for tok in pre_input:
        line.append((tok, "flag" if tok.startswith("-") else "value"))
    lines.append(line)

    if input_path is not None:
        lines.append([("  -i", "flag"), (shlex.quote(input_path), "path")])

    metadata: list[Token] = []
    mappings: list[Token] = []
    codecs: list[Token] = []
    j = 0
    while j < len(post_input):
        tok = post_input[j]
        nxt = post_input[j + 1] if j + 1 < len(post_input) else None
        if tok in ("-map_metadata", "-map_chapters") and nxt is not None:
            metadata.extend([(tok, "flag"), (nxt, "value")])
            j += 2
        elif tok == "-map" and nxt is not None:
            mappings.extend([(tok, "flag"), (nxt, "value")])
            j += 2
        else:
            codecs.append((tok, "flag" if tok.startswith("-") else "value"))
            j += 1

    def with_indent(toks: list[Token]) -> list[Token]:
        if not toks:
            return toks
        head_text, head_kind = toks[0]
        return [("  " + head_text, head_kind)] + toks[1:]

    if metadata:
        lines.append(with_indent(metadata))
    if mappings:
        lines.append(with_indent(mappings))
    if codecs:
        lines.append(with_indent(codecs))
    if output is not None:
        lines.append([("  " + shlex.quote(output), "path")])
    return lines


def render_cmd_line(tokens: list[Token], max_len: int) -> str:
    """Render a token list with syntax-highlight colours, truncating to
    `max_len` visible columns. Color codes are zero-width and don't count."""
    parts: list[str] = []
    visible = 0
    for i, (text, kind) in enumerate(tokens):
        if i > 0:
            if visible + 1 >= max_len:
                break
            parts.append(" ")
            visible += 1
        chunk = text
        remaining = max_len - visible
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        parts.append(TOKEN_COLORS.get(kind, "") + chunk + RESET)
        visible += len(chunk)
        if visible >= max_len:
            break
    return "".join(parts)


def plain_cmd_line(tokens: list[Token]) -> str:
    return " ".join(t for t, _ in tokens)


@dataclass
class Job:
    path: Path
    group: str
    duration_us: int
    height: int
    main_v: int
    codec: str = ""
    cover_v: list[int] = field(default_factory=list)
    ukr_a: Optional[int] = None
    eng_a: Optional[int] = None
    already_done: bool = False
    # Frames per second of the source's main video stream. Used as a fallback
    # progress estimator: when the muxer has multiple output streams (video +
    # cover art + audio + subs), ffmpeg's -progress emits out_time_us=N/A for
    # the whole run even though `frame=N` keeps growing. We convert frames to
    # microseconds via this rate so the per-episode bar still fills.
    frame_rate: float = 0.0


def ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-print_format", "json", "--", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def is_compliant(streams: list[dict], main: dict) -> bool:
    """True iff the file already matches ENCODING.md: HEVC, height ≤ 720,
    and audio is at most one ukr + one eng with no other-language tracks.
    Subtitles, attachments, cover art are not constrained by the policy.
    """
    if (main.get("codec_name") or "").lower() != "hevc":
        return False
    if (main.get("height") or 0) > 720:
        return False
    ukr = eng = 0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        if lang == "ukr":
            ukr += 1
        elif lang == "eng":
            eng += 1
        else:
            return False  # foreign or untagged audio → policy says drop it
    return ukr <= 1 and eng <= 1


def _parse_rate(s: str) -> float:
    """Parse an ffprobe rational like '24000/1001' into a float. 0 on failure."""
    if not s or "/" not in s:
        return 0.0
    num, _, den = s.partition("/")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return 0.0
    return n / d if d > 0 else 0.0


def best_audio(streams: list[dict], lang: str) -> Optional[int]:
    cands = [
        s for s in streams
        if s.get("codec_type") == "audio"
        and (s.get("tags", {}).get("language") or "").lower() == lang
    ]
    if not cands:
        return None
    cands.sort(key=lambda s: (
        -(s.get("channels") or 0),
        -int(s.get("bit_rate") or 0),
    ))
    return cands[0]["index"]


def build_job(path: Path, group: str) -> Optional[Job]:
    try:
        info = ffprobe(path)
    except subprocess.CalledProcessError:
        return None
    streams = info.get("streams", [])
    duration = float(info.get("format", {}).get("duration") or 0)
    duration_us = int(duration * 1_000_000)
    main = next(
        (s for s in streams
         if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if not main or duration_us <= 0:
        return None
    cover = [
        s["index"] for s in streams
        if s.get("codec_type") == "video"
        and (s.get("disposition") or {}).get("attached_pic")
    ]
    codec = (main.get("codec_name") or "").lower()
    fps = (_parse_rate(main.get("avg_frame_rate") or "")
           or _parse_rate(main.get("r_frame_rate") or ""))
    return Job(
        path=path,
        group=group,
        duration_us=duration_us,
        height=int(main.get("height") or 0),
        main_v=main["index"],
        codec=codec,
        cover_v=cover,
        ukr_a=best_audio(streams, "ukr"),
        eng_a=best_audio(streams, "eng"),
        already_done=is_compliant(streams, main),
        frame_rate=fps,
    )


def _natural_key(name: str) -> list:
    import re
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name)]


def gather_jobs(root: Path) -> list[Job]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        # Skip our own outputs / leftovers.
        if p.name.endswith(".hevc.mkv") or p.name.endswith(".hevc.tmp.mkv"):
            continue
        if p.name.endswith(".hevc.mkv.tmp"):
            continue
        files.append(p)
    files.sort(key=lambda p: (_natural_key(str(p.parent)), _natural_key(p.name)))
    jobs: list[Job] = []
    for p in files:
        j = build_job(p, p.parent.name)
        if j:
            jobs.append(j)
    return jobs


def build_ffmpeg_cmd(job: Job, tmp: Path, scale: bool) -> list[str]:
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-i", str(job.path),
        "-map_metadata", "0", "-map_chapters", "0",
        "-map", f"0:{job.main_v}",
    ]
    for c in job.cover_v:
        args += ["-map", f"0:{c}"]
    if job.ukr_a is not None:
        args += ["-map", f"0:{job.ukr_a}"]
    if job.eng_a is not None:
        args += ["-map", f"0:{job.eng_a}"]
    args += [
        "-map", "0:s?", "-map", "0:t?",
        "-c", "copy",
        "-c:v:0", "libx265", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p", "-tag:v:0", "hvc1",
    ]
    if scale:
        args += ["-filter:v:0", "scale=-2:720"]
    args.append(str(tmp))
    return args


def fmt_bytes(n: float) -> str:
    if n < 0:
        return "-" + fmt_bytes(-n)
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{int(f)} B" if u == "B" else f"{f:.2f} {u}"
        f /= 1024
    return f"{f:.2f} TB"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _utc_now_iso() -> str:
    return (datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _empty_history() -> dict:
    return {
        "version": 1,
        "lifetime": {
            "files_encoded": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "original_bytes": 0,
            "encoded_bytes": 0,
            "saved_bytes": 0,
            "source_duration_us": 0,
            "wall_time_s": 0,
            "first_seen": None,
            "last_seen": None,
        },
        "runs": [],
    }


def _prior_savings_for_root(history: dict, root: Path) -> tuple[int, int]:
    """Sum (original_bytes, saved_bytes) from prior runs against this exact
    root, so the savings counter resumes where the last run left off after
    a Ctrl-C / restart. Matching is by absolute path string."""
    target = str(root.resolve())
    orig = saved = 0
    for run in history.get("runs", []) or []:
        if run.get("root") == target:
            orig += int(run.get("original_bytes") or 0)
            saved += int(run.get("saved_bytes") or 0)
    return orig, saved


def load_history() -> dict:
    if not HISTORY_PATH.exists():
        return _empty_history()
    try:
        data = json.loads(HISTORY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_history()
    if not isinstance(data, dict) or data.get("version") != 1:
        return _empty_history()
    # Defensive merge so older history files gain new lifetime keys.
    base = _empty_history()
    base["lifetime"].update(data.get("lifetime") or {})
    base["runs"] = list(data.get("runs") or [])
    return base


def save_history(history: dict) -> None:
    tmp = HISTORY_PATH.with_suffix(HISTORY_PATH.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(history, indent=2))
        tmp.replace(HISTORY_PATH)
    except OSError as e:
        print(f"warning: failed to save history: {e}", file=sys.stderr)


class ProgressUI:
    def __init__(self, jobs: list[Job], group_colors: dict[str, tuple[int, int, int]],
                 prior_orig: int = 0, prior_saved: int = 0):
        self.jobs = jobs
        self.group_colors = group_colors
        self.total_us = sum(j.duration_us for j in jobs) or 1
        self.completed_us = 0
        self.current: Optional[Job] = None
        self.current_us = 0
        self.current_speed = ""
        self.current_cmd = ""
        self.current_note = ""
        self.saved_bytes = 0
        self.orig_bytes = 0
        # Savings from earlier runs against this same root, displayed
        # additively so the counter doesn't reset on Ctrl-C/restart. Kept
        # separate from saved_bytes/orig_bytes so per-run history recording
        # in _record_run stays accurate.
        self.prior_orig_bytes = prior_orig
        self.prior_saved_bytes = prior_saved
        self.current_cmd_lines: list[list[Token]] = []
        self.start = time.monotonic()
        self.file_start_wall: Optional[float] = None
        self.last_render = 0.0
        self.lines_drawn = 0
        # Per-run accounting for history. Encoded counts only files actually
        # produced this run; orig/new bytes track those same files for an
        # accurate saved-bytes figure.
        self.encoded_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.encoded_orig_bytes = 0
        self.encoded_new_bytes = 0
        self.encoded_source_us = 0
        cols = shutil.get_terminal_size((100, 20)).columns
        self.cols = cols
        # Leave room for the suffix " 100.0%  ETA 999h99m  saved 999.9 GB (99%)"
        # (≈ 50 chars) so the rendered line never wraps past the terminal width —
        # wrapping breaks the cursor-up bookkeeping.
        self.width = max(20, cols - 50)
        # Pre-compute which group each character cell of the bar belongs to.
        self.cell_groups: list[str] = []
        cursor = 0
        for j in jobs:
            cursor += j.duration_us
            target = int(cursor / self.total_us * self.width)
            while len(self.cell_groups) < target:
                self.cell_groups.append(j.group)
        while len(self.cell_groups) < self.width:
            self.cell_groups.append(jobs[-1].group if jobs else "")

    def mark_skipped(self, job: Job) -> None:
        """Account for a file that won't run through ffmpeg (already compliant
        or output exists), so the bar reflects prior progress on resume."""
        self.completed_us = min(self.total_us, self.completed_us + job.duration_us)
        self.skipped_count += 1

    def mark_encoded(self, job: Job, original: int, encoded: int) -> None:
        """Account for a freshly-encoded file: bar savings + run stats."""
        self.add_savings(original, encoded)
        self.encoded_count += 1
        self.encoded_orig_bytes += original
        self.encoded_new_bytes += encoded
        self.encoded_source_us += job.duration_us

    def mark_failed(self, job: Job) -> None:
        self.failed_count += 1

    def add_savings(self, original: int, encoded: int) -> None:
        self.orig_bytes += original
        self.saved_bytes += original - encoded

    def file_start(self, job: Job, *, note: str = "",
                   cmd: Optional[list[str]] = None) -> None:
        self.current = job
        self.current_us = 0
        self.current_speed = ""
        self.current_cmd_lines = format_cmd(cmd) if cmd else []
        self.current_note = note
        self.file_start_wall = time.monotonic()
        self.render(force=True)

    def file_end(self) -> None:
        if self.current:
            self.completed_us = min(
                self.total_us, self.completed_us + self.current.duration_us
            )
        self.current = None
        self.current_us = 0
        self.current_cmd_lines = []
        self.current_note = ""
        self.file_start_wall = None
        self.render(force=True)

    def progress(self, current_us: int, speed: str) -> None:
        self.current_us = max(self.current_us, current_us)
        if speed:
            self.current_speed = speed
        self.render()

    def detach(self) -> None:
        """Drop a newline below the bar and reset bookkeeping so the next
        render starts fresh — used when something else needs to print
        permanent text (errors, dry-run output)."""
        if self.lines_drawn:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self.lines_drawn = 0

    def render(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_render < 0.1:
            return
        self.last_render = now
        progressed = self.completed_us
        if self.current:
            progressed += min(self.current_us, self.current.duration_us)
        progressed = min(progressed, self.total_us)

        filled = int(progressed / self.total_us * self.width)
        cells = []
        for i in range(self.width):
            grp = self.cell_groups[i] if i < len(self.cell_groups) else ""
            base = self.group_colors.get(grp, (128, 128, 128))
            colour = base if i < filled else dim(base)
            cells.append(fg(colour) + "█")
        bar = "".join(cells) + RESET

        pct = progressed / self.total_us * 100
        elapsed = now - self.start
        if progressed > 0:
            eta = elapsed * self.total_us / progressed - elapsed
            eta_s = fmt_duration(eta)
        else:
            eta_s = "—"

        max_text = max(1, self.cols - 1)

        if self.current:
            file_pct_val = (self.current_us / self.current.duration_us
                            if self.current.duration_us else 0.0)
            cur = f"{self.current.path.name}  ·  file {file_pct_val * 100:5.1f}%"
            # Wall-clock elapsed updates every render, so users see motion even
            # while libx265 is still warming up and out_time_us is N/A
            # (common on 10-bit HEVC sources like the Simpsons rips).
            if self.file_start_wall is not None:
                cur += f"  ·  {fmt_duration(now - self.file_start_wall)}"
            if self.current_speed and self.current_speed != "N/A":
                cur += f"  ·  {self.current_speed}"
            if self.current_note:
                cur += f"  ·  {self.current_note}"
        else:
            cur = "done" if progressed >= self.total_us else "…"
            file_pct_val = 0.0

        suffix = f" {pct:5.1f}%  ETA {eta_s}"
        total_orig = self.orig_bytes + self.prior_orig_bytes
        total_saved = self.saved_bytes + self.prior_saved_bytes
        if total_orig > 0:
            ratio = total_saved / total_orig * 100
            suffix += f"  ·  saved {fmt_bytes(total_saved)} ({ratio:.0f}%)"
        line1 = bar + suffix

        # Line 2: per-file progress bar of the same width as the main bar
        # above, using the same vivid/dim colour treatment as that bar — but
        # with the status text overlaid. Dark text on the vivid filled portion
        # keeps the hue readable across the full 9-hue palette; bright text on
        # the heavily-dimmed unfilled portion stays readable too. Any status
        # text beyond `self.width` columns spills over without a coloured
        # background, so the per-episode bar lines up with the main bar exactly.
        status_padded = cur[:max_text].ljust(max_text)
        if self.current:
            season = self.group_colors.get(self.current.group, (128, 128, 128))
            inside = status_padded[:self.width]
            outside = status_padded[self.width:]
            fill_chars = min(self.width, int(file_pct_val * self.width))
            filled = bg(season) + FG_TEXT_DARK + inside[:fill_chars]
            unfilled = bg(dim(season)) + FG_TEXT_BRIGHT + inside[fill_chars:]
            line2 = filled + unfilled + RESET + outside
        else:
            line2 = status_padded

        # Lines 3..N: syntax-highlighted multi-line ffmpeg command, padded to
        # a fixed height so the bar block doesn't shift between renders.
        cmd_lines: list[str] = []
        for tokens in self.current_cmd_lines:
            cmd_lines.append(render_cmd_line(tokens, max_text))
        while len(cmd_lines) < CMD_LINE_COUNT:
            cmd_lines.append("")
        cmd_lines = cmd_lines[:CMD_LINE_COUNT]

        rows = [line1, line2] + cmd_lines
        out = []
        if self.lines_drawn:
            # Move cursor to the row of the first line so we can rewrite all rows.
            out.append(f"\r\033[{self.lines_drawn - 1}A")
        for i, row in enumerate(rows):
            prefix = "" if i == 0 else "\n"
            out.append(f"{prefix}\r{CLEAR_LINE}{row}")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.lines_drawn = len(rows)

    def finalize(self) -> None:
        if self.lines_drawn:
            sys.stdout.write("\n")
            sys.stdout.flush()


def run_one(job: Job, replace: bool, ui: ProgressUI) -> None:
    if job.already_done:
        ui.mark_skipped(job)
        return

    if replace:
        out = job.path
        tmp = job.path.with_suffix(".hevc.tmp.mkv")
    else:
        out = job.path.with_suffix(".hevc.mkv")
        tmp = out.with_suffix(out.suffix + ".tmp")
        if out.exists():
            # Output from a prior run — count its savings opportunistically.
            try:
                ui.add_savings(job.path.stat().st_size, out.stat().st_size)
            except OSError:
                pass
            ui.mark_skipped(job)
            return

    try:
        original_size = job.path.stat().st_size
    except OSError:
        original_size = 0

    scale = job.height > 720
    cmd = build_ffmpeg_cmd(job, tmp, scale)
    note = "scale→720p" if scale else "no scale"
    ui.file_start(job, note=note, cmd=cmd)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    speed = ""
    last_us = 0
    us_per_frame = (1_000_000 / job.frame_rate) if job.frame_rate > 0 else 0.0
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key in ("out_time_us", "out_time_ms"):
                # Both are microseconds in modern ffmpeg; out_time_ms is a legacy mislabel.
                if val.isdigit():
                    last_us = max(last_us, int(val))
                    ui.progress(last_us, speed)
            elif key == "frame" and us_per_frame and val.isdigit():
                # Fallback when ffmpeg reports out_time_us=N/A — happens for
                # the whole run on multi-stream outputs (video + cover art +
                # audio + subs) even though the encode itself is fine.
                est = int(int(val) * us_per_frame)
                if est > last_us:
                    last_us = est
                    ui.progress(last_us, speed)
            elif key == "speed":
                speed = val.strip()
                ui.progress(last_us, speed)
            elif key == "progress" and val == "end":
                ui.progress(job.duration_us, speed)
        ret = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        tmp.unlink(missing_ok=True)
        raise

    if ret != 0:
        err = proc.stderr.read() if proc.stderr else ""
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg exit {ret}: {err.strip()}")

    try:
        encoded_size = tmp.stat().st_size
    except OSError:
        encoded_size = 0
    tmp.replace(out)
    if original_size and encoded_size:
        ui.mark_encoded(job, original_size, encoded_size)
    ui.file_end()


def _record_run(ui: ProgressUI, root: Path, started_at: str) -> None:
    """Append this run to the history file and print a small summary."""
    if ui.encoded_count == 0 and ui.skipped_count == 0 and ui.failed_count == 0:
        return
    wall_time_s = int(time.monotonic() - ui.start)
    saved = ui.encoded_orig_bytes - ui.encoded_new_bytes
    run = {
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "root": str(root.resolve()),
        "files_encoded": ui.encoded_count,
        "files_skipped": ui.skipped_count,
        "files_failed": ui.failed_count,
        "original_bytes": ui.encoded_orig_bytes,
        "encoded_bytes": ui.encoded_new_bytes,
        "saved_bytes": saved,
        "source_duration_us": ui.encoded_source_us,
        "wall_time_s": wall_time_s,
    }
    history = load_history()
    history["runs"].append(run)
    history["runs"] = history["runs"][-MAX_RUN_HISTORY:]

    life = history["lifetime"]
    life["files_encoded"] += run["files_encoded"]
    life["files_skipped"] += run["files_skipped"]
    life["files_failed"] += run["files_failed"]
    life["original_bytes"] += run["original_bytes"]
    life["encoded_bytes"] += run["encoded_bytes"]
    life["saved_bytes"] += run["saved_bytes"]
    life["source_duration_us"] += run["source_duration_us"]
    life["wall_time_s"] += run["wall_time_s"]
    if not life.get("first_seen"):
        life["first_seen"] = started_at
    life["last_seen"] = run["finished_at"]

    save_history(history)

    print(
        f"this run: encoded {run['files_encoded']}, skipped {run['files_skipped']}"
        f", failed {run['files_failed']} · saved {fmt_bytes(saved)}"
        f" in {fmt_duration(wall_time_s)}",
        file=sys.stderr,
    )
    print(
        f"lifetime: {life['files_encoded']} files · "
        f"saved {fmt_bytes(life['saved_bytes'])} · "
        f"encoded {fmt_duration(life['source_duration_us'] / 1_000_000)} of source",
        file=sys.stderr,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-encode video files per ENCODING.md.",
    )
    ap.add_argument("directory", help="root directory to walk")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite source files on success (atomic rename)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print ffmpeg commands instead of running them")
    args = ap.parse_args()

    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            print(f"{binary} not found in PATH", file=sys.stderr)
            return 1

    root = Path(args.directory)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    print("scanning…", file=sys.stderr, flush=True)
    jobs = gather_jobs(root)
    if not jobs:
        print("no eligible video files found", file=sys.stderr)
        return 0

    groups: list[str] = []
    for j in jobs:
        if j.group not in groups:
            groups.append(j.group)
    group_colors = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(groups)}

    total_s = sum(j.duration_us for j in jobs) / 1_000_000
    skip_count = sum(1 for j in jobs if j.already_done)
    todo_count = len(jobs) - skip_count
    print(
        f"{len(jobs)} files across {len(groups)} group(s) · "
        f"total source duration {fmt_duration(total_s)} · "
        f"{skip_count} already compliant, {todo_count} to encode",
        file=sys.stderr, flush=True,
    )

    if args.dry_run:
        cols = shutil.get_terminal_size((100, 20)).columns
        max_text = max(20, cols - 1)
        use_color = sys.stdout.isatty()
        for job in jobs:
            if job.already_done:
                print(f"# skip (already compliant): {job.path}")
                continue
            scale = job.height > 720
            tmp = (job.path.with_suffix(".hevc.tmp.mkv") if args.replace
                   else job.path.with_suffix(".hevc.mkv.tmp"))
            for tokens in format_cmd(build_ffmpeg_cmd(job, tmp, scale)):
                if use_color:
                    print(render_cmd_line(tokens, max_text))
                else:
                    print(plain_cmd_line(tokens))
            print()
        return 0

    prior_orig, prior_saved = _prior_savings_for_root(load_history(), root)
    ui = ProgressUI(jobs, group_colors,
                    prior_orig=prior_orig, prior_saved=prior_saved)
    if prior_saved > 0:
        print(
            f"resuming with {fmt_bytes(prior_saved)} saved from prior runs",
            file=sys.stderr, flush=True,
        )
    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    run_started_at = _utc_now_iso()
    rc = 0
    try:
        for job in jobs:
            try:
                run_one(job, args.replace, ui)
            except RuntimeError as e:
                ui.mark_failed(job)
                ui.detach()
                print(f"error on {job.path}: {e}", file=sys.stderr)
                rc = 1
    except KeyboardInterrupt:
        rc = 130
    finally:
        ui.finalize()
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        _record_run(ui, root, run_started_at)
    return rc


if __name__ == "__main__":
    # Make Ctrl-C clean even mid-render.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
