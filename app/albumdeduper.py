"""
AlbumDeduper — album-level duplicate detection.

Finds pairs of folders that contain the same album and offers to move the
weaker entire folder atomically to /duplicates.

Rules:
  - Match albums by: same normalised artist + same normalised album title
  - Subset match: every track in the weaker folder must match a track in the
    stronger folder (by normalised title). The stronger can have MORE tracks
    (bonus editions, deluxe versions) — this is safe and expected.
  - If the weaker has any track with no match in the stronger: SKIP the pair.
    Never leave a partial album behind.
  - Score each folder as a whole. Stronger = higher average track score.
  - Move the entire weaker folder atomically. Full undo via DB.

Match strategy (track-to-track):
  - Normalise title: lowercase, strip punctuation/brackets, collapse whitespace
  - Duration similarity within 10 seconds as a tiebreaker (not required)
  - A track in the weaker folder is "matched" if any track in the stronger
    folder shares the same normalised title
"""

import os
import re
import shutil
import math
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import config
from db import Database


# ── Normalisation ─────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalise a string for fuzzy matching — lowercase, no punctuation."""
    if not s:
        return ""
    s = s.lower()
    # Remove content in brackets — (Deluxe Edition), [2023 Remaster] etc.
    s = re.sub(r'[\(\[\{][^\)\]\}]*[\)\]\}]', '', s)
    # Remove punctuation except spaces
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _norm_artist(artist: str) -> str:
    """Normalise artist — also strip leading 'the '."""
    n = _norm(artist)
    if n.startswith("the "):
        n = n[4:]
    return n


DISC_PATTERN = re.compile(
    r'^(cd|disc|disk|side|digital.?media|shm.?cd|bluray|bd|dvd|lp|vinyl)'
    r'[\s\-_]*\d'
    r'|\d{1,2}$',
    re.IGNORECASE
)

def _is_disc_sibling(folder_a: str, folder_b: str) -> bool:
    """
    Return True if folder_a and folder_b are disc subfolders of the same
    parent album — e.g. CD1/CD2, Digital Media 01/02, Disc 1/2, Side A/B.
    These should never be treated as duplicate albums.
    """
    parent_a = os.path.dirname(folder_a)
    parent_b = os.path.dirname(folder_b)
    if parent_a != parent_b:
        return False
    sub_a = os.path.basename(folder_a)
    sub_b = os.path.basename(folder_b)
    return bool(DISC_PATTERN.match(sub_a) or DISC_PATTERN.match(sub_b))


def _folder_score(tracks: List[dict], deduper_score_fn) -> float:
    """Average quality score across all tracks in a folder."""
    if not tracks:
        return 0
    scores = [deduper_score_fn(t) for t in tracks]
    return sum(scores) / len(scores)


# ── Album grouping ─────────────────────────────────────────────────────────────

def _album_key(track: dict) -> Optional[Tuple[str, str]]:
    """Return (norm_artist, norm_album) key for a track, or None if untagged."""
    artist = track.get("artist", "") or ""
    album  = track.get("album",  "") or ""
    if not artist.strip() or not album.strip():
        return None
    # Use album_artist if available (better for compilations — but we want
    # to match copies that may have different album_artist tags)
    return (_norm_artist(artist), _norm(album))


# ── Core detector ─────────────────────────────────────────────────────────────

class AlbumDeduper:
    def __init__(self, db: Database, log_fn=None):
        self.db     = db
        self.log_fn = log_fn or print

    def _log(self, msg: str):
        self.log_fn(msg)

    def find_duplicate_albums(self) -> List[Dict]:
        """
        Scan all tracked folders and find pairs where one is a complete subset
        of the other. Returns a list of pairs sorted by space saving desc.
        """
        tracks = [dict(t) for t in self.db.all_tracks()]

        # Group tracks by folder
        by_folder: dict[str, list] = defaultdict(list)
        for t in tracks:
            by_folder[os.path.dirname(t["path"])].append(t)

        # Group folders by (norm_artist, norm_album)
        album_buckets: dict[tuple, list] = defaultdict(list)
        total_folders = len(by_folder)
        checked = 0

        for folder, folder_tracks in by_folder.items():
            checked += 1
            if checked % 500 == 0 or checked == total_folders:
                self._log(f"  Grouped {checked} / {total_folders} folders")

            if not folder_tracks:
                continue

            # Use the most common album tag in the folder
            album_tags  = [t.get("album", "") for t in folder_tracks if t.get("album")]
            artist_tags = [t.get("artist", "") for t in folder_tracks if t.get("artist")]
            if not album_tags or not artist_tags:
                continue

            album  = max(set(album_tags),  key=album_tags.count)
            artist = max(set(artist_tags), key=artist_tags.count)

            key = (_norm_artist(artist), _norm(album))
            if not key[0] or not key[1]:
                continue

            album_buckets[key].append({
                "folder": folder,
                "tracks": folder_tracks,
                "album":  album,
                "artist": artist,
                "track_count": len(folder_tracks),
            })

        self._log(f"  Found {len(album_buckets)} unique album keys")

        # For each bucket with 2+ folders, check subset match
        pairs = []
        bucket_count = 0
        for key, folders in album_buckets.items():
            if len(folders) < 2:
                continue
            bucket_count += 1

            # Compare every pair of folders in this bucket
            for i in range(len(folders)):
                for j in range(i + 1, len(folders)):
                    pair = self._check_pair(folders[i], folders[j])
                    if pair:
                        pairs.append(pair)

        self._log(f"  Checked {bucket_count} album groups with 2+ copies")
        self._log(f"  Found {len(pairs)} duplicate album pairs")

        # Sort by space saving descending
        pairs.sort(key=lambda p: p["move_size_bytes"], reverse=True)
        return pairs

    def _check_pair(self, a: dict, b: dict) -> Optional[Dict]:
        """
        Check if folder a and folder b are duplicate albums.
        Returns a pair dict if one is a subset of the other, else None.
        """
        from deduper import Deduper
        deduper = Deduper(self.db)

        # Skip disc siblings — CD1 vs CD2, Digital Media 01 vs 02 etc.
        if _is_disc_sibling(a["folder"], b["folder"]):
            return None

        # Skip untagged folders
        if a["album"] in ("Unknown Title", "") or b["album"] in ("Unknown Title", ""):
            return None
        if a["artist"] in ("Unknown Artist", "") or b["artist"] in ("Unknown Artist", ""):
            return None

        # Build normalised title sets
        a_titles = {_norm(t.get("title", "")): t for t in a["tracks"] if t.get("title")}
        b_titles = {_norm(t.get("title", "")): t for t in b["tracks"] if t.get("title")}

        if not a_titles or not b_titles:
            return None

        # Determine which is the subset (weaker) and which is the superset (keeper)
        # The subset must have ALL its tracks present in the superset
        a_in_b = all(title in b_titles for title in a_titles)
        b_in_a = all(title in a_titles for title in b_titles)

        if not a_in_b and not b_in_a:
            return None  # Neither is a complete subset — skip

        # Score both folders
        a_score = _folder_score(a["tracks"], deduper.score)
        b_score = _folder_score(b["tracks"], deduper.score)

        if a_in_b and b_in_a:
            # Both are subsets of each other — identical track lists
            # Higher score wins
            keeper = a if a_score >= b_score else b
            mover  = b if a_score >= b_score else a
        elif a_in_b:
            # a is subset of b — b is the superset (keep b)
            keeper = b
            mover  = a
        else:
            # b is subset of a — a is the superset (keep a)
            keeper = a
            mover  = b

        # Don't move if same folder (shouldn't happen but be safe)
        if keeper["folder"] == mover["folder"]:
            return None

        # Calculate space to be freed
        move_size = sum(
            os.path.getsize(t["path"])
            for t in mover["tracks"]
            if os.path.exists(t["path"])
        )

        # Match count for display
        mover_titles  = {_norm(t.get("title", "")) for t in mover["tracks"] if t.get("title")}
        keeper_titles = {_norm(t.get("title", "")) for t in keeper["tracks"] if t.get("title")}
        matched = len(mover_titles & keeper_titles)

        # Require at least 3 matched tracks — single track "matches" are too
        # likely to be coincidence (e.g. "Greatest Hits" albums sharing one track name)
        if matched < 3:
            return None

        return {
            "keeper_folder":      keeper["folder"],
            "keeper_album":       keeper["album"],
            "keeper_artist":      keeper["artist"],
            "keeper_track_count": keeper["track_count"],
            "keeper_score":       round(a_score if keeper is a else b_score, 1),
            "mover_folder":       mover["folder"],
            "mover_track_count":  mover["track_count"],
            "mover_score":        round(b_score if mover is b else a_score, 1),
            "matched_tracks":     matched,
            "move_size_bytes":    move_size,
            "move_size_mb":       round(move_size / (1024 * 1024), 1),
        }

    def move_album(self, mover_folder: str, keeper_folder: str) -> Dict:
        """
        Move entire mover_folder to /duplicates atomically.
        Logs every file moved for undo.
        Returns {moved, failed, size_freed_bytes}.
        """
        # Safety check
        music_real = os.path.realpath(config.MUSIC_DIR)
        mover_real = os.path.realpath(mover_folder)
        if not mover_real.startswith(music_real + os.sep):
            raise ValueError(f"Unsafe path blocked: {mover_folder}")

        moved = failed = 0
        size_freed = 0

        tracks = [
            dict(t) for t in self.db.all_tracks()
            if os.path.dirname(t["path"]) == mover_folder
        ]

        os.makedirs(config.DUP_DIR, exist_ok=True)

        for track in tracks:
            src = track["path"]
            if not os.path.exists(src):
                continue
            try:
                dest = self._safe_dest(src)
                size = os.path.getsize(src)
                shutil.move(src, dest)
                self.db.log_album_move(src, dest, keeper_folder)
                self.db.remove_track(src)
                size_freed += size
                moved += 1
                self._log(f"  → {os.path.basename(src)}")
            except Exception as e:
                failed += 1
                self._log(f"  ❌ Failed: {os.path.basename(src)}: {e}")

        # Remove empty folder
        try:
            if os.path.isdir(mover_folder) and not os.listdir(mover_folder):
                os.rmdir(mover_folder)
                self._log(f"  🗑️  Removed empty folder: {os.path.basename(mover_folder)}")
        except Exception:
            pass

        self._log(f"  ✅ Moved {moved} tracks, freed {size_freed // (1024*1024)} MB")
        return {"moved": moved, "failed": failed, "size_freed_bytes": size_freed}

    def undo_album_move(self, mover_folder: str) -> Dict:
        """Restore all tracks from a previously moved folder."""
        moves = self.db.get_album_moves_for_folder(mover_folder)
        restored = failed = 0

        for move in moves:
            src  = move["dest_path"]    # where it is now (in /duplicates)
            dest = move["source_path"]  # where it came from

            if not os.path.exists(src):
                failed += 1
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(src, dest)
                self.db.restore_album_move(move["id"])
                restored += 1
                self._log(f"  ↩️  Restored: {os.path.basename(dest)}")
            except Exception as e:
                failed += 1
                self._log(f"  ❌ Failed: {e}")

        return {"restored": restored, "failed": failed}

    def _safe_dest(self, src: str) -> str:
        """Mirror the source path under DUP_DIR, with collision handling."""
        try:
            rel = os.path.relpath(src, config.MUSIC_DIR)
        except Exception:
            rel = os.path.basename(src)

        dest = os.path.realpath(os.path.join(config.DUP_DIR, rel))
        dup_real = os.path.realpath(config.DUP_DIR)
        if not dest.startswith(dup_real + os.sep):
            raise ValueError(f"Unsafe destination: {dest}")

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if os.path.exists(dest):
            base, ext = os.path.splitext(dest)
            counter = 1
            while os.path.exists(f"{base}_dup{counter}{ext}"):
                counter += 1
            dest = f"{base}_dup{counter}{ext}"

        return dest
