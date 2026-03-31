# Changelog

All notable changes to Duparr are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.1.0] — 2026-03-31

### Added

- **Pipeline UI** — six-step workflow enforced in order (Scan → Fix Tags → Album Dupes → Rescan → Find Dupes → Apply). State persists across container restarts in `/data/pipeline_state.json`. Steps can be skipped from the UI.
- **Tag Fixer** (`tagger.py`) — unified scan that detects and fixes two tag problem types in one pass:
  - *COLLECTION*: Album tag contains collection path junk (e.g. `"1989 - GREATEST HITS • Roy Orbison - 40 Greatest Hits [EU Vinyl 3LP]"`). Extracts real artist and album title from bullet pattern. Writes Artist, Album Artist, and Album tags.
  - *COMPILATION*: Multi-artist folder with missing or wrong Album Artist. Detects dominant artist or falls back to `Various Artists`.
  - Multilingual Various Artists variant list (English, Dutch, German, French, Italian)
  - Smart false-positive avoidance: albums where all tracks already agree on the same Album Artist are skipped — including DJ compilations tagged with the curator's name (e.g. Judge Jules, Darren Styles) and albums where featured artists appear as lead on some tracks (e.g. Pitbull)
  - Artist name normalisation: `"Zombies, The"` / `"The Zombies"` and `"Boney M."` / `"Boney M"` treated as identical
  - Full undo support — restore original Album Artist tags per folder
- **Album Deduplication** (`albumdeduper.py`) — finds entire duplicate album folders and moves the weaker copy atomically:
  - Subset matching: every track in the weaker folder must match a track in the stronger one
  - Disc sibling detection: CD1/CD2, Digital Media 01/02, Side A/B are never treated as duplicates
  - Minimum 3 matched tracks required
  - Full undo support — restore moved album folders from the UI
- **Cover Art Fetcher** (`cover.py`) — fetches and embeds missing album artwork:
  - Sources in priority order: MusicBrainz Cover Art Archive, Deezer, iTunes, Last.fm (optional, requires API key)
  - Smart query cleaning — strips bracket junk, format noise, disc suffixes, and collection path prefixes from album names before searching
  - Falls back to original (uncleaned) query per source if the cleaned version returns nothing
  - Verbose per-source logging — each source reports result count, image dimensions, and failure reason
  - Supports MP3 (ID3), FLAC, M4A/AAC, OGG, Opus
  - Saves `cover.jpg` alongside tracks and optionally embeds into files
- **Deezer** added as cover art source — no API key required, high quality, tried after MusicBrainz
- **Scanner raw-mode fallback** — when `easy=True` fails on an MP3 with a non-standard header, retries with `easy=False` before skipping; recovers files that are valid but use non-standard encoders
- **Scanner unreadable summary** — instead of printing a warning per file, counts silently and shows a single summary line at the end: `⚠️ N files skipped (unreadable/corrupt)`
- **Incremental scanning** — tracks store `mtime`; unchanged files are skipped on repeat scans
- **Basic auth** — set `DUPARR_PASSWORD` env var to enable. Off by default. `/health` always public.
- **`EXCLUDE_DIRS` config** — comma-separated list of directories to skip during scanning
- **`FPCALC_TIMEOUT` config** — configurable fpcalc timeout (default 60s) for large FLACs on slow NAS
- **`COLLECTIONS_PENALTY` config** — configurable score penalty for `_collections` folders
- **`LASTFM_API_KEY` config** — enables Last.fm as cover art fallback
- **Job history** — last 50 jobs logged to `/data/job_history.json`, visible on the Report page
- **Live progress reporting** — scan and dedup progress streamed to UI in real time
- **`followlinks=False`** on scanner — symlinks are visible to Navidrome but the scanner never follows them into their target directories
- **UI cap** — duplicate groups capped at 500 for browser performance; highest priority shown first
- **Navidrome rescan trigger** — auto-triggers Navidrome library scan after Apply if `NAVIDROME_URL` is set

### Changed

- Scanner now removes stale DB entries for files no longer on disk before each scan (`cleanup_missing`)
- Deduper same-folder safety: tracks in the same folder (or disc sibling subfolders) require fingerprint confirmation rather than metadata match alone
- `db.py` rewritten for thread safety — short-lived connections per call with WAL mode, replacing the shared connection model
- Score caching: `score()` computed once per track and cached on the track dict
- Artist normalisation preserves order (returns tuple) with a paired frozenset for overlap checks
- UI groups sorted by confidence then size (HIGH first, largest savings first)
- `COVER_SOURCES` default updated to `musicbrainz,deezer,itunes` to reflect Deezer addition

### Fixed

- **Pipeline step index off-by-one** — dashboard action button showed wrong step after Scan completed (showed Album Dupes instead of Fix Tags). Caused by stale index from a previous pipeline structure.
- **`unreadable` counter** in `scanner.py` now correctly initialised before use (introduced alongside the unreadable summary feature in this release)

---

## [1.0.0] — 2025

### Added

- Initial release
- Track-level duplicate detection: Chromaprint fingerprint (Pass 1) + `(primary_artist, clean_title)` identity match (Pass 2)
- Quality scoring: format, bitrate, tags, artwork, filesize, compilation/collections penalties
- Variant detection: bracket keywords, multi-word phrases, dash suffixes
- Web UI: dashboard, duplicate review, apply, undo, logs
- SQLite database with migration support
- Docker deployment with named volume for persistence
- Navidrome integration (rescan trigger)
- Report download (plain text)
- `MIN_SCORE_GAP` config to skip near-equal duplicates
- `USE_FINGERPRINT` toggle
