# Duparr

**Duplicate music library manager for self-hosters.**

Duparr scans your music library, identifies duplicate tracks, and moves them to a safe holding folder — nothing is ever deleted. A web UI lets you review, apply, and undo everything.

Built for Navidrome + Symfonium but works with any music library on disk.

---

## Features

- **Two-pass duplicate detection**
  - Chromaprint audio fingerprint (primary) — catches duplicates across formats and bitrates
  - Artist + title identity matching (fallback) — one artist, one song, one copy
- **Variant preservation** — remixes, live versions, acoustic versions are always kept
- **Quality scoring** — keeps the best copy (FLAC over MP3, higher bitrate, proper artist folders over compilations)
- **Incremental scanning** — after the first scan, only new and changed files are processed
- **Safe by design** — files are moved to `/duplicates`, never deleted. Full undo from the UI
- **Web UI** — review groups, apply moves, restore files, view job history and logs
- **Navidrome integration** — optionally trigger a library rescan after moves
- **Docker / Portainer ready** — single container, persistent data volume

---

## Quick Start

### 1. Clone and build

```bash
git clone https://github.com/yourname/duparr.git
cd duparr
docker build -t duparr-local .
```

### 2. Create your `docker-compose.yml`

```yaml
services:
  duparr:
    image: duparr-local
    pull_policy: never
    container_name: duparr
    restart: unless-stopped
    ports:
      - "5045:5045"
    volumes:
      - /path/to/your/music:/music-rw
      - /path/to/your/duplicates:/duplicates
      - duparr-data:/data
      - ./ui:/ui
    environment:
      MUSIC_DIR: /music-rw
      DUP_DIR: /duplicates
      DB_PATH: /data/music.db
      USE_FINGERPRINT: "false"   # set true for deeper matching (slower)
      MIN_SCORE_GAP: "0"         # 0 = move all dupes regardless of quality gap

volumes:
  duparr-data:
```

### 3. Deploy and open

```bash
docker compose up -d
```

Open `http://your-server:5045`

---

## Usage

### Workflow

1. **Scan + Find Dupes** — indexes your library and groups duplicates
2. **Review** — browse groups in the UI, check what will be moved
3. **Apply All & Rescan** — moves duplicates to `/duplicates`, rescans, refreshes
4. Repeat until recoverable space stops dropping

### First scan

The first scan processes your entire library. Subsequent scans are incremental — only new or changed files are processed, completing in seconds.

If you want to force a full rescan, wipe the database:

```bash
docker exec duparr python3 -c "
import sys; sys.path.insert(0, '/app')
import config
from db import Database
db = Database(config.DB_PATH)
db.conn.execute('DELETE FROM tracks')
db.conn.commit()
print('Done')
"
```

### Undoing moves

Open `/undo` in the UI to see all files in `/duplicates` and restore them individually or all at once.

---

## Configuration

All settings are environment variables — no config files to edit.

| Variable | Default | Description |
|---|---|---|
| `MUSIC_DIR` | `/music` | Path to your music library inside the container |
| `DUP_DIR` | `/duplicates` | Where duplicates are moved |
| `DB_PATH` | `/data/music.db` | SQLite database location |
| `USE_FINGERPRINT` | `true` | Enable Chromaprint audio fingerprinting |
| `FPCALC_PATH` | `/usr/bin/fpcalc` | Path to fpcalc binary |
| `FPCALC_TIMEOUT` | `60` | Seconds before fingerprint times out per file |
| `MIN_SCORE_GAP` | `10` | Minimum quality difference before moving a dupe. Set `0` to move all |
| `COLLECTIONS_PENALTY` | `100` | Score penalty for tracks in `_collections` folders |
| `EXCLUDE_DIRS` | _(empty)_ | Comma-separated paths to skip during scanning |
| `COVER_SOURCES` | `musicbrainz,itunes` | Cover art sources in priority order |
| `COVER_MIN_SIZE` | `300` | Minimum cover dimension in pixels |
| `COVER_EMBED` | `true` | Embed fetched covers into audio files |
| `COVER_SAVE_FILE` | `true` | Save fetched covers as `cover.jpg` in album folder |
| `LASTFM_API_KEY` | _(empty)_ | Last.fm API key for cover fetching |
| `NAVIDROME_URL` | _(empty)_ | e.g. `http://navidrome:4533` — triggers rescan after moves |
| `NAVIDROME_USER` | _(empty)_ | Navidrome username |
| `NAVIDROME_PASSWORD` | _(empty)_ | Navidrome password |

---

## How duplicate detection works

### Pass 1 — Fingerprint

Uses Chromaprint (`fpcalc`) to generate an audio fingerprint for each file. Identical audio produces identical fingerprints regardless of format, bitrate, or tags. This catches the same recording in FLAC and MP3 simultaneously.

Enable with `USE_FINGERPRINT=true`. Slower but more thorough — recommended for a one-time deep clean.

### Pass 2 — Identity matching

Groups tracks by `(primary_artist, clean_title)`. "Bad" by Michael Jackson from six different compilation albums all map to the same identity key and form one group.

- Primary artist = first token from the artist tag (order-preserved)
- Clean title = title with bracket variant content stripped, punctuation removed, lowercased
- Same-folder tracks are excluded — two tracks in the same album folder are almost certainly different songs, not duplicates

### Variant detection

Tracks are classified as variants and **never moved** if their title contains recognised keywords in brackets:

- `Bad (Remix)` → variant
- `Bad [Live at Wembley]` → variant  
- `Bad` → original

Multi-word phrases in the file path are also checked (`radio edit`, `extended mix`, `club mix` etc.). Single-word path matching is intentionally avoided — words like `mix`, `live`, `edit` appear in too many legitimate folder and album names.

### Scoring

The keeper in each group is the highest-scoring track:

| Factor | Points |
|---|---|
| FLAC format | +100 |
| AAC / OGG / Opus | +50 |
| MP3 | +30 |
| Bitrate (capped) | +0 to +50 |
| Complete tags | +10 |
| Has artwork | +5 |
| File size tiebreaker | +0 to +10 |
| `_collections` folder | −100 |
| Compilation album | −40 |

Properly organised artist folders always beat compilation dumps. FLAC always beats MP3.

---

## Excluding folders

If you have torrent staging folders, symlink targets, or any other paths inside your music directory that should not be scanned:

```yaml
environment:
  EXCLUDE_DIRS: /music-rw/staging,/music-rw/tmp
```

Note: if you use hard links from a torrent client into your music folder, make sure the hard link destination is a clean path that Duparr will scan correctly. Avoid hard linking the full absolute path structure (e.g. `/music/mnt/data/torrents/...`) as this creates phantom duplicates.

---

## Fingerprinting performance

On large libraries (50k+ tracks), fingerprinting can take many hours. Recommended approach:

1. Run with `USE_FINGERPRINT=false` first — identity matching catches the majority of duplicates quickly
2. Once the library is clean, enable `USE_FINGERPRINT=true` for a deeper pass to catch cross-format duplicates

---

## Project structure

```
duparr/
├── app/
│   ├── main.py        # CLI orchestrator
│   ├── app.py         # Flask web UI
│   ├── scanner.py     # Library scanner with incremental support
│   ├── deduper.py     # Duplicate detection and scoring engine
│   ├── cover.py       # Album art fetcher
│   ├── db.py          # SQLite database layer
│   ├── config.py      # Configuration from environment variables
│   └── templates/
│       ├── index.html # Dashboard
│       ├── report.html
│       ├── undo.html
│       └── report.html
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Requirements

- Docker
- A music library on disk
- (~Optional) Chromaprint (`fpcalc`) — included in the Docker image

---

## Licence

MIT
