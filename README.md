<div align="center">

# Duparr

**Enforce a clean music library: one artist, one song, one copy.**

Duparr is a self-hosted music deduplication engine that scans your library, groups duplicate tracks using metadata and optional audio fingerprinting, scores them by quality, and lets you safely review and move them — nothing is ever deleted.

![GitHub release](https://img.shields.io/github/v/release/mortaljinx/duparr?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square)
![Docker](https://img.shields.io/badge/docker-self--hosted-blue?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12-blue?style=flat-square)
![SQLite](https://img.shields.io/badge/database-sqlite-lightblue?style=flat-square)
![Status](https://img.shields.io/badge/status-stable-green?style=flat-square)

</div>

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
- 🏷 **Tag fixer** — detects and repairs two tag problem types: collection junk in Album tags, and missing/wrong Album Artist on compilations
- 💿 **Album-level dedup** — finds entire duplicate album folders and moves the weaker copy atomically
- 🧩 **Same-album safety guard** — tracks in the same folder require fingerprint confirmation, preventing false positives on CD1/CD2, vinyl sides, and box sets
- 🔍 **Explainable decisions** — every duplicate shows match type, confidence level, and score difference
- ⚡ **Incremental scanning** — after the first run, only new or changed files are processed
- 🌐 **Web UI** — review groups, apply changes, undo moves, and monitor progress live
- ↩️ **Full undo** — restore any moved file or album folder to its original location
- 🖼 **Cover art fetcher** — automatically finds and embeds missing album art from MusicBrainz, Deezer, iTunes, and Last.fm
- 🔐 **Optional auth** — basic auth via env var, off by default for trusted LAN use

---

## 🧭 The Pipeline

Duparr enforces a structured workflow. Each step unlocks the next, and state persists across container restarts.

```
① SCAN  →  ② FIX TAGS  →  ③ ALBUM DUPES  →  ④ RESCAN  →  ⑤ FIND DUPES  →  ⑥ APPLY
```

| Step | Name | Description | Skippable? |
|---|---|---|---|
| **1** | **Scan Library** | Build or update the track index. Incremental on repeat runs. | No |
| **2** | **Fix Tags** | Detect and fix Album Artist / Album tag problems. | Yes |
| **3** | **Album Dupes** | Find and move duplicate album folders. | Yes |
| **4** | **Rescan** | Refresh index after album moves. | Required if step 3 ran |
| **5** | **Find Dupes** | Detect duplicate tracks across the library. | No |
| **6** | **Apply** | Move duplicate tracks to `/duplicates`. | No |

Steps can be skipped from the UI if not needed for your library.

---

## 🔍 How Duplicate Detection Works

**Pass 1 — Chromaprint fingerprint** (when enabled): exact audio match regardless of format or tags. Catches the same recording ripped as both FLAC and MP3.

**Pass 2 — Identity match**: `(primary_artist, clean_title)` — same song, possibly different quality. Same-folder tracks are excluded from this pass; they require fingerprint confirmation to avoid false positives on multi-take albums, vinyl sides, and disc sets.

Variants (`(Remix)`, `[Live]`, `(Acoustic)` etc.) are split out of every group and never moved.

---

## 📊 Confidence Levels

| Level | Meaning |
|---|---|
| **HIGH** | Fingerprint match, or metadata match with strong score difference |
| **MEDIUM** | Metadata match with clear quality winner |
| **LOW** | Small score gap — manual review recommended |

Start with HIGH confidence groups for safe, high-value wins.

---

## ⚖️ Scoring System

| Condition | Points |
|---|---|
| FLAC format | +100 |
| AAC / OGG / Opus | +50 |
| MP3 | +30 |
| Bitrate (proportional, capped at 50) | +0–50 |
| Complete tags (artist + title + album) | +10 |
| Has embedded or folder artwork | +5 |
| Filesize tiebreaker | +0–10 |
| `_collections` folder | −100 |
| Compilation / greatest hits album | −40 |

---

## 🏷 Tag Fixer

The tag fixer detects two types of problem in a single scan:

**COLLECTION** — Album tag contains structured junk from bulk downloads:
```
"1989 - GREATEST HITS • Roy Orbison - 40 Greatest Hits Of Roy Orbison [EU Vinyl 3LP]"
```
Extracts the real artist and album title, writes Artist / Album Artist / Album tags.

**COMPILATION** — Multi-artist folder with missing or wrong Album Artist tag. Detects the dominant artist (e.g. `Pitbull feat. X` → Album Artist = `Pitbull`) or falls back to `Various Artists`.

Smart false-positive avoidance: albums where all tracks already agree on the same Album Artist are skipped — including DJ compilations tagged with the curator's name. Multilingual "Various Artists" variants are recognised (English, Dutch, German, French, Italian).

---

## 💿 Album Deduplication

Finds pairs of folders containing the same album and moves the weaker copy atomically:

- Matches by normalised artist + album title
- Subset matching: every track in the weaker folder must match a track in the stronger one — never leaves a partial album behind
- Disc siblings (CD1/CD2, Digital Media 01/02, Side A/B) are never treated as duplicates
- Minimum 3 matched tracks required to avoid coincidental matches on greatest hits albums
- Full undo support — restore an entire moved album folder from the UI

---

## 🛡 Safety First

- Never deletes files — everything lands in `/duplicates`
- Same-folder tracks require fingerprint confirmation before being flagged
- Variants are always preserved
- Score gap (`MIN_SCORE_GAP`) prevents near-equal files from being moved
- Path traversal is blocked on all move operations
- Full undo for every operation — track-level and album-level

---

## 🚀 Quick Start

### 1. Clone

```bash
git clone https://github.com/mortaljinx/duparr.git
cd duparr
```

### 2. Configure `docker-compose.yml`

Edit the volume paths to match your setup:

```yaml
volumes:
  - /your/music:/music-rw
  - /your/duplicates:/duplicates
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

Follow the pipeline steps in order. Start with **① SCAN LIBRARY**, then work through each step.

---

## ⚙️ Configuration

All options are set via environment variables in `docker-compose.yml`.

| Variable | Default | Description |
|---|---|---|
| `MUSIC_DIR` | `/music-rw` | Music library path inside the container |
| `DUP_DIR` | `/duplicates` | Where duplicates are moved |
| `DB_PATH` | `/data/music.db` | SQLite database path |
| `USE_FINGERPRINT` | `false` | Enable Chromaprint audio fingerprinting (slower but more accurate) |
| `FPCALC_PATH` | `/usr/bin/fpcalc` | Path to fpcalc binary |
| `FPCALC_TIMEOUT` | `60` | Seconds before fpcalc times out per file — increase for large FLACs on slow NAS |
| `MIN_SCORE_GAP` | `0` | Minimum quality gap before flagging a duplicate. Raise to `10` to skip near-equal files |
| `COLLECTIONS_PENALTY` | `100` | Score penalty for tracks inside a `_collections` folder |
| `EXCLUDE_DIRS` | _(empty)_ | Comma-separated directory paths to skip during scanning |
| `COVER_SOURCES` | `musicbrainz,deezer,itunes` | Cover art sources in priority order |
| `COVER_MIN_SIZE` | `300` | Minimum cover dimension in pixels |
| `COVER_EMBED` | `true` | Embed cover art into audio files |
| `COVER_SAVE_FILE` | `true` | Save `cover.jpg` alongside tracks |
| `DUPARR_PASSWORD` | _(empty)_ | Enable basic auth — leave empty to disable |
| `NAVIDROME_URL` | _(empty)_ | Auto-trigger Navidrome rescan after Apply (e.g. `http://navidrome:4533`) |
| `NAVIDROME_USER` | _(empty)_ | Navidrome username |
| `NAVIDROME_PASSWORD` | _(empty)_ | Navidrome password |
| `LASTFM_API_KEY` | _(empty)_ | Last.fm API key — enables Last.fm as a cover art fallback source |

---

## 🔐 Authentication

Auth is **off by default** — suitable for trusted LAN use behind your router.

To enable, add to your `docker-compose.yml`:

```yaml
environment:
  DUPARR_PASSWORD: "your-password-here"
```

The browser will prompt for a password on every session. The `/health` endpoint is always public for Docker healthchecks.

---

## 🎧 Variant Detection

Variants are always preserved — they appear in the UI as **VAR.** and are never moved.

Detected via:

- **Bracket keywords** in title: `(Remix)`, `[Live]`, `(Acoustic)`, `(Demo)`, `(Instrumental)`, `(Remaster)` etc.
- **Multi-word bracket phrases**: `(Radio Edit)`, `(Extended Mix)`, `(Club Version)`, `(12" Mix)` etc.
- **Dash-suffix patterns**: `Song - Live`, `Song - Acoustic Mix`, `Song - Remastered`
- **Path phrases**: `live version`, `radio edit`, `extended mix`, `instrumental version` etc.

Single-word path matching (`mix`, `edit`, `version`) is intentionally avoided — these appear in `Original Mix`, `Album Version`, `Single Version`, which are canonical originals.

---

## 🖼 Cover Art

Click **# COVERS** in the dashboard to fetch missing album art.

- Groups tracks by folder — one fetch per album
- Sources tried in order: MusicBrainz Cover Art Archive → Deezer → iTunes → Last.fm (if API key set)
- Cleans search queries automatically — strips junk from album names before searching
- Falls back to original (uncleaned) query if the cleaned version returns nothing
- Embeds into MP3 (ID3), FLAC (Vorbis), M4A/AAC (iTunes atoms), OGG, Opus
- Saves `cover.jpg` alongside tracks for Navidrome / Symfonium compatibility

---

## ↩️ Undo

- Full history of every moved file and album folder
- Restore individual tracks or entire albums from the Undo page
- Restore everything at once with **RESTORE ALL**
- `/duplicates` mirrors your original library structure — easy to navigate manually too

---

## 📦 Supported Formats

MP3, FLAC, M4A/AAC, OGG Vorbis, Opus, WMA, WAV, AIFF

---

## 🧠 Tips

- **Start with `USE_FINGERPRINT=false`** — metadata matching catches the vast majority of duplicates and is much faster
- **Enable fingerprinting** for deeper cleanup on untagged or mis-tagged collections, or to catch cross-format dupes (same recording as FLAC and MP3)
- **Large libraries (50k+ tracks)**: apply HIGH confidence groups first, then rescan — staged runs are faster and safer
- **Symlinked folders**: the scanner uses `followlinks=False` — symlinks in your music directory are visible to Navidrome but the scanner never follows them into their target. Use `EXCLUDE_DIRS` to skip any directories you want ignored entirely
- **After album moves**: always run Step 4 (Rescan) before Step 5 (Find Dupes) — the index needs to reflect what actually moved

---

## 🛣 Roadmap

- [ ] Confidence-based auto-clean (HIGH confidence only, no review needed)
- [ ] CLI / headless mode
- [ ] Smart keep rules (user-defined format and folder preferences)
- [ ] Webhook support
- [ ] Non-root container user

---

## 📜 License

MIT — see [LICENSE](LICENSE).
