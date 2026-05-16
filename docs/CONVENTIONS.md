# Library conventions

The library tooling (`library/encode.py`, `library/library.py`) and the *arr/Plex/Jellyfin matchers all assume the layout and naming below. Deviations cause silent metadata mismatches.

## Layout

Under `paths.library` (e.g. `/srv/media`):

```
movies/
  Movie Name (Year)/
    Movie Name (Year).ext
series/
  Show Name (Year)/
    Season 01/
      Show Name - S01E01 - Title.ext
      Show Name - S01E02 - Title.ext
music/
  Artist/Album/track.ext
downloads/
  watch/         # transmission watch dir
  ...            # in-flight + completed downloads (organised by *arr)
```

## Naming rules

- **Movies** — folder per movie, name + year: `Asteroid City (2023)/Asteroid City (2023).mkv`. Year disambiguates remakes and improves agent matching. A folder holding multiple films won't match correctly — split it.
- **Show folders** — `Show Name (Year)/` — year matters for ambiguous titles.
- **Seasons** — `Season 01/`, `Season 02/` … zero-padded. `Season 00/` for specials. Both padded and unpadded are tolerated; padded is preferred for sort order.
- **Episodes** — must contain `SxxExx` (or the `1x01` alt form). Clean form: `Show Name - SxxExx - Title.ext`.
- **Multi-episode files** — `Show - S01E12-E13 - Title.ext`.
- **Year-as-season** — animation shorts (Looney Tunes etc.) can use `Season 1932/` when matching against TVDB's year-grouped seasons.

## Encoding policy

Re-encode every file. Cap height at 720p — sources taller than 720p are scaled with `scale=-2:720`; sources at or below 720p keep their resolution.

**Target:** HEVC (libx265), CRF 23, preset medium.

### Tracks to keep

- **Video** — main video stream only (re-encoded). Attached cover art (mjpeg) copied through.
- **Audio** — one Ukrainian + one English. Pick the highest-quality variant: most channels first, then highest bitrate.
- **Subtitles** — keep all.

### Metadata to preserve

- Chapters and global container tags (title, etc.).
- Per-stream language and title tags on every kept track.
- Attachments (fonts, posters, NFO, etc.).

`encode.py` implements this policy. See `library/encode.py --help` for flags.
