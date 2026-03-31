"""
CoverFixer — fetches and embeds missing album artwork.
Sources: MusicBrainz Cover Art Archive, Deezer, iTunes, Last.fm (tried in order).
Embeds into audio file + saves cover.jpg alongside.
"""

import os
import re
import time
import struct
import requests
from typing import List, Dict, Optional
from collections import defaultdict

from mutagen.id3 import ID3, APIC, error as ID3Error
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

import config
from db import Database

HEADERS = {"User-Agent": "Duparr/1.1 (self-hosted; github.com/mortaljinx/Duparr)"}


def _clean_search_query(artist: str, album: str):
    a  = album
    ar = artist.strip()

    if re.match(r'^unknown\s+artist$|^$', ar, re.IGNORECASE):
        bullet = re.search(r'[•·]\s*(.+?)\s*-\s*(.+?)(?:\s*\[|$)', a)
        if bullet:
            ar = bullet.group(1).strip()
            a  = bullet.group(2).strip()

    a = re.sub(r'\s*\[[^\]]{1,60}\]', '', a)
    a = re.sub(r'\s*\(\d{4}\)', '', a)
    a = re.sub(r'\s*/\s*(?:CD|Disc|Disk|Side)\s*\d*\s*$', '', a, flags=re.IGNORECASE)
    a = re.sub(r'^\d{4}\s*[-\u2013]\s*(?:GREATEST HITS|BEST OF)[^•·]*[•·]\s*', '', a, flags=re.IGNORECASE)
    a = re.sub(r'\s*_[^_]+_\s*', ' ', a)
    a = re.sub(r'\b(?:MP3|FLAC|320\s*kbps|Part\s+\d+\s+Of\s+\d+)\b', '', a, flags=re.IGNORECASE)
    a = re.sub(r'\s*-?\s*TL\s*$', '', a)
    a = re.sub(r'\s+', ' ', a).strip(' -\u2013\u2022\xb7')

    if re.match(r'^various(\s+artists?)?$|^va$|^v\.?a\.?$|^unknown\s+artist$|^$', ar, re.IGNORECASE):
        ar = ''

    return ar.strip(), a.strip()


class CoverFixer:
    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def find_missing(self) -> List[Dict]:
        tracks = self.db.tracks_missing_covers()
        by_dir = defaultdict(list)
        for t in tracks:
            folder = os.path.dirname(t["path"])
            by_dir[folder].append(dict(t))

        albums = []
        for folder, tracks_in_folder in by_dir.items():
            artists    = [t["artist"] for t in tracks_in_folder if t["artist"]]
            album_tags = [t["album"]  for t in tracks_in_folder if t["album"]]
            albums.append({
                "folder": folder,
                "artist": _most_common(artists)    or "Unknown Artist",
                "album":  _most_common(album_tags) or os.path.basename(folder),
                "tracks": tracks_in_folder,
            })
        return albums

    def fix_all(self, albums: List[Dict]):
        total = len(albums)
        fixed = 0
        for i, album in enumerate(albums, 1):
            artist, alb = album["artist"], album["album"]
            print(f"  [{i}/{total}] {artist} -- {alb}")
            img_data = self._fetch_cover(artist, alb)
            if img_data:
                self._apply_cover(album, img_data)
                fixed += 1
                print(f"    >> Applied ({len(img_data)//1024}KB)")
            else:
                print(f"    >> No cover found")
            time.sleep(0.5)
        print(f"\n  Applied {fixed}/{total}")

    def _fetch_cover(self, artist: str, album: str) -> Optional[bytes]:
        clean_artist, clean_album = _clean_search_query(artist, album)
        if not clean_album:
            clean_album = album

        cleaned = (clean_artist != artist or clean_album != album)
        qa = clean_artist or artist
        qb = clean_album  or album
        if cleaned:
            print(f"    Cleaned query: {qa!r} / {qb!r}")
        else:
            print(f"    Query: {qa!r} / {qb!r}")

        for source in config.COVER_SOURCES:
            fn = getattr(self, f"_from_{source}", None)
            if not fn:
                print(f"      [{source}] no handler")
                continue
            print(f"      [{source}] trying...")
            data = fn(clean_artist, clean_album)
            if data and len(data) > 1024:
                print(f"      [{source}] HIT {len(data)//1024}KB")
                return data
            if cleaned:
                print(f"      [{source}] miss -- retrying with original tags...")
                data = fn(artist, album)
                if data and len(data) > 1024:
                    print(f"      [{source}] HIT {len(data)//1024}KB (original)")
                    return data
                print(f"      [{source}] miss (original)")
            else:
                print(f"      [{source}] miss")
        return None

    def _from_musicbrainz(self, artist: str, album: str) -> Optional[bytes]:
        try:
            q = ('artist:"%s" AND release:"%s"' % (artist, album)) if artist else ('release:"%s"' % album)
            r = self.session.get(
                "https://musicbrainz.org/ws/2/release",
                params={"query": q, "fmt": "json", "limit": 5},
                timeout=10,
            )
            if r.status_code != 200:
                print(f"        MB search HTTP {r.status_code}: {r.text[:120]}")
                return None
            releases = r.json().get("releases", [])
            print(f"        MB: {len(releases)} release(s)")
            for release in releases:
                mbid  = release.get("id")
                title = release.get("title", "?")
                score = release.get("score", "?")
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
                            print(f"        CAA {title!r} score={score}: {w}x{h}")
                            return art.content
                        print(f"        CAA {title!r}: too small {w}px (min {config.COVER_MIN_SIZE})")
                    else:
                        print(f"        CAA {title!r} ({mbid}): HTTP {art.status_code}")
                except Exception as e:
                    print(f"        CAA {title!r} ({mbid}): {e}")
        except Exception as e:
            print(f"        MB exception: {e}")
        return None

    def _from_deezer(self, artist: str, album: str) -> Optional[bytes]:
        try:
            q = ("%s %s" % (artist, album)).strip()
            r = self.session.get(
                "https://api.deezer.com/search/album",
                params={"q": q, "limit": 5},
                timeout=10,
            )
            if r.status_code != 200:
                print(f"        Deezer HTTP {r.status_code}: {r.text[:120]}")
                return None
            results = r.json().get("data", [])
            print(f"        Deezer: {len(results)} result(s)")
            for result in results:
                title     = result.get("title", "?")
                cover_url = (result.get("cover_xl") or result.get("cover_big")
                             or result.get("cover_medium"))
                if not cover_url:
                    print(f"        Deezer {title!r}: no cover URL")
                    continue
                img = self.session.get(cover_url, timeout=10)
                if img.status_code == 200 and img.content:
                    w, h = _estimate_image_size(img.content)
                    if w >= config.COVER_MIN_SIZE:
                        print(f"        Deezer {title!r}: {w}x{h}")
                        return img.content
                    print(f"        Deezer {title!r}: too small {w}px")
                else:
                    print(f"        Deezer {title!r}: image HTTP {img.status_code}")
        except Exception as e:
            print(f"        Deezer exception: {e}")
        return None

    def _from_itunes(self, artist: str, album: str) -> Optional[bytes]:
        try:
            term = ("%s %s" % (artist, album)).strip()
            r = self.session.get(
                "https://itunes.apple.com/search",
                params={"term": term, "media": "music", "entity": "album", "limit": 3},
                timeout=10,
            )
            if r.status_code != 200:
                print(f"        iTunes HTTP {r.status_code}: {r.text[:120]}")
                return None
            results = r.json().get("results", [])
            print(f"        iTunes: {len(results)} result(s)")
            for result in results:
                name    = result.get("collectionName", "?")
                art_url = result.get("artworkUrl100", "")
                if not art_url:
                    print(f"        iTunes {name!r}: no artwork URL")
                    continue
                art_url = art_url.replace("100x100bb", "600x600bb")
                img = self.session.get(art_url, timeout=10)
                if img.status_code == 200 and img.content:
                    print(f"        iTunes {name!r}: OK")
                    return img.content
                print(f"        iTunes {name!r}: image HTTP {img.status_code}")
        except Exception as e:
            print(f"        iTunes exception: {e}")
        return None

    def _from_lastfm(self, artist: str, album: str) -> Optional[bytes]:
        if not config.LASTFM_API_KEY:
            print(f"        Last.fm: no API key (set LASTFM_API_KEY)")
            return None
        try:
            r = self.session.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method":  "album.getinfo",
                    "artist":  artist,
                    "album":   album,
                    "api_key": config.LASTFM_API_KEY,
                    "format":  "json",
                },
                timeout=10,
            )
            if r.status_code != 200:
                print(f"        Last.fm HTTP {r.status_code}")
                return None
            images = r.json().get("album", {}).get("image", [])
            print(f"        Last.fm: {len(images)} image(s)")
            for img in reversed(images):
                url = img.get("#text", "")
                if url and "2a96cbd8b46e442fc41c2b86b821562f" not in url:
                    data = self.session.get(url, timeout=10)
                    if data.status_code == 200:
                        return data.content
                    print(f"        Last.fm image HTTP {data.status_code}")
        except Exception as e:
            print(f"        Last.fm exception: {e}")
        return None

    def _apply_cover(self, album: Dict, img_data: bytes):
        if config.COVER_SAVE_FILE:
            cover_path = os.path.join(album["folder"], "cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(img_data)

        if config.COVER_EMBED:
            for track in album["tracks"]:
                try:
                    self._embed(track["path"], img_data)
                    self.db.update_cover_status(track["path"], True)
                except Exception as e:
                    print(f"    Embed failed {track['path']}: {e}")

    def _embed(self, path: str, img_data: bytes):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3Error:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img_data))
            tags.save(path)
        elif ext == ".flac":
            f = FLAC(path)
            f.clear_pictures()
            pic = Picture()
            pic.type = 3; pic.mime = "image/jpeg"; pic.desc = "Cover"; pic.data = img_data
            f.add_picture(pic)
            f.save()
        elif ext in (".m4a", ".mp4", ".aac"):
            f = MP4(path)
            f.tags["covr"] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
            f.save()
        elif ext in (".ogg", ".opus"):
            import base64
            from mutagen.oggvorbis import OggVorbis
            f = OggVorbis(path)
            pic = Picture()
            pic.type = 3; pic.mime = "image/jpeg"; pic.desc = "Cover"; pic.data = img_data
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            f.save()

    def trigger_navidrome_scan(self):
        if not config.NAVIDROME_URL:
            return
        try:
            requests.get(
                f"{config.NAVIDROME_URL}/rest/startScan",
                params={"u": config.NAVIDROME_USER, "p": config.NAVIDROME_PASSWORD,
                        "v": "1.16.1", "c": "duparr", "f": "json"},
                timeout=10,
            ).raise_for_status()
            print("Navidrome scan triggered")
        except Exception as e:
            print(f"Navidrome scan failed: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _most_common(lst: list) -> Optional[str]:
    if not lst:
        return None
    return max(set(lst), key=lst.count)


def _estimate_image_size(data: bytes) -> tuple:
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        elif data[:2] == b'\xff\xd8':
            i = 2
            while i < len(data):
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + length
    except Exception:
        pass
    return (9999, 9999)
