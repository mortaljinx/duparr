# Duparr

**Enforce a clean music library: one artist, one song, one copy.**

Duparr is a self-hosted music deduplication engine that scans your library, groups duplicate tracks using metadata and optional audio fingerprinting, scores them by quality, and lets you safely review and move them — nothing is ever deleted.

![Docker Pulls](https://img.shields.io/badge/status-v1.0.0-green?style=flat-square) ![License](https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square)

---

## ✨ Why Duparr?

Most deduplication tools are either too aggressive or too naive.

Duparr is built for real-world, messy libraries:

- **Understands music context** — not just filenames or hashes
- **Explains every decision** — no black box behaviour
- **Designed for safety** — nothing happens without review
- **Built for self-hosters** — works perfectly with Navidrome, Symfonium, or any local setup

---

## 🔑 Key Features

- 🛡 **Never deletes** — files are moved to `/duplicates`, fully reversible from the UI
- 🧠 **Dual detection engine** — metadata matching (fast) + optional Chromaprint fingerprinting (accurate, cross-format)
- 🎧 **Variant preservation** — remixes, live versions, acoustic versions, and edits are always kept
- ⚖️ **Smart scoring** — automatically keeps the best version: FLAC > AAC/OGG > MP3, bitrate, tags, artwork
- 🧩 **Same-album safety guard** — tracks in the same folder require fingerprint confirmation, preventing false positives on CD1/CD2, vinyl sides, and box sets
- 🔍 **Explainable decisions** — every duplicate shows match type, confidence level, and score difference
- ⚡ **Incremental scanning** — after the first run, only new or changed files are processed
- 🌐 **Web UI** — review groups, apply changes, undo moves, and monitor progress live
- ↩️ **Full undo** — restore any moved file to its original location
- 🖼 **Cover art fetcher** — automatically finds and embeds missing album art from MusicBrainz and iTunes
- 🔐 **Optional auth** — basic auth via env var, off by default for trusted LAN use

---

## 🧭 How It Works

```
Scan → Index → Group → Score → Review → Move → Undo
```

1. **Scan** — reads your library into SQLite (incremental — fast after first run)
2. **Group** — Pass 1: audio fingerprint match. Pass 2: `(primary_artist, clean_title)` identity match. Same-folder tracks require fingerprint confirmation.
3. **Score** — each track is scored, best version becomes the keeper
4. **Review** — UI shows keeper vs duplicates, confidence level, reason for match, space saved
5. **Apply** — moves duplicates to `/duplicates`, DB updated only after confirmed move
6. **Undo** — restore anything instantly from the Undo page

---

## 📊 Confidence Levels

| Level | Meaning |
|---|---|
| HIGH | Fingerprint match or metadata match with strong score difference |
| MEDIUM | Metadata match with clear winner |
| LOW | Small score gap — manual review recommended |

👉 Start by cleaning HIGH confidence groups for safe wins.

---

## ⚖️ Scoring System

| Condition | Points |
|---|---|
| FLAC format | +100 |
| AAC / OGG / Opus | +50 |
| MP3 | +30 |
| Bitrate (proportional, capped) | +0 to +50 |
| Complete tags | +10 |
| Has embedded artwork | +5 |
| Filesize tiebreaker | +0 to +10 |
| `_collections` folder | −100 |
| Compilation album | −40 |

---

## 🛡 Safety First

Duparr is designed to avoid destructive mistakes:

- Never deletes files
- Same-folder matches require fingerprint confirmation
- Variants are always preserved
- Score gap prevents near-equal files being moved
- Full undo for every operation

👉 You are always in control — nothing happens without review.

---

## 🚀 Quick Start

### 1. Clone

```bash
git clone https://github.com/mortaljinx/duparr.git
cd duparr
```

### 2. Configure `docker-compose.yml`

```yaml
services:
  duparr:
    image: duparr-local
    build: .
    container_name: duparr
    restart: unless-stopped
    ports:
      - "5045:5045"
    volumes:
      - /your/music:/music-rw
      - /your/duplicates:/duplicates
      - ./ui:/ui
      - duparr-data:/data
    environment:
      MUSIC_DIR: /music-rw
      DUP_DIR: /duplicates
      DB_PATH: /data/music.db
      USE_FINGERPRINT: "false"
      MIN_SCORE_GAP: "0"

volumes:
  duparr-data:
```

### 3. Build and run

```bash
docker compose build
docker compose up -d
```

### 4. Open the UI

```
http://your-server:5045
```

Click **SCAN + FIND** to begin.

---

## ⚙️ Configuration

| Variable | Default | Description |
|---|---|---|
| `MUSIC_DIR` | `/music-rw` | Music library path inside container |
| `DUP_DIR` | `/duplicates` | Duplicate storage path |
| `DB_PATH` | `/data/music.db` | SQLite database path |
| `USE_FINGERPRINT` | `false` | Enable Chromaprint audio fingerprinting |
| `FPCALC_PATH` | `/usr/bin/fpcalc` | Path to fpcalc binary |
| `MIN_SCORE_GAP` | `0` | Minimum quality gap before flagging a duplicate |
| `EXCLUDE_DIRS` | _(empty)_ | Directories to skip, comma-separated |
| `COVER_SOURCES` | `musicbrainz,itunes` | Cover art sources in priority order |
| `COVER_MIN_SIZE` | `300` | Minimum cover dimension in pixels |
| `COVER_EMBED` | `true` | Embed cover art into audio files |
| `COVER_SAVE_FILE` | `true` | Save `cover.jpg` alongside tracks |
| `DUPARR_PASSWORD` | _(empty)_ | Enable basic auth — leave empty to disable |
| `NAVIDROME_URL` | _(empty)_ | Auto-trigger Navidrome rescan after Apply |
| `NAVIDROME_USER` | _(empty)_ | Navidrome username |
| `NAVIDROME_PASSWORD` | _(empty)_ | Navidrome password |

👉 Set `MIN_SCORE_GAP=0` to flag all duplicates. Raise it (e.g. `10`) to only flag clear quality differences.

---

## 🔐 Authentication

Auth is **off by default** — suitable for trusted LAN use behind your router.

To enable, add to your `docker-compose.yml`:

```yaml
environment:
  DUPARR_PASSWORD: "your-password-here"
```

The browser will prompt for a password. The `/health` endpoint is always public for Docker healthchecks.

---

## 🎧 Variant Detection

Variants are always preserved — they appear in the UI as **VAR.** and are never moved.

Detected via:

- **Bracket keywords** in title: `(Remix)`, `[Live]`, `(Acoustic)`, `(Demo)`, `(Instrumental)` etc.
- **Multi-word bracket phrases**: `(Radio Edit)`, `(Extended Mix)`, `(Club Version)` etc.
- **Dash-suffix patterns**: `Song - Live`, `Song - Acoustic Mix`, `Song - Remaster`
- **Path phrases**: `live version`, `radio edit`, `extended mix`, `instrumental version` etc.

---

## 🖼 Cover Art

Click **# COVERS** in the dashboard to fetch missing album art.

- Groups tracks by folder — one fetch per album
- MusicBrainz Cover Art Archive first, iTunes as fallback
- Embeds into MP3 (ID3), FLAC, and M4A/AAC
- Saves `cover.jpg` alongside tracks for Navidrome/Symfonium

---

## ↩️ Undo

- Full history of every moved file
- Restore individually or all at once from the Undo page
- `/duplicates` mirrors your original library structure

---

## 📦 Supported Formats

MP3, FLAC, AAC (M4A), OGG, Opus, WMA, WAV, AIFF, APE, WavPack

---

## 🛣 Roadmap

- [ ] Confidence-based auto-clean (HIGH confidence only)
- [ ] CLI / headless mode
- [ ] Smart keep rules (user-defined format and folder preferences)
- [ ] Webhook support
- [ ] Non-root container user

---

## 🧠 Notes

- Start with `USE_FINGERPRINT=false` for speed — metadata matching catches the vast majority of duplicates
- Enable fingerprinting for deeper cleanup on untagged or mis-tagged collections
- Large libraries (50k+ tracks) benefit from staged runs: apply HIGH confidence groups first, then rescan

---

## 📜 License

MIT — do whatever you want with it.
