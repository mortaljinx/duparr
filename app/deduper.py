"""
Duparr Deduper — v1.0

Rule: one artist, one song — unless it's a variant (remix, live, acoustic, etc.)

Duplicate detection in two passes:
  1. Chromaprint audio fingerprint (primary — catches cross-format dupes)
  2. (primary_artist, clean_title) identity match (fallback)
     Variants are split out of every group and always preserved.

Key design decisions:
  - Variants detected by bracket content in title only (bracket-first)
    e.g. "Bad (Remix)" → variant, "Bad" → original
    Single-word path matching is intentionally avoided — words like "mix",
    "live", "edit" appear in too many legitimate folder names.
  - Same-folder tracks require fingerprint confirmation, not metadata match
  - Score caching: score() computed once per track
  - Artist primary key: order-preserved first token from tags
"""

import os
import shutil
import re
import json
import math
from collections import defaultdict

import config


BRACKET_VARIANT_KEYWORDS = {
    # These unambiguously mean a non-original version
    "remix", "live", "demo", "acoustic", "instrumental",
    "backing", "karaoke", "mono", "stereo",
    "remaster", "remastered",
    "feat", "ft", "featuring",
}

# Multi-word bracket phrases that are also unambiguous variants
# Single words like "mix", "edit", "version" are intentionally excluded —
# they appear in "Original Mix", "Album Version", "Single Version" which
# are canonical originals, not variants.
BRACKET_VARIANT_PHRASES = {
    "radio edit", "radio mix", "radio version",
    "extended mix", "extended version", "extended edit",
    "club mix", "club edit", "club version",
    "12 inch", "12inch", "12\" mix",
    "alternate mix", "alternate version", "alt mix",
    "explicit version", "clean version",
    "bonus track", "bonus version",
}

COMPILATION_KEYWORDS = [
    "greatest hits",
    "best of",
    "the best of",
    "very best of",
    "the essential",
    "past, present and future",
    "past present and future",
    "number ones",
    "definitive collection",
    "complete collection",
    "the collection",
    "ultimate collection",
    "the anthology",
    "platinum collection",
    "gold collection",
]

COMPILATION_PENALTY = 40

# Maximum groups exported to UI — keeps browser responsive at scale
MAX_UI_GROUPS = 500


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_generic_title(title: str) -> bool:
    t = (title or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^(various artists?|va)\s*[-:]\s*", "", t)
    t = re.sub(r"^[\[\(\{]+(.+?)[\]\)\}]+$", r"\1", t).strip()
    GENERIC_TITLES = {
        "", "untitled", "unknown", "unknown track", "audio track",
        "silence", "track", "bonus track", "hidden track",
    }
    if t in GENERIC_TITLES:
        return True
    if re.fullmatch(r"(track|trk)\s*\d+", t):
        return True
    if re.fullmatch(r"cd\s*\d+\s*track\s*\d+", t):
        return True
    if re.search(r"\btrack\s*\d+\b", t):
        return True
    return False


def is_compilation(track: dict) -> bool:
    text = f"{track.get('album', '')} {track.get('path', '')}".lower()
    return any(kw in text for kw in COMPILATION_KEYWORDS)


def is_variant(track: dict) -> bool:
    """
    Bracket-first variant detection.

    Strong signal: bracket content in title contains an unambiguous variant
    keyword (remix, live, demo, acoustic etc.) OR a multi-word variant phrase
    (radio edit, club mix, extended version etc.).

    Also catches dash-separated suffixes: "Song - Live", "Song - Acoustic Mix"

    Single words like "mix", "edit", "version" are intentionally NOT matched
    alone — they appear in "Original Mix", "Album Version", "Single Version"
    which are canonical originals, not variants.
    """
    title = (track.get("title") or "").lower()

    # ── Bracket check (strongest signal) ─────────────────────────────────────
    bracket_contents = re.findall(r"\(([^)]*)\)|\[([^\]]*)\]|\{([^}]*)\}", title)
    for groups in bracket_contents:
        content = " ".join(g for g in groups if g).lower()
        if any(kw in content for kw in BRACKET_VARIANT_KEYWORDS):
            return True
        if any(phrase in content for phrase in BRACKET_VARIANT_PHRASES):
            return True

    # ── Dash-suffix check: "Title - Live", "Title - Acoustic Mix" ────────────
    # Only match at end of title after a spaced dash/en-dash
    DASH_VARIANT_SUFFIXES = {
        "remix", "live", "acoustic", "demo", "instrumental",
        "remaster", "remastered", "extended", "club mix",
        "radio edit", "radio mix", "acoustic mix", "live version",
        "live recording", "acoustic version",
    }
    dash_match = re.search(r"\s[-–]\s(.+)$", title)
    if dash_match:
        suffix = dash_match.group(1).lower().strip()
        if any(kw in suffix for kw in DASH_VARIANT_SUFFIXES):
            return True

    # ── Multi-word phrases in the path only ───────────────────────────────────
    path = (track.get("path") or "").lower()
    MULTIWORD_PATH_VARIANTS = [
        "radio edit", "extended mix", "extended version",
        "club mix", "club edit", "12 inch", "12inch",
        "acoustic version", "live version", "live recording",
        "instrumental version", "backing track",
    ]
    return any(kw in path for kw in MULTIWORD_PATH_VARIANTS)


def normalize_artist(artist: str) -> tuple:
    """
    Split an artist string into individual tokens, preserving order.
    Returns a tuple so the first element is always the primary (lead) artist.
    Also returns a frozenset for O(1) overlap checks elsewhere.
    """
    a = (artist or "").lower().strip()
    if not a:
        return ()
    # Split on featuring markers first, then hard separators
    for sep in ["feat.", "ft.", "featuring"]:
        a = a.replace(sep, "|")
    parts = re.split(r"[,&]| and ", a)
    return tuple(p.strip() for p in parts if len(p.strip()) > 1)


def artist_set(artist_tuple: tuple) -> frozenset:
    """Frozenset view of a normalize_artist tuple for overlap checks."""
    return frozenset(artist_tuple)


def _clean_bracket(pattern: str, text: str) -> str:
    def replacer(m):
        content = m.group(1).lower()
        if any(kw in content for kw in BRACKET_VARIANT_KEYWORDS):
            return ""
        if any(phrase in content for phrase in BRACKET_VARIANT_PHRASES):
            return ""
        return m.group(0)
    return re.sub(pattern, replacer, text)


def clean_title(title: str) -> str:
    t = (title or "").lower().strip()
    t = re.sub(r'^\d+\s*[.\-\)]\s*', '', t)
    t = _clean_bracket(r'\(([^)]*)\)', t)
    t = _clean_bracket(r'\[([^\]]*)\]', t)
    t = _clean_bracket(r'\{([^}]*)\}', t)
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def confidence(match_type: str, score_gap: int) -> str:
    if match_type == "fingerprint" and score_gap >= config.MIN_SCORE_GAP:
        return "HIGH"
    if match_type == "metadata" and score_gap >= config.MIN_SCORE_GAP:
        return "MEDIUM"
    return "LOW"


def _precompute(tracks: list) -> list:
    """
    Precompute expensive per-track fields once.
    _artists is now a tuple (ordered) with a paired _artist_set (frozenset).
    _score starts as None and is filled lazily by Deduper.score().
    """
    for t in tracks:
        t["_clean_title"]  = clean_title(t.get("title", ""))
        t["_artists"]      = normalize_artist(t.get("artist", ""))
        t["_artist_set"]   = artist_set(t["_artists"])
        t["_is_variant"]   = is_variant(t)
        t["_folder"]       = os.path.dirname(t.get("path", ""))
        t["_score"]        = None  # populated lazily by Deduper.score()
    return tracks


def _same_album(path1: str, path2: str) -> bool:
    """
    Returns True if two tracks are in the same album — either the exact same
    folder, or sibling disc/side subfolders of the same parent album folder.

    Catches multi-disc rips:
      Album/CD1/track.flac  vs  Album/CD2/track.flac
      Album/SHM-CD 01/...   vs  Album/SHM-CD 02/...
      Album/SIDE1.mp3       vs  Album/SIDE2.mp3
      Album/06 song.flac    vs  Album/07 song.flac  (flat same folder)

    Does NOT block tracks from different albums by the same artist.
    """
    folder1 = os.path.dirname(path1)
    folder2 = os.path.dirname(path2)

    if folder1 == folder2:
        return True

    parent1 = os.path.dirname(folder1)
    parent2 = os.path.dirname(folder2)

    if parent1 != parent2 or not parent1:
        return False

    # Only treat as same album if subfolders look like disc/side markers
    sub1 = os.path.basename(folder1).lower()
    sub2 = os.path.basename(folder2).lower()
    disc_pattern = re.compile(
        r'^(cd|disc|disk|side|shm.?cd|digital.?media|bluray|bd)\s*[\d]'
        r'|\d{1,2}$'
    )
    return bool(disc_pattern.match(sub1) or disc_pattern.match(sub2))


# ── Deduper ───────────────────────────────────────────────────────────────────

class Deduper:
    def __init__(self, db):
        self.db = db

    def find_duplicates(self) -> list:
        tracks = _precompute([dict(t) for t in self.db.all_tracks()])

        # ── Pass 1: Fingerprint ───────────────────────────────────────────────
        # Exact audio match — catches cross-format dupes regardless of tags.
        fp_buckets: dict[str, list] = defaultdict(list)
        for t in tracks:
            if t.get("fingerprint"):
                fp_buckets[t["fingerprint"]].append(t)

        in_fp_group: set[str] = set()
        fp_groups = []
        for g in fp_buckets.values():
            if len(g) < 2:
                continue
            for t in g:
                t["_match_type"] = "fingerprint"
                in_fp_group.add(t["path"])
            fp_groups.append(g)

        # ── Pass 2: Identity match ────────────────────────────────────────────
        # Rule: same primary artist + same clean title = same song.
        # Duration intentionally not used — "Bad" at 4:06 and 4:08 are the
        # same song. Variants split out downstream in process().
        identity_buckets: dict[tuple, list] = defaultdict(list)
        for t in tracks:
            if t["path"] in in_fp_group:
                continue
            if is_generic_title(t.get("title", "")):
                continue
            if not t["_artists"]:
                continue
            clean = t["_clean_title"]
            if not clean:
                continue
            # First token = lead/primary artist (order-preserved from tags).
            primary_artist = t["_artists"][0]
            identity_buckets[(primary_artist, clean)].append(t)

        meta_groups = []
        for g in identity_buckets.values():
            if len(g) < 2:
                continue

            # Split into subgroups — tracks in the same album (same folder or
            # sibling disc subfolders) require fingerprint confirmation, not
            # metadata alone. This prevents false positives like:
            #   - Bob Dylan's two Forever Young takes on the same album
            #   - Vinyl rips split into SIDE1/SIDE2/SIDE3/SIDE4
            #   - Multi-disc rips where CD1 and CD2 both have the same tracks
            subgroups = []
            for t in g:
                placed = False
                for sg in subgroups:
                    ref = sg[0]
                    # Must share at least one artist token
                    if not (t["_artist_set"] & ref["_artist_set"]):
                        continue
                    # Same album — skip, needs fingerprint confirmation
                    if _same_album(t.get("path", ""), ref.get("path", "")):
                        continue
                    sg.append(t)
                    placed = True
                    break
                if not placed:
                    subgroups.append([t])

            for sg in subgroups:
                if len(sg) >= 2:
                    for t in sg:
                        t["_match_type"] = "metadata"
                    meta_groups.append(sg)

        return fp_groups + meta_groups

    def score(self, track: dict) -> int:
        """
        Compute quality score for a track. Result is cached on the track dict
        so repeated calls (process loop + UI export) don't recompute.
        """
        if track.get("_score") is not None:
            return track["_score"]

        s = 0
        fmt      = (track.get("format") or "").lower()
        bitrate  = track.get("bitrate") or 0
        path     = (track.get("path") or "").lower()
        filesize = track.get("filesize") or 0

        if fmt == "flac":
            s += 100
        elif fmt in ("m4a", "aac", "ogg", "opus"):
            s += 50
        elif fmt == "mp3":
            s += 30

        s += min(bitrate // 8, 50)

        if track.get("artist") and track.get("title") and track.get("album"):
            s += 10

        if track.get("has_cover"):
            s += 5

        if filesize > 0:
            s += min(int(math.log2(filesize / (1024 * 1024) + 1)), 10)

        if "_collections" in path:
            s -= config.COLLECTIONS_PENALTY
        elif is_compilation(track):
            s -= COMPILATION_PENALTY

        track["_score"] = s
        return s

    def process(self, groups: list, dry_run: bool = True) -> int:
        os.makedirs(config.DUP_DIR, exist_ok=True)
        total_freed = 0
        total = len(groups)

        print(f"Found {total} duplicate group(s):\n")

        for i, group in enumerate(groups, 1):
            if len(group) < 2:
                continue

            non_variants = [t for t in group if not t.get("_is_variant", is_variant(t))]
            variants     = [t for t in group if t.get("_is_variant", is_variant(t))]

            if len(non_variants) == 0:
                print(f"⚠️  Group {i}: all variants — skipping")
                continue

            if len(non_variants) < 2:
                print(f"⚠️  Group {i}: one original + {len(variants)} variant(s) — skipping")
                continue

            scored = sorted(non_variants, key=self.score, reverse=True)
            keeper = scored[0]
            dupes  = scored[1:]

            if not os.path.exists(keeper["path"]):
                print(f"⚠️  Group {i}: keeper missing on disk, skipping")
                continue

            print(f"Group {i}/{total}")
            print(f"  ✅ KEEP [{self.score(keeper):4d}] {keeper['path']}")

            for v in variants:
                print(f"  ⚠️  VARIANT (kept) {v['path']}")

            for d in dupes:
                if not os.path.exists(d["path"]):
                    continue

                gap = self.score(keeper) - self.score(d)
                if gap < config.MIN_SCORE_GAP:
                    print(f"  ⏭️  SKIP (gap={gap}) {d['path']}")
                    continue

                size = d.get("filesize") or 0
                total_freed += size
                dest = self._safe_dest(d["path"])

                print(f"  🗑️  {'WOULD MOVE' if dry_run else 'MOVING'} [{self.score(d):4d}] {d['path']}")
                print(f"       → {dest}")

                if not dry_run:
                    try:
                        shutil.move(d["path"], dest)
                        # Only remove from DB after the move succeeds
                        self.db.remove_track(d["path"])
                    except ValueError as e:
                        print(f"  ❌ BLOCKED: {e}")
                    except Exception as e:
                        print(f"  ❌ ERROR moving file (DB not updated): {e}")

            print()

        # ── Export to UI ──────────────────────────────────────────────────────
        output = []
        for group in groups:
            if len(group) < 2:
                continue

            non_variants = [t for t in group if not t.get("_is_variant", is_variant(t))]
            variants     = [t for t in group if t.get("_is_variant", is_variant(t))]

            if len(non_variants) < 2:
                continue

            sorted_non   = sorted(non_variants, key=self.score, reverse=True)
            keeper       = sorted_non[0]
            keeper_score = self.score(keeper)
            match_type   = keeper.get("_match_type", "metadata")

            output.append({
                "keeper": {
                    "title":   keeper.get("title"),
                    "artist":  keeper.get("artist"),
                    "album":   keeper.get("album"),
                    "path":    keeper.get("path"),
                    "format":  keeper.get("format"),
                    "bitrate": keeper.get("bitrate"),
                    "score":   keeper_score,
                },
                "dupes": [
                    {
                        "title":      d.get("title"),
                        "artist":     d.get("artist"),
                        "path":       d.get("path"),
                        "format":     d.get("format"),
                        "bitrate":    d.get("bitrate"),
                        "size":       d.get("filesize"),
                        "score":      self.score(d),
                        "confidence": confidence(match_type, keeper_score - self.score(d)),
                        "reason":     f"{match_type} match, score gap {keeper_score - self.score(d)}",
                    }
                    for d in sorted_non[1:]
                ],
                "variants": [
                    {"title": v.get("title"), "path": v.get("path")}
                    for v in variants
                ],
                "match_type": match_type,
            })

        # Sort by confidence (HIGH first) then size (largest first)
        # Paginate to MAX_UI_GROUPS to keep browser responsive
        conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        output.sort(key=lambda g: (
            conf_order.get(g["dupes"][0]["confidence"] if g["dupes"] else "LOW", 2),
            -sum(d.get("size") or 0 for d in g["dupes"])
        ))

        total_groups = len(output)
        output = output[:MAX_UI_GROUPS]

        try:
            os.makedirs("/ui", exist_ok=True)
            with open("/ui/dupes.json", "w") as f:
                json.dump({
                    "groups":       output,
                    "total_groups": total_groups,
                    "capped":       total_groups > MAX_UI_GROUPS,
                    "cap":          MAX_UI_GROUPS,
                }, f, indent=2)
            if total_groups > MAX_UI_GROUPS:
                print(f"📡 Exported {MAX_UI_GROUPS} of {total_groups} groups to UI (showing highest priority)")
            else:
                print(f"📡 Exported {total_groups} groups to UI")
        except Exception as e:
            print(f"⚠️  UI export failed: {e}")

        return total_freed

    def _safe_dest(self, src: str) -> str:
        try:
            rel = os.path.relpath(src, config.MUSIC_DIR)
        except Exception:
            rel = os.path.basename(src)

        # Guard against path traversal (e.g. src containing ../)
        dest = os.path.realpath(os.path.join(config.DUP_DIR, rel))
        dup_dir_real = os.path.realpath(config.DUP_DIR)
        if not dest.startswith(dup_dir_real + os.sep):
            raise ValueError(f"Unsafe destination path blocked: {dest}")

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if os.path.exists(dest):
            base, ext = os.path.splitext(dest)
            counter = 1
            while os.path.exists(f"{base}_dup{counter}{ext}"):
                counter += 1
            dest = f"{base}_dup{counter}{ext}"

        return dest
