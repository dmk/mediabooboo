# Media Stack Troubleshooting

Quick reference for issues encountered on a Jellyfin + Plex + Sonarr + Radarr stack. Each section: symptom → diagnosis → fix (copy-paste ready).

Snippets reference three env vars. Export them once before running:

```bash
export STACK_DIR=~/Services/mystack          # compose deployment dir
export STACK_NAME=mystack                    # stack.name from answers.yaml
export LIBRARY_ROOT=/srv/media               # media library root
```

Container names use `${STACK_NAME}-<svc>`, matching the generated `stack.name`.

## Setup snapshot

- Media on host: `$LIBRARY_ROOT/series/`, `$LIBRARY_ROOT/movies/`
- Docker containers (compose stack): `${STACK_NAME}-jellyfin`, `${STACK_NAME}-plex`, `${STACK_NAME}-sonarr`, `${STACK_NAME}-radarr`, `${STACK_NAME}-prowlarr`, `${STACK_NAME}-jellyseerr`, `${STACK_NAME}-transmission`, `${STACK_NAME}-caddy`, `${STACK_NAME}-pihole`, `${STACK_NAME}-flaresolverr`, `${STACK_NAME}-beszel-agent`
- Container service configs: `$STACK_DIR/<service>/config/`
- Mount mapping (host → container):
  - Jellyfin: `$LIBRARY_ROOT/series` → `/media/series`, `$LIBRARY_ROOT/movies` → `/media/movies`
  - Plex: `$LIBRARY_ROOT/series` → `/series`, `$LIBRARY_ROOT/movies` → `/movies`, `$LIBRARY_ROOT/music` → `/music`
  - Sonarr: `$LIBRARY_ROOT/series` → `/series`, `$LIBRARY_ROOT/downloads` → `/downloads`
- API keys (read-only refs):
  - Sonarr: `$STACK_DIR/sonarr/config/config.xml` `<ApiKey>` tag
  - Sonarr is **not** exposed on host ports by default; hit API via `docker exec ${STACK_NAME}-sonarr curl …` against `localhost:8989`
  - Jellyfin: no static API key in config; must be created via UI (Dashboard → API Keys)

## Plex/Jellyfin file layout requirements

See `docs/CONVENTIONS.md` for the canonical layout and naming. The most common failure modes are missing year suffixes on show folders, episode files without `SxxExx`, or multiple movies in a single folder.

## Issue: Jellyfin can't play episodes after folder rename / DB UNIQUE constraint failures

**Symptom**
- Show page loads, metadata present, but Seasons list is empty
- Playback fails immediately
- Logs show repeated `SQLite Error 19: 'UNIQUE constraint failed: UserData.ItemId, UserData.UserId, UserData.CustomDataKey'`
- Or `'file is not a database'` / `'disk I/O error'` (often a corrupt `-wal` from the same root cause)
- "Scan Media Library" completes in seconds (instead of minutes) — it's bailing on the constraint failure

**Diagnosis**
Folder renames create duplicate `BaseItems` rows (old path + new path) sharing the same TVDB `CustomDataKey` for the same user. When Jellyfin tries to tombstone the old item (move it to placeholder ItemId `00000000-0000-0000-0000-000000000001`), the resulting `(tombstone, UserId, CustomDataKey)` collides with the new row's data → batch update aborts → reconciliation never completes.

Verify (read-only on a copy):
```bash
docker stop ${STACK_NAME}-jellyfin
cp $STACK_DIR/jellyfin/config/data/jellyfin.db /tmp/jellyfin.db.bak
docker start ${STACK_NAME}-jellyfin
sqlite3 /tmp/jellyfin.db.bak "PRAGMA integrity_check;"   # should be 'ok' even when broken
sqlite3 /tmp/jellyfin.db.bak "SELECT COUNT(*) FROM (SELECT UserId, CustomDataKey FROM UserData WHERE ItemId<>'00000000-0000-0000-0000-000000000001' GROUP BY UserId, CustomDataKey HAVING COUNT(*)>1);"
# If > 0, you have the duplicate-pair problem.
```

**Fix**
Delete `BaseItems` whose `Path` no longer exists on disk; `ON DELETE CASCADE` cleans up orphan `UserData`.

```bash
docker stop ${STACK_NAME}-jellyfin
cp $STACK_DIR/jellyfin/config/data/jellyfin.db /tmp/jellyfin.db.before_fix  # rollback point

python3 << 'PYEOF'
import sqlite3, os
DB = f"{os.environ['STACK_DIR']}/jellyfin/config/data/jellyfin.db"
SERIES_ROOT = f"{os.environ['LIBRARY_ROOT']}/series"
con = sqlite3.connect(DB); cur = con.cursor()
cur.execute("PRAGMA foreign_keys = ON;")
cur.execute("SELECT DISTINCT substr(Path, length('/media/series/') + 1, instr(substr(Path, length('/media/series/') + 1) || '/', '/') - 1) FROM BaseItems WHERE Path LIKE '/media/series/%'")
folders = [r[0] for r in cur.fetchall()]
stale = [f for f in folders if not os.path.exists(f"{SERIES_ROOT}/{f}")]
print("Stale folders:", stale)
for s in stale:
    cur.execute("DELETE FROM BaseItems WHERE Path = ? OR Path LIKE ?", (f"/media/series/{s}", f"/media/series/{s}/%"))
con.commit(); con.close()
PYEOF

sqlite3 $STACK_DIR/jellyfin/config/data/jellyfin.db "PRAGMA integrity_check;"
docker start ${STACK_NAME}-jellyfin
# Then trigger Dashboard → Scheduled Tasks → "Scan Media Library"
```

Rollback if needed: `docker stop ${STACK_NAME}-jellyfin && cp /tmp/jellyfin.db.before_fix $STACK_DIR/jellyfin/config/data/jellyfin.db && docker start ${STACK_NAME}-jellyfin`

**Note:** `PRAGMA foreign_keys` is off by default per-connection in SQLite. The DELETE only cascades when you enable it on the connection that performs the delete.

## Issue: Library scan doesn't discover renamed folders

**Symptom**
- Folder renamed on disk
- "Refresh Metadata" on the show in Jellyfin doesn't help
- Library still shows old paths

**Diagnosis**
"Refresh Metadata" only re-fetches TVDB info for an item Jellyfin already knows about — it does **not** re-walk the filesystem. The LibraryMonitor's auto-watcher on Docker Desktop / macOS bind mounts is unreliable (inotify events don't always propagate from APFS through Docker's VM). New folders are invisible until a full scan.

**Fix**
Trigger **Dashboard → Scheduled Tasks → "Scan Media Library"**. For reliability, schedule it daily (Dashboard → Scheduled Tasks → click the task → set a trigger):

```
Trigger: Daily at 04:00
```

Same applies to Plex (Settings → Library → Scheduled Tasks).

## Issue: Sonarr writes new downloads to old (pre-rename) paths

**Symptom**
- After renaming `From` → `From (2022)`, a new download appears at `$LIBRARY_ROOT/series/From/Season 4/...` (the old location)

**Diagnosis**
Sonarr stores an absolute `path` per series. Renaming the folder on disk doesn't update Sonarr; it keeps writing to the path it remembers.

**Inspect**
```bash
APIKEY=$(grep -oE 'ApiKey>[^<]+' $STACK_DIR/sonarr/config/config.xml | cut -d'>' -f2)
docker exec ${STACK_NAME}-sonarr curl -s -H "X-Api-Key: $APIKEY" http://localhost:8989/api/v3/series \
  | python3 -c "import json,sys; [print(f\"[{s['id']:3d}] {s['title']:50s} -> {s['path']}\") for s in sorted(json.load(sys.stdin), key=lambda x: x['title'])]"
```

**Fix**
- UI: Series → click show → Edit → update **Path** to match new folder → Save
- API: `PUT /api/v3/series/{id}` with updated `path` field (preserve the rest of the body)

Watch for **pre-existing wrong matches** (Sonarr matched the folder to a totally different TVDB show). Examples seen here: "Daredevils" mapped to `Daredevil (2015)`, "Three - The Web Series" mapped to Futurama folder. Path edit won't help; delete + re-add with correct TVDB lookup.

## Issue: Plex shows nothing under TV

**Symptom**
- Movies library populated, no TV shows visible

**Diagnosis**
Check Plex's configured library sections:
```bash
docker exec ${STACK_NAME}-plex "/usr/lib/plexmediaserver/Plex Media Scanner" --list
```
If only "Movies" shows up, the TV library wasn't created.

**Fix**
Plex Web → Settings → Manage → Libraries → Add Library → **TV Shows** → point at `/series` (container path).

## Issue: Plex movies stuck on `local://` stubs / wrong-film matches after bulk import

**Symptom**
- After dropping in many new movie folders at once, some appear in Plex with stripped or odd titles (e.g. "The Darjeeling" instead of "The Darjeeling Limited"), no poster, no synopsis
- Or a movie matched a completely unrelated film (seen here: Asteroid City → "1 city and 24h")
- "Refresh Metadata" or a re-scan doesn't fix it

**Diagnosis**
Plex's online agent didn't resolve the match on first scan, so the item is stuck on a `local://` GUID — a placeholder Plex built from the filename. Re-scan/refresh just re-walks the local data; they don't replace the agent decision. The wrong-match variant happens when the agent *did* fire but the first low-confidence hit got committed.

Identify offenders by GUID prefix:
```bash
TOKEN=$(grep -oE 'PlexOnlineToken="[^"]+"' "$STACK_DIR/plex/config/Library/Application Support/Plex Media Server/Preferences.xml" | head -1 | cut -d'"' -f2)
docker exec ${STACK_NAME}-plex curl -s "http://localhost:32400/library/sections/1/all?X-Plex-Token=$TOKEN" \
  | python3 -c "import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
for v in sorted(root.findall('Video'), key=lambda x: x.get('title','')):
    if v.get('guid','').startswith('local://'):
        print(f\"[{v.get('ratingKey')}] {v.get('title')} ({v.get('year','?')})  STUB\")"
```

**Fix — re-match via API (no UI clicks)**
For each offender: list candidates → apply the right GUID → refresh metadata.
```bash
TOKEN=<token>
ID=<ratingKey>
TITLE="The Darjeeling Limited"
YEAR=2007

# 1. list candidates (top result is usually correct)
T=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$TITLE")
docker exec ${STACK_NAME}-plex curl -s \
  "http://localhost:32400/library/metadata/$ID/matches?manual=1&title=$T&year=$YEAR&X-Plex-Token=$TOKEN" \
  | python3 -c "import sys, xml.etree.ElementTree as ET
for m in ET.fromstring(sys.stdin.read()).findall('SearchResult')[:5]:
    print(f\"  {m.get('name','-')[:50]:50s} ({m.get('year','-')})  {m.get('guid','-')}\")"

# 2. apply the chosen GUID
GUID="plex://movie/5d9f34f16fc551001ef7f446"
G=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$GUID")
docker exec ${STACK_NAME}-plex curl -s -X PUT \
  "http://localhost:32400/library/metadata/$ID/match?guid=$G&name=$T&X-Plex-Token=$TOKEN"

# 3. pull posters/synopsis/art
docker exec ${STACK_NAME}-plex curl -s -X PUT \
  "http://localhost:32400/library/metadata/$ID/refresh?X-Plex-Token=$TOKEN"
```

**Prevention**
- After bulk-adding folders, scan the library list for `local://` GUIDs once before considering the import done.
- Plex needs folder-per-movie (`Movie Name (Year)/Movie Name (Year).ext`). A folder holding multiple films won't match correctly — split it first.

## Issue: Playback hits old path immediately after rename

**Symptom**
- Logs show ffmpeg called with the pre-rename path
- `FFmpegException: FFmpeg exited with code 254`
- Stack trace includes `DirectoryNotFoundException: Could not find a part of the path '…'`

**Diagnosis**
DB still has stale paths. Exit code **254** here is *file-not-found*, not a codec problem. Don't waste time on HEVC/transcoding — it never got that far.

**Fix**
Same as "Library scan doesn't discover renamed folders" above. If that scan can't complete due to constraint failures, apply the "UNIQUE constraint" fix first.

## Useful one-liners

```bash
# Tail Jellyfin logs (strip .NET stack frames)
docker logs --since 5m ${STACK_NAME}-jellyfin 2>&1 | grep -v "^   at " | tail -40

# Find a specific show's known paths in Jellyfin's DB
sqlite3 $STACK_DIR/jellyfin/config/data/jellyfin.db \
  "SELECT Id, Type, Path FROM BaseItems WHERE Path LIKE '%X-Files%';"

# Probe a media file's codec from inside Jellyfin (use the container's ffmpeg, not host)
docker exec ${STACK_NAME}-jellyfin /usr/lib/jellyfin-ffmpeg/ffprobe -v error -show_streams \
  "/media/series/<show>/<file>" | head

# Force a clean DB checkpoint without restart (when -wal grows large)
docker exec ${STACK_NAME}-jellyfin sqlite3 /config/data/jellyfin.db "PRAGMA wal_checkpoint(TRUNCATE);"
# (only works if sqlite3 is installed in container — Jellyfin image lacks it; use host sqlite3 with the host path instead)

# Restart Jellyfin (forces WAL checkpoint, releases stale locks)
docker restart ${STACK_NAME}-jellyfin

# Watch a scan in progress
docker logs -f --since 5s ${STACK_NAME}-jellyfin 2>&1 | grep -v "^   at " | grep -iE "scan|Adding|Removing item|error|UNIQUE|task.*completed"

# Grab Plex's online token (needed for the /library API)
grep -oE 'PlexOnlineToken="[^"]+"' "$STACK_DIR/plex/config/Library/Application Support/Plex Media Server/Preferences.xml" | head -1 | cut -d'"' -f2

# Dump every movie in section 1 with its matched GUID (spot `local://` stubs / wrong matches)
TOKEN=<token>
docker exec ${STACK_NAME}-plex curl -s "http://localhost:32400/library/sections/1/all?X-Plex-Token=$TOKEN" \
  | python3 -c "import sys, xml.etree.ElementTree as ET
for v in sorted(ET.fromstring(sys.stdin.read()).findall('Video'), key=lambda x: x.get('title','')):
    print(f\"[{v.get('ratingKey'):>5}] {v.get('title'):50s} ({v.get('year','????')})  {v.get('guid','-')}\")"
```

## Things that look like problems but aren't

- **Empty `SQLiteBackups/` directory** — Jellyfin only writes backups around version upgrades, not routinely.
- **`.DS_Store` files in series subfolders** — macOS-generated, ignored by both Plex and Jellyfin.
- **`@eaDir` folders** — Synology metadata sidecars (from prior NAS storage). Harmless.
- **Container time differs from host** — Jellyfin logs in container-local TZ. `docker exec ${STACK_NAME}-jellyfin date` to align.
- **`docker logs` showing very old timestamps** — `docker logs` keeps history across `docker restart`. Use `--since 5m` to scope.
- **`Plex Media Scanner --list` only prints one item** — that flag is for listing *sections* (where it's correct) and unreliable for in-section enumeration. Use `--tree` for the full contents of a section.
