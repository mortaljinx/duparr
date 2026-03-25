"""
CoverFixer — fetches and embeds missing album artwork.
Sources: MusicBrainz Cover Art Archive (primary), iTunes (fallback).
Embeds into audio file + saves cover.jpg alongside.
"""

import os
import io
import time
import hashlib
import requests
from typing import List, Dict, Optional
from collections import defaultdict

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, error as ID3Error
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen import File as MutagenFile

import config
from db import Database

HEADERS = {"User-Agent": "MusicCleaner/1.0 (self-hosted; github.com/yourname/music-cleaner)"}

class CoverFixer:
    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def find_missing(self) -> List[Dict]:
        """
        Return list of unique albums (by folder) missing cover art.
        Groups tracks by their parent directory.
        """
        tracks = self.db.tracks_missing_covers()
        # Group by directory — one fetch per album folder
        by_dir = defaultdict(list)
        for t in tracks:
            folder = os.path.dirname(t["path"])
            by_dir[folder].append(dict(t))

        albums = []
        for folder, tracks_in_folder in by_dir.items():
            # Use the most common artist/album tags in this folder
            artists = [t["artist"] for t in tracks_in_folder if t["artist"]]
            albums_tags = [t["album"] for t in tracks_in_folder if t["album"]]
            albums.append({
                "folder": folder,
                "artist": _most_common(artists) or "Unknown Artist",
                "album":  _most_common(albums_tags) or os.path.basename(folder),
                "tracks": tracks_in_folder,
            })

        return albums

    def fix_all(self, albums: List[Dict]):
        total = len(albums)
        fixed = 0
        for i, album in enumerate(albums, 1):
            print(f"  [{i}/{total}] {album['artist']} — {album['album']}")
            img_data = self._fetch_cover(album["artist"], album["album"])
            if img_data:
                self._apply_cover(album, img_data)
                fixed += 1
                print(f"    ✅ Cover applied")
            else:
                print(f"    ❌ No cover found")
            # Be polite to APIs
            time.sleep(1)
        print(f"\n✅ Applied covers to {fixed}/{total} albums")

    def _fetch_cover(self, artist: str, album: str) -> Optional[bytes]:
        for source in config.COVER_SOURCES:
            fn = getattr(self, f"_from_{source}", None)
            if fn:
                data = fn(artist, album)
                if data and len(data) > 1024:  # sanity check
                    return data
        return None

    def _from_musicbrainz(self, artist: str, album: str) -> Optional[bytes]:
        try:
            # Step 1: search MusicBrainz for release
            r = self.session.get(
                "https://musicbrainz.org/ws/2/release",
                params={
                    "query": f'artist:"{artist}" AND release:"{album}"',
                    "fmt": "json",
                    "limit": 5,
                },
                timeout=10,
            )
            r.raise_for_status()
            releases = r.json().get("releases", [])
            if not releases:
                return None

            # Step 2: try Cover Art Archive for each release
            for release in releases:
                mbid = release.get("id")
                if not mbid:
                    continue
                try:
                    art = self.session.get(
                        f"https://coverartarchive.org/release/{mbid}/front",
                        timeout=10, allow_redirects=True,
                    )
                    if art.status_code == 200 and art.content:
                        w, h = _estimate_image_size(art.content)
                        if w >= config.COVER_MIN_SIZE:
                            return art.content
                except Exception:
                    continue
        except Exception as e:
            print(f"    ⚠️  MusicBrainz error: {e}")
        return None

    def _from_itunes(self, artist: str, album: str) -> Optional[bytes]:
        try:
            r = self.session.get(
                "https://itunes.apple.com/search",
                params={
                    "term": f"{artist} {album}",
                    "media": "music",
                    "entity": "album",
                    "limit": 3,
                },
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            for result in results:
                art_url = result.get("artworkUrl100", "")
                if art_url:
                    # Upgrade to 600px version
                    art_url = art_url.replace("100x100bb", "600x600bb")
                    img = self.session.get(art_url, timeout=10)
                    if img.status_code == 200:
                        return img.content
        except Exception as e:
            print(f"    ⚠️  iTunes error: {e}")
        return None

    def _from_lastfm(self, artist: str, album: str) -> Optional[bytes]:
        if not config.LASTFM_API_KEY:
            return None
        try:
            r = self.session.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method": "album.getinfo",
                    "artist": artist,
                    "album": album,
                    "api_key": config.LASTFM_API_KEY,
                    "format": "json",
                },
                timeout=10,
            )
            r.raise_for_status()
            images = r.json().get("album", {}).get("image", [])
            # Last.fm returns sizes: small, medium, large, extralarge, mega
            for img in reversed(images):
                url = img.get("#text", "")
                if url and "2a96cbd8b46e442fc41c2b86b821562f" not in url:  # skip placeholder
                    data = self.session.get(url, timeout=10)
                    if data.status_code == 200:
                        return data.content
        except Exception as e:
            print(f"    ⚠️  Last.fm error: {e}")
        return None

    def _apply_cover(self, album: Dict, img_data: bytes):
        # Save cover.jpg to folder
        if config.COVER_SAVE_FILE:
            cover_path = os.path.join(album["folder"], "cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(img_data)

        # Embed into each track
        if config.COVER_EMBED:
            for track in album["tracks"]:
                try:
                    self._embed(track["path"], img_data)
                    self.db.update_cover_status(track["path"], True)
                except Exception as e:
                    print(f"    ⚠️  Embed failed for {track['path']}: {e}")

    def _embed(self, path: str, img_data: bytes):
        ext = os.path.splitext(path)[1].lower()

        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3Error:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(
                encoding=3, mime="image/jpeg", type=3,
                desc="Cover", data=img_data
            ))
            tags.save(path)

        elif ext == ".flac":
            f = FLAC(path)
            f.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = img_data
            f.add_picture(pic)
            f.save()

        elif ext in (".m4a", ".mp4", ".aac"):
            f = MP4(path)
            f.tags["covr"] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
            f.save()

        elif ext in (".ogg", ".opus"):
            import base64
            from mutagen.oggvorbis import OggVorbis
            from mutagen.flac import Picture
            f = OggVorbis(path)
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = img_data
            encoded = base64.b64encode(pic.write()).decode("ascii")
            f["metadata_block_picture"] = [encoded]
            f.save()

    def trigger_navidrome_scan(self):
        if not config.NAVIDROME_URL:
            return
        try:
            url = f"{config.NAVIDROME_URL}/rest/startScan"
            params = {
                "u": config.NAVIDROME_USER,
                "p": config.NAVIDROME_PASSWORD,
                "v": "1.16.1",
                "c": "music-cleaner",
                "f": "json",
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            print("✅ Navidrome scan triggered")
        except Exception as e:
            print(f"⚠️  Could not trigger Navidrome scan: {e}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _most_common(lst: list) -> Optional[str]:
    if not lst:
        return None
    return max(set(lst), key=lst.count)

def _estimate_image_size(data: bytes) -> tuple[int, int]:
    """Quick JPEG/PNG dimension check without PIL."""
    try:
        import struct
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        elif data[:2] == b'\xff\xd8':  # JPEG
            i = 2
            while i < len(data):
                if data[i] != 0xFF:
                    break
                marker = data[i+1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i+5:i+9])
                    return w, h
                length = struct.unpack(">H", data[i+2:i+4])[0]
                i += 2 + length
    except Exception:
        pass
    return (9999, 9999)  # assume ok if we can't parse
