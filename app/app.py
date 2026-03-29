"""
Duparr UI — Flask web interface with live log streaming
"""

from flask import Flask, render_template, jsonify, send_file, request, Response
import json
import os
import shutil
import subprocess
import threading
import sys
import functools
import hashlib
from datetime import datetime

sys.path.insert(0, "/app")
from db import Database
import config

app = Flask(__name__, template_folder="/app/templates")

DUPES_FILE   = "/ui/dupes.json"
REPORT_FILE  = "/ui/report.json"
HISTORY_FILE = "/data/job_history.json"
LOG_FILE     = "/data/duparr.log"

# ── Basic auth ────────────────────────────────────────────────────────────────
# Set DUPARR_PASSWORD env var to enable. Leave empty to disable (LAN-only use).
_AUTH_PASSWORD = os.getenv("DUPARR_PASSWORD", "").strip()

def _check_auth(password: str) -> bool:
    if not _AUTH_PASSWORD:
        return True  # Auth disabled
    # Constant-time comparison to prevent timing attacks
    expected = hashlib.sha256(_AUTH_PASSWORD.encode()).digest()
    actual   = hashlib.sha256(password.encode()).digest()
    return hashlib.compare_digest(expected, actual)

def _auth_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _AUTH_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.password):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Duparr"'}
            )
        return f(*args, **kwargs)
    return decorated

# Apply auth to all routes via before_request
@app.before_request
def require_auth():
    if request.endpoint == "health":
        return  # Health check is always public
    if not _AUTH_PASSWORD:
        return
    auth = request.authorization
    if not auth or not _check_auth(auth.password):
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Duparr"'}
        )

_job = {
    "running":  False,
    "status":   "",
    "log":      [],
    "lock":     threading.Lock(),
    "progress": {"current": 0, "total": 0, "phase": ""},
}

# ── Pipeline state ─────────────────────────────────────────────────────────────
# Tracks which steps the user has completed so the UI can enforce order.
# Persisted to /data/pipeline_state.json so it survives container restarts.

PIPELINE_FILE = "/data/pipeline_state.json"
_PIPELINE_DEFAULT = {
    "scanned":       False,   # At least one scan completed
    "tags_done":     False,   # Tag scan run (fixed or skipped)
    "album_dupes_done": False, # Album dedup run (moved or skipped)
    "rescanned":     False,   # Rescan after album dedup
    "dupes_found":   False,   # Track dedup run
}

def _load_pipeline() -> dict:
    try:
        with open(PIPELINE_FILE) as f:
            state = json.load(f)
            # Merge with defaults in case new keys added
            return {**_PIPELINE_DEFAULT, **state}
    except Exception:
        return dict(_PIPELINE_DEFAULT)

def _save_pipeline(state: dict):
    try:
        os.makedirs(os.path.dirname(PIPELINE_FILE), exist_ok=True)
        with open(PIPELINE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def _set_pipeline(key: str, value: bool = True):
    state = _load_pipeline()
    state[key] = value
    _save_pipeline(state)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_dupes():
    try:
        with open(DUPES_FILE) as f:
            raw = json.load(f)
        # Support both old format (list) and new format (dict with metadata)
        if isinstance(raw, list):
            return raw, {"total_groups": len(raw), "capped": False, "cap": 500}
        return raw.get("groups", []), {
            "total_groups": raw.get("total_groups", 0),
            "capped":       raw.get("capped", False),
            "cap":          raw.get("cap", 500),
        }
    except Exception:
        return [], {"total_groups": 0, "capped": False, "cap": 500}


def _load_report():
    try:
        with open(REPORT_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_report(data: dict):
    try:
        os.makedirs("/ui", exist_ok=True)
        existing = _load_report()
        existing.update(data)
        with open(REPORT_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


def _load_history() -> list:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _append_history(entry: dict):
    """Append a job entry to the history file. Keeps last 50 entries."""
    try:
        os.makedirs("/data", exist_ok=True)
        history = _load_history()
        history.append(entry)
        history = history[-50:]  # keep last 50
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception:
        pass


def _fmt_bytes(b):
    if not b:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _write_log(line: str):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _run_cmd_streaming(args, label):
    """Run command with live line-by-line output streamed to job log and disk."""
    header = f"\n{'='*60}\n▶ {label} — {_now()}\n{'='*60}"
    _job["log"].append(f"▶ {label}")
    _job["progress"] = {"current": 0, "total": 0, "phase": label}
    _write_log(header)

    cmd = [sys.executable, "-u", "/app/main.py"] + args
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    full_output = []

    import re as _re
    _scan_progress = _re.compile(r"Scanned (\d+) new/changed tracks")
    _dupe_progress = _re.compile(r"Group (\d+)/(\d+)")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _job["log"].append(line)
                full_output.append(line)
                _write_log(line)
                if len(_job["log"]) > 500:
                    _job["log"] = _job["log"][-400:]
                # Parse progress from scanner output
                m = _scan_progress.search(line)
                if m:
                    _job["progress"]["current"] = int(m.group(1))
                    _job["progress"]["phase"] = "scanning"
                # Parse total from scanner upfront count
                if "Total files to check:" in line:
                    m2 = _re.search(r"Total files to check: (\d+)", line)
                    if m2:
                        _job["progress"]["total"] = int(m2.group(1))
                m = _dupe_progress.search(line)
                if m:
                    _job["progress"]["current"] = int(m.group(1))
                    _job["progress"]["total"]   = int(m.group(2))
                    _job["progress"]["phase"]   = "deduping"
                if "✅ Scanned" in line:
                    m2 = _re.search(r"Scanned (\d+)", line)
                    if m2:
                        _job["progress"]["current"] = int(m2.group(1))
                        _job["progress"]["total"]   = int(m2.group(1))
        proc.wait()
        if proc.returncode != 0:
            msg = f"⚠️  Exit code {proc.returncode}"
            _job["log"].append(msg)
            _write_log(msg)
    except Exception as e:
        msg = f"❌ {e}"
        _job["log"].append(msg)
        _write_log(msg)

    return "\n".join(full_output)


def _parse_scan_output(output: str) -> dict:
    count = 0
    for line in output.splitlines():
        if "✅ Scanned" in line:
            try:
                count = int(line.strip().split()[1])
            except Exception:
                pass
        elif "Scanned" in line and "tracks" in line:
            try:
                count = int(line.strip().split()[1])
            except Exception:
                pass
    return {"tracks_scanned": count, "last_scan": _now()}


def _parse_dupes_output(output: str) -> dict:
    groups = 0
    for line in output.splitlines():
        if "duplicate group(s)" in line:
            try:
                groups = int(line.strip().split()[1])
            except Exception:
                pass
    return {"dupe_groups_found": groups, "last_dupe_scan": _now()}


def _parse_move_output(output: str) -> dict:
    moved = sum(1 for l in output.splitlines() if "MOVING" in l)
    freed_bytes = 0
    freed_str = "0 B"
    for line in output.splitlines():
        if "Freed" in line:
            parts = line.strip().split()
            if len(parts) >= 3:
                freed_str = f"{parts[-2]} {parts[-1]}"
    return {
        "last_move":            _now(),
        "files_moved_last_run": moved,
        "space_freed_last_run": freed_str,
    }


def _background_job(steps, job_type="job"):
    with _job["lock"]:
        if _job["running"]:
            return
        _job["running"] = True
        _job["log"] = []

    started_at = _now()
    history_entry = {
        "type":       job_type,
        "started_at": started_at,
        "status":     "running",
        "steps":      [],
    }

    try:
        for label, args, parse_fn in steps:
            _job["status"] = label
            output = _run_cmd_streaming(args, label)

            step_result = {"label": label}
            if parse_fn:
                parsed = parse_fn(output)
                _save_report(parsed)
                step_result.update(parsed)
            history_entry["steps"].append(step_result)

        # Navidrome
        nd_url  = os.environ.get("NAVIDROME_URL", "")
        nd_user = os.environ.get("NAVIDROME_USER", "")
        nd_pass = os.environ.get("NAVIDROME_PASSWORD", "")
        if nd_url and nd_user:
            import urllib.request, urllib.parse
            _job["log"].append("🎵 Triggering Navidrome rescan...")
            try:
                params = urllib.parse.urlencode({"u": nd_user, "p": nd_pass,
                                                 "v": "1.16.1", "c": "duparr", "f": "json"})
                urllib.request.urlopen(f"{nd_url}/rest/startScan?{params}", timeout=10)
                _job["log"].append("✅ Navidrome scan triggered")
            except Exception as e:
                _job["log"].append(f"⚠️  Navidrome: {e}")

        _job["status"] = "done"
        _job["log"].append("✅ Done")
        history_entry["status"] = "done"
        history_entry["finished_at"] = _now()

        # Update pipeline state based on job type
        if job_type == "scan_only":
            state = _load_pipeline()
            if not state["scanned"]:
                _set_pipeline("scanned", True)
            elif state["album_dupes_done"]:
                # Step 4 rescan after album dedup
                _set_pipeline("rescanned", True)
            else:
                _set_pipeline("scanned", True)
        elif job_type == "full_scan":
            # Legacy full_scan (scan + find dupes) — mark both
            _set_pipeline("scanned", True)
            _set_pipeline("dupes_found", True)
        elif job_type == "find_dupes":
            _set_pipeline("dupes_found", True)
        elif job_type == "apply_all":
            _set_pipeline("dupes_found", True)
            _set_pipeline("rescanned", True)

    except Exception as e:
        _job["status"] = "error"
        _job["log"].append(f"❌ {e}")
        history_entry["status"] = "error"
        history_entry["error"] = str(e)
    finally:
        _job["running"] = False
        _append_history(history_entry)


# ── Undo helpers ──────────────────────────────────────────────────────────────

def _dup_dir():
    return os.environ.get("DUP_DIR", "/duplicates")


def _music_dir():
    return os.environ.get("MUSIC_DIR", "/music-rw")


def _list_moved_files() -> list:
    """Walk /duplicates and return all audio files with their original path."""
    dup_dir   = _dup_dir()
    music_dir = _music_dir()
    results   = []
    audio_ext = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".aac", ".wav", ".aiff"}

    if not os.path.exists(dup_dir):
        return results

    for root, dirs, files in os.walk(dup_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if os.path.splitext(fname)[1].lower() not in audio_ext:
                continue
            dup_path = os.path.join(root, fname)
            # Reconstruct original path
            try:
                rel = os.path.relpath(dup_path, dup_dir)
                # Strip _dup suffix if present
                if rel.endswith("_dup" + os.path.splitext(rel)[1]):
                    base, ext = os.path.splitext(rel)
                    rel = base[:-4] + ext
                original = os.path.join(music_dir, rel)
            except Exception:
                original = ""
            results.append({
                "dup_path":    dup_path,
                "original":    original,
                "exists":      os.path.exists(original),
                "size":        os.path.getsize(dup_path),
                "name":        fname,
                "rel":         os.path.relpath(dup_path, dup_dir),
            })

    results.sort(key=lambda x: x["rel"])
    return results


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """Public health check — used by Docker HEALTHCHECK."""
    return jsonify({"status": "ok"}), 200


def _build_report_context():
    """Shared context for index and dupes pages."""
    data, meta = _load_dupes()
    total_size = sum(d.get("size") or 0 for g in data for d in g.get("dupes", []))
    stats = {
        "total_groups": meta["total_groups"],
        "total_size":   _fmt_bytes(total_size),
        "high":   sum(1 for g in data if any(d.get("confidence") == "HIGH"   for d in g.get("dupes", []))),
        "medium": sum(1 for g in data if any(d.get("confidence") == "MEDIUM" for d in g.get("dupes", []))),
        "low":    sum(1 for g in data if all(d.get("confidence") == "LOW"    for d in g.get("dupes", []))),
    }
    return {"groups": data, "stats": stats, "cap_meta": meta}


@app.route("/")
def index():
    return render_template("index.html", **_build_report_context())


@app.route("/report")
def report():
    data    = _load_report()
    dupes, meta = _load_dupes()
    history = _load_history()
    total_size = sum(d.get("size") or 0 for g in dupes for d in g.get("dupes", []))
    data["current_recoverable"] = _fmt_bytes(total_size)
    data["current_groups"]      = meta["total_groups"]
    return render_template("report.html", report=data, history=history)


@app.route("/undo")
def undo_page():
    files = _list_moved_files()
    total_size = sum(f["size"] for f in files)
    return render_template("undo.html", files=files,
                           total_count=len(files),
                           total_size=_fmt_bytes(total_size))


@app.route("/api/undo", methods=["POST"])
def api_undo():
    """Restore a single file from /duplicates to its original path."""
    data     = request_json()
    dup_path = data.get("dup_path", "")

    if not dup_path:
        return jsonify({"error": "No path provided"}), 400

    # Safety: path must be inside DUP_DIR
    dup_dir = _dup_dir()
    if not os.path.abspath(dup_path).startswith(os.path.abspath(dup_dir)):
        return jsonify({"error": "Invalid path"}), 400

    if not os.path.exists(dup_path):
        return jsonify({"error": "File not found in duplicates"}), 404

    try:
        rel      = os.path.relpath(dup_path, dup_dir)
        original = os.path.join(_music_dir(), rel)
        os.makedirs(os.path.dirname(original), exist_ok=True)

        if os.path.exists(original):
            return jsonify({"error": f"File already exists at original path"}), 409

        shutil.move(dup_path, original)
        _append_history({
            "type":       "undo",
            "started_at": _now(),
            "finished_at": _now(),
            "status":     "done",
            "steps":      [{"label": f"Restored: {os.path.basename(original)}"}],
        })
        return jsonify({"ok": True, "restored_to": original})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/undo_all", methods=["POST"])
def api_undo_all():
    """Restore ALL files from /duplicates back to their original paths."""
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400

    def _do_undo_all():
        with _job["lock"]:
            _job["running"] = True
            _job["log"] = []
        _job["status"] = "Restoring files"

        files   = _list_moved_files()
        restored = 0
        skipped  = 0
        errors   = 0

        try:
            for f in files:
                dup_path = f["dup_path"]
                original = f["original"]
                if not original:
                    skipped += 1
                    continue
                if os.path.exists(original):
                    _job["log"].append(f"⏭️  SKIP (exists): {os.path.basename(original)}")
                    skipped += 1
                    continue
                try:
                    os.makedirs(os.path.dirname(original), exist_ok=True)
                    shutil.move(dup_path, original)
                    _job["log"].append(f"✅ Restored: {os.path.basename(original)}")
                    restored += 1
                except Exception as e:
                    _job["log"].append(f"❌ {os.path.basename(dup_path)}: {e}")
                    errors += 1

                if len(_job["log"]) > 500:
                    _job["log"] = _job["log"][-400:]

            summary = f"✅ Done — {restored} restored, {skipped} skipped, {errors} errors"
            _job["log"].append(summary)
            _job["status"] = "done"
            _append_history({
                "type":        "undo_all",
                "started_at":  _now(),
                "finished_at": _now(),
                "status":      "done",
                "steps":       [{"label": summary}],
            })
        except Exception as e:
            _job["status"] = "error"
            _job["log"].append(f"❌ {e}")
        finally:
            _job["running"] = False

    threading.Thread(target=_do_undo_all, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/pipeline")
def api_pipeline():
    """Return current pipeline state for UI enforcement."""
    return jsonify(_load_pipeline())


@app.route("/api/pipeline/skip", methods=["POST"])
def api_pipeline_skip():
    """Mark a pipeline step as skipped (treated as done)."""
    data = request.get_json()
    step = data.get("step", "")
    valid = set(_PIPELINE_DEFAULT.keys())
    if step not in valid:
        return jsonify({"error": f"Unknown step: {step}"}), 400
    _set_pipeline(step, True)
    return jsonify({"ok": True, "state": _load_pipeline()})


@app.route("/api/pipeline/reset", methods=["POST"])
def api_pipeline_reset():
    """Reset pipeline state (e.g. after erasing DB)."""
    _save_pipeline(dict(_PIPELINE_DEFAULT))
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({
        "running":  _job["running"],
        "status":   _job["status"],
        "log":      _job["log"][-100:],
        "progress": _job["progress"],
    })


@app.route("/api/report")
def api_report():
    return jsonify(_load_report())


@app.route("/api/history")
def api_history():
    return jsonify(_load_history())


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400
    threading.Thread(target=_background_job, args=([
        ("Scanning library", ["scan"], _parse_scan_output),
    ], "scan"), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/find_dupes", methods=["POST"])
def api_find_dupes():
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400
    threading.Thread(target=_background_job, args=([
        ("Finding duplicates", ["dupes", "--dry-run"], _parse_dupes_output),
    ], "find_dupes"), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/apply_all", methods=["POST"])
def api_apply_all():
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400
    threading.Thread(target=_background_job, args=([
        ("Moving duplicates",    ["dupes", "--move"],    _parse_move_output),
        ("Rescanning library",   ["scan"],               _parse_scan_output),
        ("Refreshing dupe list", ["dupes", "--dry-run"], _parse_dupes_output),
    ], "apply_all"), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scan_only", methods=["POST"])
def api_scan_only():
    """Step 1 / Step 4 — scan library only, no duplicate detection."""
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400
    threading.Thread(target=_background_job, args=([
        ("Scanning library", ["scan"], _parse_scan_output),
    ], "scan_only"), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/full_scan", methods=["POST"])
def api_full_scan():
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400
    threading.Thread(target=_background_job, args=([
        ("Scanning library",   ["scan"],               _parse_scan_output),
        ("Finding duplicates", ["dupes", "--dry-run"], _parse_dupes_output),
    ], "full_scan"), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/fetch_covers", methods=["POST"])
def api_fetch_covers():
    if _job["running"]:
        return jsonify({"error": "Job already running"}), 400
    threading.Thread(target=_background_job, args=([
        ("Fetching covers", ["covers", "--fetch"], None),
    ], "fetch_covers"), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/erase_database", methods=["POST"])
def api_erase_database():
    """Wipe the track database so the next scan starts fresh."""
    if _job["running"]:
        return jsonify({"error": "A job is currently running — wait for it to finish"}), 400
    try:
        _save_pipeline(dict(_PIPELINE_DEFAULT))  # Reset pipeline on DB erase
        db = Database(config.DB_PATH)
        with db._conn() as conn:
            conn.execute("DELETE FROM tracks")
        # Also clear the dupes UI file so stale groups don't show
        try:
            os.remove("/ui/dupes.json")
        except FileNotFoundError:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/album-dupes")
def album_dupes_page():
    """Album-level duplicate detection results page."""
    return render_template("album_dupes.html")


@app.route("/api/album_dupes/scan", methods=["POST"])
def api_album_dupes_scan():
    """Kick off a background album duplicate scan."""
    if _job["running"]:
        return jsonify({"error": "A job is currently running"}), 400

    _job["running"]  = True
    _job["status"]   = "Scanning for duplicate albums"
    _job["log"]      = ["▶ Album duplicate scan started"]
    _job["progress"] = {"current": 0, "total": 0, "phase": "album_dupes"}

    def _run():
        try:
            from albumdeduper import AlbumDeduper
            db      = Database(config.DB_PATH)
            scanner = AlbumDeduper(db, log_fn=lambda msg: _job["log"].append(msg))
            pairs   = scanner.find_duplicate_albums()

            import json as _json
            with open("/tmp/album_dup_results.json", "w") as f:
                _json.dump(pairs, f)

            # Write report to /ui
            report = ["DUPARR ALBUM DUPLICATE REPORT", "=" * 60, ""]
            total_mb = sum(p.get("move_size_mb", 0) for p in pairs)
            report.append(f"Duplicate album pairs found: {len(pairs)}")
            report.append(f"Total space to free: {total_mb:.0f} MB ({total_mb/1024:.1f} GB)")
            report.append("")
            report.append("=" * 60)
            for p in pairs:
                report.append("")
                report.append(f"{p['keeper_album']} — {p['keeper_artist']}")
                report.append(f"  KEEP ({p['keeper_track_count']} tracks, score {p['keeper_score']}): {p['keeper_folder']}")
                report.append(f"  MOVE ({p['mover_track_count']} tracks, score {p['mover_score']}): {p['mover_folder']}")
                report.append(f"  Match: {p['matched_tracks']}/{p['mover_track_count']} tracks  •  Save: {p['move_size_mb']:.0f} MB")
            with open("/ui/album_dup_report.txt", "w") as f:
                f.write("\n".join(report))

            _job["status"] = "done"
            _job["log"].append(f"✅ Done — {len(pairs)} duplicate album pairs found")
            _job["log"].append("📄 Report saved — download from Album Dupes page")
            _set_pipeline("album_dupes_done", True)
        except Exception as e:
            import traceback
            _job["status"] = "error"
            _job["log"].append(f"❌ {e}")
            _job["log"].append(traceback.format_exc())
        finally:
            _job["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True})


@app.route("/ui/tag_scan_report.txt")
def tag_scan_report():
    """Generate report fresh from current scan results — never serves stale cache."""
    try:
        import json as _json
        with open("/tmp/tag_scan_results.json") as f:
            groups = _json.load(f)
    except FileNotFoundError:
        return "No scan results yet — run a tag scan first.", 404
    except Exception as e:
        return f"Error reading results: {e}", 500

    high   = [g for g in groups if g["confidence"] == "HIGH"]
    med    = [g for g in groups if g["confidence"] == "MEDIUM"]
    low    = [g for g in groups if g["confidence"] == "LOW"]

    lines = [
        "DUPARR TAG SCAN REPORT",
        "=" * 60,
        "",
        f"Total fragmented compilations: {len(groups)}",
        f"  HIGH:   {len(high)}",
        f"  MEDIUM: {len(med)}",
        f"  LOW:    {len(low)}",
        "",
        "=" * 60,
        "FULL LIST",
        "=" * 60,
    ]
    for g in groups:
        lines.append("")
        lines.append(f"[{g['confidence']}] {g['album']}  ({g['track_count']} tracks)")
        lines.append(f"  Folder: {g['folder']}")
        lines.append(f"  Track artists: {', '.join(g['artists'])}")
        aa = ', '.join(g['album_artists']) if g.get('album_artists') else '[NOT SET]'
        lines.append(f"  Current album artist: {aa}")

    from io import BytesIO
    report_bytes = "\n".join(lines).encode("utf-8")
    return send_file(
        BytesIO(report_bytes),
        as_attachment=True,
        download_name="tag_scan_report.txt",
        mimetype="text/plain"
    )


@app.route("/ui/album_dup_report.txt")
def album_dup_report():
    path = "/ui/album_dup_report.txt"
    if not os.path.exists(path):
        return "No report yet — run an album duplicate scan first.", 404
    return send_file(path, as_attachment=True, download_name="album_dup_report.txt", mimetype="text/plain")


@app.route("/api/album_dupes/results")
def api_album_dupes_results():
    """Return results of the last album duplicate scan."""
    try:
        import json as _json
        with open("/tmp/album_dup_results.json") as f:
            pairs = _json.load(f)
        return jsonify({"pairs": pairs, "scanned": True})
    except FileNotFoundError:
        return jsonify({"pairs": [], "scanned": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/album_dupes/move", methods=["POST"])
def api_album_dupes_move():
    """Move an entire duplicate album folder to /duplicates."""
    data          = request.get_json()
    mover_folder  = data.get("mover_folder", "").strip()
    keeper_folder = data.get("keeper_folder", "").strip()

    if not mover_folder or not keeper_folder:
        return jsonify({"error": "mover_folder and keeper_folder required"}), 400

    music_real = os.path.realpath(config.MUSIC_DIR)
    if not os.path.realpath(mover_folder).startswith(music_real + os.sep):
        return jsonify({"error": "Unsafe path blocked"}), 400

    def _log(msg):
        _job["log"].append(msg)

    try:
        from albumdeduper import AlbumDeduper
        db      = Database(config.DB_PATH)
        scanner = AlbumDeduper(db, log_fn=_log)
        result  = scanner.move_album(mover_folder, keeper_folder)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/album_dupes/undo", methods=["POST"])
def api_album_dupes_undo():
    """Restore a previously moved album folder."""
    data         = request.get_json()
    mover_folder = data.get("mover_folder", "").strip()

    if not mover_folder:
        return jsonify({"error": "mover_folder required"}), 400

    try:
        from albumdeduper import AlbumDeduper
        db      = Database(config.DB_PATH)
        scanner = AlbumDeduper(db)
        result  = scanner.undo_album_move(mover_folder)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.route("/dupes")
def dupes_page():
    """Track duplicate groups — the report page."""
    return render_template("dupes.html", **_build_report_context())


@app.route("/tags")
def tags_page():
    """Compilation tag fixer review page — results load async."""
    return render_template("tags.html")


@app.route("/api/tags/scan", methods=["POST"])
def api_tags_scan():
    """Kick off a background tag scan."""
    if _job["running"]:
        return jsonify({"error": "A job is currently running"}), 400

    def _run():
        try:
            _job["running"]  = True
            _job["status"]   = "Scanning for fragmented compilations"
            _job["log"]      = ["▶ Tag scan started"]
            _job["progress"] = {"current": 0, "total": 0, "phase": "tags"}

            from tagger import Tagger
            db     = Database(config.DB_PATH)
            tagger = Tagger(db, log_fn=lambda msg: _job["log"].append(msg))
            groups = tagger.find_all()

            import json as _json
            with open("/tmp/tag_scan_results.json", "w") as f:
                _json.dump(groups, f)

            collection = sum(1 for g in groups if g.get("type") == "COLLECTION")
            compilation = sum(1 for g in groups if g.get("type") == "COMPILATION")
            _job["status"] = "done"
            _job["log"].append(f"✅ Done — {len(groups)} folders ({collection} collection, {compilation} compilation)")
            _set_pipeline("tags_done", True)
        except Exception as e:
            import traceback
            _job["status"] = "error"
            _job["log"].append(f"❌ {e}")
            _job["log"].append(traceback.format_exc())
        finally:
            _job["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/tags/status")
def api_tags_status():
    """How many tag fixes are in the DB."""
    try:
        db = Database(config.DB_PATH)
        fixes = db.all_tag_fixes()
        return jsonify({"fixes_applied": len(fixes)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags/results")
def api_tags_results():
    """Return the results of the last tag scan."""
    try:
        import json as _json
        with open("/tmp/tag_scan_results.json") as f:
            groups = _json.load(f)
        return jsonify({"groups": groups, "scanned": True})
    except FileNotFoundError:
        return jsonify({"groups": [], "pending": True, "scanned": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fix_folder", methods=["POST"])
def api_fix_folder():
    """Fix tags for a specific folder (compilation or collection)."""
    data   = request.get_json()
    folder = data.get("folder", "").strip()
    tracks = data.get("tracks", [])

    if not folder or not tracks:
        return jsonify({"error": "folder and tracks required"}), 400

    # Path safety — all tracks must be within MUSIC_DIR
    music_real = os.path.realpath(config.MUSIC_DIR)
    for path in tracks:
        real = os.path.realpath(path)
        if not real.startswith(music_real + os.sep):
            return jsonify({"error": f"Unsafe path blocked: {path}"}), 400

    # Capture tagger output into job log
    log_lines = []
    def _log(msg):
        log_lines.append(msg)
        _job["log"].append(msg)

    correct_aa = data.get("correct_aa", None)  # optional — auto-detected if not supplied

    try:
        from tagger import Tagger
        db     = Database(config.DB_PATH)
        tagger = Tagger(db, log_fn=_log)
        fix_type   = data.get("fix_type", "COMPILATION")
        new_artist = data.get("new_artist", "").strip()
        new_album  = data.get("new_album", "").strip() or None
        result = tagger.fix_folder(
            folder, tracks,
            fix_type=fix_type,
            correct_aa=correct_aa,
            new_artist=new_artist or None,
            new_album=new_album,
        )
        result["log"] = log_lines
        return jsonify(result)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _log(f"❌ Exception: {e}")
        return jsonify({"error": str(e), "detail": tb, "log": log_lines}), 500


@app.route("/api/undo_folder_tags", methods=["POST"])
def api_undo_folder_tags():
    """Restore original Album Artist tags for a folder."""
    data   = request.get_json()
    folder = data.get("folder", "").strip()

    if not folder:
        return jsonify({"error": "folder required"}), 400

    music_real = os.path.realpath(config.MUSIC_DIR)
    if not os.path.realpath(folder).startswith(music_real + os.sep):
        return jsonify({"error": "Unsafe path blocked"}), 400

    from tagger import Tagger
    db = Database(config.DB_PATH)
    tagger = Tagger(db)
    result = tagger.undo_folder(folder)
    return jsonify(result)


@app.route("/logs")
def logs():
    logs_content = ""
    try:
        with open(LOG_FILE) as f:
            lines = f.read().splitlines()[-500:]
        logs_content = "\n".join(lines)
    except FileNotFoundError:
        logs_content = ""
    except Exception as e:
        logs_content = f"Error reading log: {e}"
    return render_template("logs.html", logs=logs_content)


@app.route("/api/logs/raw")
def logs_raw():
    try:
        with open(LOG_FILE) as f:
            return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except FileNotFoundError:
        return "No log file yet.", 200, {"Content-Type": "text/plain"}
    except Exception as e:
        return str(e), 500, {"Content-Type": "text/plain"}


@app.route("/download")
def download():
    data, _ = _load_dupes()
    lines = []
    for i, g in enumerate(data, 1):
        lines.append(f"GROUP {i} [{g.get('match_type','?').upper()}]")
        lines.append(f"  KEEP: {g['keeper'].get('title')} — {g['keeper'].get('path')}")
        for d in g.get("dupes", []):
            mb = f"{(d.get('size') or 0) / 1024 / 1024:.1f} MB"
            lines.append(f"  MOVE [{d.get('confidence','?')}] {mb}: {d.get('path')}")
        for v in g.get("variants", []):
            lines.append(f"  KEPT VARIANT: {v.get('path')}")
        lines.append("")
    tmp = "/tmp/duparr_report.txt"
    with open(tmp, "w") as f:
        f.write("\n".join(lines))
    return send_file(tmp, as_attachment=True, download_name="duparr_report.txt")


def request_json():
    from flask import request
    try:
        return request.get_json() or {}
    except Exception:
        return {}


if __name__ == "__main__":
    os.makedirs("/ui",   exist_ok=True)
    os.makedirs("/data", exist_ok=True)
    app.run(host="0.0.0.0", port=5045, debug=False)
