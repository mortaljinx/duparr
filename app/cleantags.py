"""
CleanTags — fixes malformed tags in bulk-downloaded music collections.

Detects folders where the Album tag contains structured junk like:
  "1989 - GREATEST HITS • Roy Orbison - 40 Greatest Hits Of Roy Orbison [EU Vinyl 3LP]"

And rewrites them to proper tags:
  Artist:       Roy Orbison
  Album Artist: Roy Orbison
  Album:        40 Greatest Hits Of Roy Orbison

Also handles plain messy album names:
  "Greatest Hits [EU Vinyl 2LP]"  →  "Greatest Hits"
  "Kamikaze (2018)"                →  "Kamikaze"
  "Horizontal (1968)/CD 01"        →  "Horizontal"

Only rewrites tags where:
  - The album tag contains detectable junk AND
  - The cleaned version is meaningfully different AND
  - At least medium confidence the extraction is correct

Never touches files that already have clean tags.
"""

import os
import re
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TPE1, TPE2, TALB
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

import config
from db import Database


# ── Detection patterns ────────────────────────────────────────────────────────

# Bullet-separated collection pattern:
# "YYYY - GREATEST HITS • Artist Name - Album Title [junk]"
BULLET_PATTERN = re.compile(
    r'^\d{4}\s*[-–]\s*(?:GREATEST HITS|BEST OF|THE BEST OF)[^•·]*[•·]\s*'
    r'(.+?)\s*-\s*(.+?)(?:\s*\[|\s*$)',
    re.IGNORECASE
)

# Junk suffixes to strip from album names
JUNK_BRACKETS = re.compile(r'\s*\[[^\]]{1,60}\]')
JUNK_YEAR_PAREN = re.compile(r'\s*\(\d{4}\)')
JUNK_DISC_SUFFIX = re.compile(r'\s*/\s*(?:CD|Disc|Disk|Side)\s*\d*\s*$', re.IGNORECASE)
JUNK_COLLECTION_PREFIX = re.compile(
    r'^\d{4}\s*[-–]\s*(?:GREATEST HITS|BEST OF)[^•·]*[•·]\s*', re.IGNORECASE
)
JUNK_UNDERSCORE = re.compile(r'\s*_[^_]+_\s*')
JUNK_FORMAT = re.compile(r'\b(?:MP3|FLAC|320\s*kbps|Part\s+\d+\s+Of\s+\d+)\b', re.IGNORECASE)
JUNK_TL_SUFFIX = re.compile(r'\s*-?\s*TL\s*$')

UNKNOWN_ARTIST = re.compile(r'^unknown\s+artist$|^$', re.IGNORECASE)


def _clean_album(album: str) -> str:
    """Strip junk from an album name."""
    a = album
    a = JUNK_BRACKETS.sub('', a)
    a = JUNK_YEAR_PAREN.sub('', a)
    a = JUNK_DISC_SUFFIX.sub('', a)
    a = JUNK_COLLECTION_PREFIX.sub('', a)
    a = JUNK_UNDERSCORE.sub(' ', a)
    a = JUNK_FORMAT.sub('', a)
    a = JUNK_TL_SUFFIX.sub('', a)
    a = re.sub(r'\s+', ' ', a).strip(' -–•·')
    return a


def _extract_from_bullet(album: str) -> Optional[Tuple[str, str]]:
    """
    Try to extract (artist, album_title) from a bullet-pattern album tag.
    Returns None if pattern not matched.
    """
    m = BULLET_PATTERN.match(album)
    if not m:
        return None
    artist = m.group(1).strip()
    title  = _clean_album(m.group(2).strip())
    if len(artist) < 2 or len(title) < 2:
        return None
    return artist, title


def _analyse_folder(folder_tracks: List[dict]) -> Optional[Dict]:
    """
    Analyse a folder and return a fix proposal if tags need cleaning.
    Returns None if tags are already clean.
    """
    # Read fresh tags from disk
    track_tags = []
    for t in folder_tracks:
        if not os.path.exists(t['path']):
            continue
        try:
            audio = MutagenFile(t['path'], easy=True)
            if audio is None:
                continue
            def first(key):
                v = audio.get(key, [])
                return str(v[0]).strip() if v else ''
            track_tags.append({
                'path':         t['path'],
                'artist':       first('artist'),
                'album':        first('album'),
                'albumartist':  first('albumartist'),
            })
        except Exception:
            continue

    if not track_tags:
        return None

    # Get most common album tag
    albums = [t['album'] for t in track_tags if t['album']]
    if not albums:
        return None
    album = max(set(albums), key=albums.count)

    # Get most common artist
    artists = [t['artist'] for t in track_tags if t['artist']]
    artist  = max(set(artists), key=artists.count) if artists else ''

    # Try bullet extraction first (highest confidence)
    extracted = _extract_from_bullet(album)
    if extracted:
        new_artist, new_album = extracted
        # Validate — extracted artist shouldn't be the same junk
        if not UNKNOWN_ARTIST.match(new_artist):
            return {
                'folder':      folder,
                'album':       album,
                'artist':      artist,
                'new_artist':  new_artist,
                'new_album':   new_album,
                'confidence':  'HIGH',
                'method':      'bullet_extract',
                'tracks':      [t['path'] for t in track_tags],
                'track_count': len(track_tags),
            }

    # Try plain album junk stripping (medium confidence)
    cleaned_album = _clean_album(album)
    if cleaned_album and cleaned_album != album and len(cleaned_album) >= 3:
        # Only fix artist if it's unknown
        new_artist = artist if not UNKNOWN_ARTIST.match(artist) else ''
        return {
            'folder':      folder,
            'album':       album,
            'artist':      artist,
            'new_artist':  new_artist,
            'new_album':   cleaned_album,
            'confidence':  'MEDIUM',
            'method':      'album_clean',
            'tracks':      [t['path'] for t in track_tags],
            'track_count': len(track_tags),
        }

    return None


def _write_tags(path: str, new_artist: str, new_album: str, fix_artist: bool) -> bool:
    """Write cleaned artist and album tags to a file."""
    try:
        ext = os.path.splitext(path)[1].lower()

        if ext == '.mp3':
            try:
                tags = ID3(path)
            except Exception:
                tags = ID3()
            tags['TALB'] = TALB(encoding=3, text=new_album)
            if fix_artist and new_artist:
                tags['TPE1'] = TPE1(encoding=3, text=new_artist)
                tags['TPE2'] = TPE2(encoding=3, text=new_artist)
            tags.save(path)

        elif ext == '.flac':
            audio = FLAC(path)
            audio['album'] = [new_album]
            if fix_artist and new_artist:
                audio['artist']      = [new_artist]
                audio['albumartist'] = [new_artist]
            audio.save()

        elif ext in ('.m4a', '.aac', '.mp4'):
            audio = MP4(path)
            audio['\xa9alb'] = [new_album]
            if fix_artist and new_artist:
                audio['\xa9ART'] = [new_artist]
                audio['aART']    = [new_artist]
            audio.save()

        else:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                return False
            audio['album'] = [new_album]
            if fix_artist and new_artist:
                audio['artist']      = [new_artist]
                audio['albumartist'] = [new_artist]
            audio.save()

        return True
    except Exception as e:
        print(f"  Could not write tags to {path}: {e}")
        return False


class CleanTags:
    def __init__(self, db: Database, log_fn=None):
        self.db     = db
        self.log_fn = log_fn or print

    def _log(self, msg: str):
        self.log_fn(msg)

    def find_dirty(self) -> List[Dict]:
        """Scan all tracked folders for albums needing tag cleanup."""
        tracks = self.db.all_tracks()

        by_folder = defaultdict(list)
        for t in tracks:
            by_folder[os.path.dirname(t['path'])].append(dict(t))

        results = []
        total   = len(by_folder)
        checked = 0

        for folder_path, folder_tracks in by_folder.items():
            checked += 1
            if checked % 500 == 0 or checked == total:
                self._log(f"  Scanned {checked} / {total} folders")

            if len(folder_tracks) < 1:
                continue

            # Read fresh tags and analyse
            track_tags = []
            for t in folder_tracks:
                if not os.path.exists(t['path']):
                    continue
                try:
                    audio = MutagenFile(t['path'], easy=True)
                    if audio is None:
                        continue
                    def first(key):
                        v = audio.get(key, [])
                        return str(v[0]).strip() if v else ''
                    track_tags.append({
                        'path':        t['path'],
                        'artist':      first('artist'),
                        'album':       first('album'),
                        'albumartist': first('albumartist'),
                    })
                except Exception:
                    continue

            if not track_tags:
                continue

            albums  = [t['album'] for t in track_tags if t['album']]
            artists = [t['artist'] for t in track_tags if t['artist']]
            if not albums:
                continue

            album  = max(set(albums),  key=albums.count)
            artist = max(set(artists), key=artists.count) if artists else ''

            # Try bullet extraction
            extracted = _extract_from_bullet(album)
            if extracted:
                new_artist, new_album = extracted
                if not UNKNOWN_ARTIST.match(new_artist) and new_album:
                    results.append({
                        'folder':      folder_path,
                        'album':       album,
                        'artist':      artist,
                        'new_artist':  new_artist,
                        'new_album':   new_album,
                        'confidence':  'HIGH',
                        'method':      'bullet_extract',
                        'tracks':      [t['path'] for t in track_tags],
                        'track_count': len(track_tags),
                    })
                    continue

            # Try plain album cleaning
            cleaned = _clean_album(album)
            if cleaned and cleaned != album and len(cleaned) >= 3:
                new_artist = '' if UNKNOWN_ARTIST.match(artist) else artist
                results.append({
                    'folder':      folder_path,
                    'album':       album,
                    'artist':      artist,
                    'new_artist':  new_artist,
                    'new_album':   cleaned,
                    'confidence':  'MEDIUM',
                    'method':      'album_clean',
                    'tracks':      [t['path'] for t in track_tags],
                    'track_count': len(track_tags),
                })

        order = {'HIGH': 0, 'MEDIUM': 1}
        results.sort(key=lambda x: (order.get(x['confidence'], 2), x['folder']))
        self._log(f"  Found {len(results)} folders needing tag cleanup")
        return results

    def fix_folder(self, folder: str, tracks: List[str],
                   new_artist: str, new_album: str) -> Dict:
        """
        Write cleaned Artist, Album Artist, and Album tags to all tracks.
        Returns {fixed, failed, skipped}.
        """
        fixed = failed = skipped = 0

        # Only write artist if it's a real value (not empty)
        fix_artist = bool(new_artist and new_artist.strip())

        self._log(f"  Album  → \"{new_album}\"")
        if fix_artist:
            self._log(f"  Artist → \"{new_artist}\"")

        for path in tracks:
            if not os.path.exists(path):
                skipped += 1
                continue

            if _write_tags(path, new_artist, new_album, fix_artist):
                fixed += 1
                self._log(f"  OK: {os.path.basename(path)}")
            else:
                failed += 1
                self._log(f"  FAIL: {os.path.basename(path)}")

        self._log(f"  Done — fixed {fixed}, skipped {skipped}, failed {failed}")
        return {'fixed': fixed, 'failed': failed, 'skipped': skipped}
