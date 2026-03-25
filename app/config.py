"""
Duparr configuration
All values can be overridden via environment variables.
"""

import os

# ── Directories ───────────────────────────────────────────────────────────────
MUSIC_DIR = os.getenv("MUSIC_DIR",   "/music")
DUP_DIR   = os.getenv("DUP_DIR",     "/duplicates")
DB_PATH   = os.getenv("DB_PATH",     "/data/music.db")

# Comma-separated list of directory paths to skip during scanning.
# Useful for excluding torrent staging folders or symlink targets inside MUSIC_DIR.
# Example: EXCLUDE_DIRS=/music/staging,/music/tmp
EXCLUDE_DIRS = [
    d.strip() for d in os.getenv("EXCLUDE_DIRS", "").split(",") if d.strip()
]

# ── Fingerprinting ────────────────────────────────────────────────────────────
FPCALC_PATH     = os.getenv("FPCALC_PATH",     "/usr/bin/fpcalc")
USE_FINGERPRINT = os.getenv("USE_FINGERPRINT", "true").lower() == "true"
FPCALC_TIMEOUT  = int(os.getenv("FPCALC_TIMEOUT", "60"))  # seconds; increase for large FLACs on slow NAS

# ── Duplicate detection ───────────────────────────────────────────────────────
# Minimum score gap required before a dupe is moved (prevents moving near-equal files)
# Set to 0 to move all duplicates regardless of quality difference
MIN_SCORE_GAP = int(os.getenv("MIN_SCORE_GAP", "10"))

# Score penalty for tracks found inside a _collections folder
COLLECTIONS_PENALTY = int(os.getenv("COLLECTIONS_PENALTY", "100"))

# ── Cover fetching ────────────────────────────────────────────────────────────
# Sources tried in order: musicbrainz, itunes, lastfm
COVER_SOURCES   = os.getenv("COVER_SOURCES",   "musicbrainz,itunes").split(",")
LASTFM_API_KEY  = os.getenv("LASTFM_API_KEY",  "")   # optional
COVER_MIN_SIZE  = int(os.getenv("COVER_MIN_SIZE", "300"))  # minimum pixel dimension
COVER_EMBED     = os.getenv("COVER_EMBED",     "true").lower() == "true"
COVER_SAVE_FILE = os.getenv("COVER_SAVE_FILE", "true").lower() == "true"

# ── Navidrome integration ─────────────────────────────────────────────────────
NAVIDROME_URL      = os.getenv("NAVIDROME_URL",      "")  # e.g. http://navidrome:4533
NAVIDROME_USER     = os.getenv("NAVIDROME_USER",     "")
NAVIDROME_PASSWORD = os.getenv("NAVIDROME_PASSWORD", "")

# ── Supported audio formats ───────────────────────────────────────────────────
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".opus",
    ".wma", ".aac", ".wav", ".aiff"
}
