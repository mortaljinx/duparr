"""
Scanner — walks the music directory and extracts metadata into the DB.
Incremental: skips files that haven't changed since last scan (mtime check).
Handles MP3 (ID3), FLAC (Vorbis comments), M4A/AAC (iTunes atoms) correctly.
followlinks=False: symlinked albums are visible to Navidrome but the scanner
never follows them into their target directory (e.g. torrent folders).
"""

import os
import subprocess
import json
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

import config
from db import Database


class Scanner:
    def __init__(self, db: Database):
        self.db = db

    def scan(self) -> int:
        count = 0
        skipped = 0
        fp_errors = 0

        # Remove stale DB entries for files no longer on disk
        removed = self.db.cleanup_missing()
        if removed:
            print(f"  🧹 Removed {removed} stale DB entries")

        # Count total audio files upfront for progress reporting
        def _excluded(path: str) -> bool:
            """Return True if path is at or under an excluded directory."""
            return any(
                path == ex or path.startswith(ex + os.sep)
                for ex in config.EXCLUDE_DIRS
            )

        total_files = sum(
            1 for root, dirs, files in os.walk(config.MUSIC_DIR, followlinks=False)
            if not _excluded(root)
            for f in files if os.path.splitext(f)[1].lower() in config.AUDIO_EXTENSIONS
        )
        print(f"  📂 Total files to check: {total_files}")

        for root, dirs, files in os.walk(config.MUSIC_DIR, followlinks=False):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            # Skip excluded directories (full subtree, exact-boundary match)
            if config.EXCLUDE_DIRS:
                dirs[:] = [
                    d for d in dirs
                    if not _excluded(os.path.join(root, d))
                ]
                if _excluded(root):
                    continue

            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in config.AUDIO_EXTENSIONS:
                    continue

                path = os.path.join(root, fname)

                # ── Incremental check ─────────────────────────────────────
                # Skip if file hasn't changed since last scan
                try:
                    current_mtime = os.path.getmtime(path)
                except OSError:
                    continue

                stored_mtime = self.db.get_track_mtime(path)
                if stored_mtime is not None and current_mtime <= stored_mtime:
                    skipped += 1
                    continue
                # ─────────────────────────────────────────────────────────

                info = self._extract(path)
                if not info:
                    continue

                info["mtime"] = current_mtime

                fingerprint = None
                if config.USE_FINGERPRINT:
                    fingerprint = self._fingerprint(path)
                    if fingerprint is None:
                        fp_errors += 1

                info["fingerprint"] = fingerprint
                self.db.upsert_track(info)
                count += 1

                if count % 100 == 0:
                    print(f"  Scanned {count} new/changed tracks... ({skipped} unchanged skipped)")

        print(f"  ✅ Scanned {count} new/changed tracks, {skipped} unchanged skipped")

        if config.USE_FINGERPRINT and fp_errors:
            print(f"  ⚠️  {fp_errors} tracks could not be fingerprinted")

        return count

    def _extract(self, path: str) -> dict | None:
        try:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                return None

            ext = os.path.splitext(path)[1].lower()
            tags = audio.tags or {}

            def tag(key, fallback=""):
                val = tags.get(key)
                if val:
                    return str(val[0]).strip() if isinstance(val, list) else str(val).strip()
                return fallback

            tracknum_raw = tag("tracknumber") or tag("track")
            try:
                track_num = int(tracknum_raw.split("/")[0])
            except (ValueError, AttributeError):
                track_num = 0

            bitrate = 0
            duration = 0.0
            if hasattr(audio, "info"):
                bitrate = getattr(audio.info, "bitrate", 0) // 1000
                duration = getattr(audio.info, "length", 0.0)

            has_cover = self._has_embedded_cover(path, ext, audio) or self._has_folder_art(path)

            return {
                "path":      path,
                "artist":    tag("artist"),
                "album":     tag("album"),
                "title":     tag("title") or os.path.splitext(os.path.basename(path))[0],
                "track_num": track_num,
                "duration":  round(duration, 2),
                "bitrate":   bitrate,
                "format":    ext.lstrip("."),
                "filesize":  os.path.getsize(path),
                "has_cover": 1 if has_cover else 0,
                "mtime":     0,  # set by caller
            }
        except Exception as e:
            print(f"  ⚠️  Could not read {path}: {e}")
            return None

    def _has_embedded_cover(self, path: str, ext: str, audio) -> bool:
        try:
            if ext == ".mp3":
                from mutagen.id3 import ID3
                return any(k.startswith("APIC") for k in ID3(path).keys())
            elif ext == ".flac":
                return len(FLAC(path).pictures) > 0
            elif ext in (".m4a", ".mp4", ".aac"):
                f = MP4(path)
                return f.tags is not None and "covr" in f.tags
            elif ext in (".ogg", ".opus"):
                return "metadata_block_picture" in (audio.tags or {})
        except Exception:
            pass
        return False

    def _has_folder_art(self, path: str) -> bool:
        folder = os.path.dirname(path)
        for name in ["cover.jpg", "cover.png", "folder.jpg", "folder.png",
                     "front.jpg", "front.png", "album.jpg", "album.png"]:
            if os.path.exists(os.path.join(folder, name)):
                return True
        return False

    def _fingerprint(self, path: str) -> str | None:
        """Run fpcalc and return the audio fingerprint string, or None on failure."""
        try:
            result = subprocess.run(
                [config.FPCALC_PATH, "-json", path],
                capture_output=True,
                text=True,
                timeout=config.FPCALC_TIMEOUT
            )
            if result.returncode == 0:
                return json.loads(result.stdout).get("fingerprint")
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass
        return None
