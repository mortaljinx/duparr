"""
Tagger — unified tag fixer.

Detects and fixes two types of tag problems in one scan:

1. COLLECTION — album tag contains collection path junk:
     "1989 - GREATEST HITS • Roy Orbison - 40 Greatest Hits Of Roy Orbison [EU Vinyl 3LP]"
   Fix: extract real artist + album, write Artist / Album Artist / Album tags.

2. COMPILATION — folder has multiple track artists but missing/wrong Album Artist:
     Various tracks by different artists, Album Artist = "" or inconsistent
   Fix: write Album Artist = "Various Artists" (or dominant artist if single-artist album).

Results carry a 'type' field ('COLLECTION' or 'COMPILATION') so the UI can show
a badge. Both types go through the same scan → review → fix workflow.
"""

import os
import re
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TPE1, TPE2, TALB
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

try:
    from mutagen.oggvorbis import OggVorbis
    _HAS_OGG = True
except ImportError:
    _HAS_OGG = False

try:
    from mutagen.oggopus import OggOpus
    _HAS_OPUS = True
except ImportError:
    _HAS_OPUS = False

import config
from db import Database

VARIOUS_ARTISTS = "Various Artists"

VA_VARIANTS = {
    # English
    "va", "v.a.", "v/a", "various", "various artists",
    "various artist", "variousartists",
    # Spanish
    "varios artistas", "varios",
    # Dutch
    "varioussze artisten", "verschillende artiesten", "diverse artiesten",
    "artiesten", "verschillende",
    # German
    "verschiedene interpreten", "verschiedene künstler", "verschiedene",
    "künstler", "interpreten",
    # French
    "artistes variés", "artistes varies", "divers artistes",
    # Italian
    "artisti vari", "vari artisti",
    # Generic / malformed
    "(va)", "va.", "aa.vv.", "aa. vv.",
}

UNKNOWN_ARTIST = re.compile(r"^unknown\s+artist$|^$", re.IGNORECASE)

# Spam tokens embedded by dodgy download sites
SPAM_TOKENS = re.compile(
    r"lanzamientosmp3|zippyshare|320kbps?\.co|mp3skull|"
    r"kat\.ph|thepiratebay|kickass|musicpleer|mp3\.vc",
    re.IGNORECASE,
)

# ── Collection tag detection patterns ─────────────────────────────────────────

BULLET_PATTERN = re.compile(
    r"^\d{4}\s*[-–]\s*(?:GREATEST HITS|BEST OF|THE BEST OF)[^•·]*[•·]\s*"
    r"(.+?)\s*-\s*(.+?)(?:\s*\[|\s*$)",
    re.IGNORECASE,
)

_JUNK_BRACKETS   = re.compile(r"\s*\[[^\]]{1,60}\]")
_JUNK_YEAR       = re.compile(r"\s*\(\d{4}\)")
_JUNK_DISC       = re.compile(r"\s*/\s*(?:CD|Disc|Disk|Side)\s*\d*\s*$", re.IGNORECASE)
_JUNK_PREFIX     = re.compile(r"^\d{4}\s*[-–]\s*(?:GREATEST HITS|BEST OF)[^•·]*[•·]\s*", re.IGNORECASE)
_JUNK_UNDERSCORE = re.compile(r"\s*_[^_]+_\s*")
_JUNK_FORMAT     = re.compile(r"\b(?:MP3|FLAC|320\s*kbps|Part\s+\d+\s+Of\s+\d+)\b", re.IGNORECASE)
_JUNK_TL         = re.compile(r"\s*-?\s*TL\s*$")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise_va(val: str) -> bool:
    return val.strip().lower() in VA_VARIANTS


def _normalise_artist(val: str) -> str:
    """Normalise artist name for comparison — strip trailing dots, collapse spaces."""
    return re.sub(r"\s+", " ", val.strip().rstrip(".")).lower()


def _first(val) -> str:
    if not val:
        return ""
    if isinstance(val, list):
        return str(val[0]).strip() if val else ""
    return str(val).strip()


def _read_tags(path: str) -> Dict:
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return {}
        return {
            "artist":       _first(audio.get("artist")),
            "album":        _first(audio.get("album")),
            "album_artist": _first(audio.get("albumartist")),
        }
    except Exception:
        return {}


def _clean_album(album: str) -> str:
    a = album
    a = _JUNK_BRACKETS.sub("", a)
    a = _JUNK_YEAR.sub("", a)
    a = _JUNK_DISC.sub("", a)
    a = _JUNK_PREFIX.sub("", a)
    a = _JUNK_UNDERSCORE.sub(" ", a)
    a = _JUNK_FORMAT.sub("", a)
    a = _JUNK_TL.sub("", a)
    return re.sub(r"\s+", " ", a).strip(" -–•·")


def _extract_from_bullet(album: str) -> Optional[Tuple[str, str]]:
    """Extract (artist, album_title) from bullet-pattern collection tag."""
    m = BULLET_PATTERN.match(album)
    if not m:
        return None
    artist = m.group(1).strip()
    title  = _clean_album(m.group(2).strip())
    if len(artist) < 2 or len(title) < 2:
        return None
    if UNKNOWN_ARTIST.match(artist):
        return None
    return artist, title


def _find_dominant_artist(artists: List[str]) -> Optional[str]:
    """
    Find a single artist that appears in ALL track artist strings.
    Handles "Pitbull feat. Ne-Yo", "Pitbull & Shakira" etc.
    Returns None if no single dominant artist found.
    """
    if not artists:
        return None

    candidates = {}
    for a in artists:
        # Split on feat./ft./&/with/vs./x
        parts = re.split(r"\s+(?:feat\.?|ft\.?|featuring|&|with|vs\.?|x)\s+", a, flags=re.IGNORECASE)
        for p in parts:
            p = p.strip()
            if p and not SPAM_TOKENS.search(p):
                candidates[p.lower()] = p

    for name_lower, name in candidates.items():
        if all(name_lower in a.lower() for a in artists):
            return name

    return None


def _write_tags(path: str, album_artist: str,
                artist: str = None, album: str = None) -> bool:
    """
    Write tags to a file. album_artist is always written.
    artist and album are optional — only written if provided.
    """
    try:
        ext = os.path.splitext(path)[1].lower()

        if ext == ".mp3":
            try:
                tags = ID3(path)
            except Exception:
                tags = ID3()
            tags["TPE2"] = TPE2(encoding=3, text=album_artist)
            if artist is not None:
                tags["TPE1"] = TPE1(encoding=3, text=artist)
            if album is not None:
                tags["TALB"] = TALB(encoding=3, text=album)
            tags.save(path)

        elif ext == ".flac":
            audio = FLAC(path)
            audio["albumartist"] = [album_artist]
            if artist is not None:
                audio["artist"] = [artist]
            if album is not None:
                audio["album"] = [album]
            audio.save()

        elif ext in (".m4a", ".aac", ".mp4"):
            audio = MP4(path)
            audio["aART"] = [album_artist]
            if artist is not None:
                audio["\xa9ART"] = [artist]
            if album is not None:
                audio["\xa9alb"] = [album]
            audio.save()

        elif ext == ".ogg":
            audio = OggVorbis(path) if _HAS_OGG else MutagenFile(path, easy=True)
            if audio is None:
                return False
            audio["albumartist"] = [album_artist]
            if artist is not None:
                audio["artist"] = [artist]
            if album is not None:
                audio["album"] = [album]
            audio.save()

        elif ext == ".opus":
            audio = OggOpus(path) if _HAS_OPUS else MutagenFile(path, easy=True)
            if audio is None:
                return False
            audio["albumartist"] = [album_artist]
            if artist is not None:
                audio["artist"] = [artist]
            if album is not None:
                audio["album"] = [album]
            audio.save()

        else:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                return False
            audio["albumartist"] = [album_artist]
            if artist is not None:
                audio["artist"] = [artist]
            if album is not None:
                audio["album"] = [album]
            audio.save()

        return True
    except Exception as e:
        print(f"  Could not write tags to {path}: {e}")
        return False


# ── Main class ────────────────────────────────────────────────────────────────

class Tagger:
    def __init__(self, db: Database, log_fn=None):
        self.db     = db
        self.log_fn = log_fn or print

    def _log(self, msg: str):
        self.log_fn(msg)

    def find_all(self) -> List[Dict]:
        """
        Unified scan — returns both COLLECTION and COMPILATION issues.
        Each result has a 'type' field: 'COLLECTION' or 'COMPILATION'.
        """
        tracks = self.db.all_tracks()

        by_folder = defaultdict(list)
        for t in tracks:
            by_folder[os.path.dirname(t["path"])].append(dict(t))

        results   = []
        total     = len(by_folder)
        checked   = 0

        for folder, folder_tracks in by_folder.items():
            checked += 1
            if checked % 500 == 0 or checked == total:
                self._log(f"  Scanned {checked}/{total} folders...")

            if len(folder_tracks) < 2:
                continue

            # Read fresh tags from disk
            track_tags = []
            for t in folder_tracks:
                if not os.path.exists(t["path"]):
                    continue
                tags = _read_tags(t["path"])
                if tags:
                    tags["path"] = t["path"]
                    track_tags.append(tags)

            if len(track_tags) < 2:
                continue

            albums = {t["album"] for t in track_tags if t["album"]}
            if not albums:
                continue

            album         = max(albums, key=list(albums).count) if len(albums) > 1 else albums.pop()
            artists       = [t["artist"] for t in track_tags if t["artist"]]
            unique_artists= {a for a in artists if not SPAM_TOKENS.search(a)}
            album_artists = {t["album_artist"] for t in track_tags if t["album_artist"]}

            # ── Check 1: Collection junk ───────────────────────────────────
            dominant_artist = max(set(artists), key=artists.count) if artists else ""
            extracted = _extract_from_bullet(album)
            if extracted:
                new_artist, new_album = extracted
                results.append({
                    "type":        "COLLECTION",
                    "folder":      folder,
                    "album":       album,
                    "artist":      dominant_artist,
                    "album_artists": sorted(album_artists),
                    "new_artist":  new_artist,
                    "new_album":   new_album,
                    "correct_aa":  new_artist,
                    "confidence":  "HIGH",
                    "track_count": len(track_tags),
                    "tracks":      [t["path"] for t in track_tags],
                    "artists":     sorted(unique_artists),
                })
                continue  # don't double-detect

            # Also catch plain album junk (no bullet) but only if artist unknown
            if UNKNOWN_ARTIST.match(dominant_artist):
                cleaned = _clean_album(album)
                if cleaned and cleaned != album and len(cleaned) >= 3:
                    results.append({
                        "type":        "COLLECTION",
                        "folder":      folder,
                        "album":       album,
                        "artist":      dominant_artist,
                        "album_artists": sorted(album_artists),
                        "new_artist":  "",
                        "new_album":   cleaned,
                        "correct_aa":  VARIOUS_ARTISTS,
                        "confidence":  "MEDIUM",
                        "track_count": len(track_tags),
                        "tracks":      [t["path"] for t in track_tags],
                        "artists":     sorted(unique_artists),
                    })
                    continue

            # ── Check 2: Compilation / fragmented Album Artist ─────────────
            if len(unique_artists) <= 1:
                continue  # Single-artist album — skip

            missing    = sum(1 for t in track_tags if not t["album_artist"])
            already_va = sum(1 for t in track_tags if _normalise_va(t["album_artist"]))

            if already_va == len(track_tags):
                continue  # Already correctly tagged

            # Determine correct Album Artist
            dominant = _find_dominant_artist(artists)
            correct_aa = dominant if dominant else VARIOUS_ARTISTS

            # Skip if all tracks already have the correct album artist
            if dominant:
                dom_norm = _normalise_artist(dominant)
                already_correct = sum(
                    1 for t in track_tags
                    if _normalise_artist(t["album_artist"]) == dom_norm
                )
                if already_correct == len(track_tags):
                    continue  # Already correctly tagged — nothing to do

            # Skip if Album Artist is already consistent (all tracks have the same
            # non-empty, non-VA value). Handles: DJ compilations, "Zombies, The" vs
            # "The Zombies", Pitbull feat. credits appearing as lead on some tracks, etc.
            aa_values = {t["album_artist"].strip() for t in track_tags if t["album_artist"].strip()}
            if len(aa_values) == 1:
                existing_aa = next(iter(aa_values))
                if existing_aa and not _normalise_va(existing_aa):
                    # One consistent non-VA album artist already set — trust it
                    continue

            if missing == len(track_tags) or len(album_artists) > 1:
                confidence = "HIGH"
            elif missing > 0 or already_va > 0:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"

            results.append({
                "type":         "COMPILATION",
                "folder":       folder,
                "album":        album,
                "artist":       dominant_artist,
                "album_artists": sorted(album_artists),
                "new_artist":   "",          # compilations don't change Artist tag
                "new_album":    album,       # album tag stays the same
                "correct_aa":   correct_aa,
                "confidence":   confidence,
                "missing_count": missing,
                "track_count":  len(track_tags),
                "tracks":       [t["path"] for t in track_tags],
                "artists":      sorted(unique_artists),
            })

        order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        results.sort(key=lambda x: (order.get(x["confidence"], 2), x["type"], x["folder"]))

        collection_count  = sum(1 for r in results if r["type"] == "COLLECTION")
        compilation_count = sum(1 for r in results if r["type"] == "COMPILATION")
        self._log(f"  Found {len(results)} folders needing fixes "
                  f"({collection_count} collection, {compilation_count} compilation)")
        return results

    # keep find_fragmented as alias for backward compat
    def find_fragmented(self) -> List[Dict]:
        return self.find_all()

    def fix_folder(self, folder: str, tracks: List[str],
                   fix_type: str = "COMPILATION",
                   correct_aa: str = None,
                   new_artist: str = None,
                   new_album: str = None) -> Dict:
        """
        Fix tags for a folder.
        - COLLECTION: writes Artist, Album Artist, and Album tags
        - COMPILATION: writes only Album Artist tag
        """
        fixed = failed = skipped = 0

        for path in tracks:
            if not os.path.exists(path):
                skipped += 1
                continue

            current = _read_tags(path)
            orig_aa = current.get("album_artist", "")

            if fix_type == "COLLECTION":
                # Write all three tags
                aa = correct_aa or new_artist or VARIOUS_ARTISTS
                write_artist = new_artist if new_artist else None
                write_album  = new_album  if new_album  else None

                self.db.log_tag_fix(path, orig_aa)
                if _write_tags(path, aa, artist=write_artist, album=write_album):
                    fixed += 1
                    self._log(f"  ✅ {os.path.basename(path)}")
                else:
                    failed += 1
                    self.db.remove_tag_fix(path)
                    self._log(f"  ❌ {os.path.basename(path)}")

            else:  # COMPILATION
                aa = correct_aa or VARIOUS_ARTISTS
                if _normalise_va(orig_aa) and orig_aa == VARIOUS_ARTISTS:
                    skipped += 1
                    continue

                self.db.log_tag_fix(path, orig_aa)
                if _write_tags(path, aa):
                    fixed += 1
                    self._log(f"  ✅ {os.path.basename(path)}")
                else:
                    failed += 1
                    self.db.remove_tag_fix(path)
                    self._log(f"  ❌ {os.path.basename(path)}")

        self._log(f"  Done — fixed {fixed}, skipped {skipped}, failed {failed}")
        return {"fixed": fixed, "failed": failed, "skipped": skipped}

    def undo_folder(self, folder: str) -> Dict:
        """Restore original Album Artist tags for all tracks in a folder."""
        fixes    = self.db.get_tag_fixes_for_folder(folder)
        restored = failed = 0

        for fix in fixes:
            path        = fix["path"]
            original_aa = fix["original_album_artist"]

            if not os.path.exists(path):
                failed += 1
                continue

            if _write_tags(path, original_aa):
                self.db.restore_tag_fix(path)
                restored += 1
                self._log(f"  ↩️  Restored: {os.path.basename(path)}")
            else:
                failed += 1
                self._log(f"  ❌ Failed: {os.path.basename(path)}")

        self._log(f"  ↩️  Done — restored {restored}, failed {failed}")
        return {"restored": restored, "failed": failed}
