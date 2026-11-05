"""Local dashboard: add directories to convert, watch queue progress, see
before/after size and VMAF quality score per file, retry failures, and
restore a quarantined original if you change your mind before it purges."""
import functools
import hmac
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import psutil
from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_file, url_for

from . import db, folders, quarantine, queue_manager, scanner, workers
from .config import CONFIG, PROJECT_ROOT, SHARED_ENCODE_SETTINGS

log = logging.getLogger("aiconverter.web")

app = Flask(__name__)


def _fmt_bytes(n):
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_duration(seconds):
    if not seconds:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


app.jinja_env.filters["fmt_bytes"] = _fmt_bytes
app.jinja_env.filters["fmt_duration"] = _fmt_duration
app.jinja_env.filters["fmt_time"] = lambda t: time.strftime("%b %d %H:%M", time.localtime(t)) if t else "-"


VIEWS = ("all", "active", "trouble")


@app.route("/")
def dashboard():
    # The selected view lives in a cookie so it survives the page's
    # auto-refresh and the redirect back here after any button click --
    # a query-string-only filter got silently dropped by both.
    view = request.args.get("view") or request.cookies.get("view") or "all"
    if view not in VIEWS:
        view = "all"
    jobs = db.list_jobs(view=view)
    stats = db.summary_stats()
    watch_dirs = db.list_watch_dirs()
    status = queue_manager.current_status()
    resp = make_response(render_template(
        "dashboard.html", jobs=jobs, stats=stats, watch_dirs=watch_dirs,
        status=status, config=CONFIG, view=view, view_counts=db.count_by_view(),
        summary=db.library_summary(),
        workers=workers.dashboard_rows(), local_name=queue_manager.LOCAL_NAME,
    ))
    resp.set_cookie("view", view, max_age=60 * 60 * 24 * 365)
    return resp


@app.route("/jobs/requeue_trouble", methods=["POST"])
def requeue_trouble():
    n = db.requeue_trouble_jobs()
    log.info("Requeued %d trouble jobs from the dashboard", n)
    return redirect(url_for("dashboard"))


PRIORITIES = {"high": 10, "normal": 0, "low": -10}


def _profile_arg(form):
    p = form.get("profile", "standard")
    return p if p in CONFIG["codec_profiles"] else "standard"


def _priority_arg(form):
    return PRIORITIES.get(form.get("priority", "normal"), 0)


def _scan_in_background(path, recursive, profile, priority):
    threading.Thread(target=scanner.scan_directory, args=(path, recursive, profile, priority), daemon=True).start()


@app.route("/add_directory", methods=["POST"])
def add_directory():
    path = request.form.get("path", "").strip().strip('"')
    recursive = request.form.get("recursive") == "on"
    profile, priority = _profile_arg(request.form), _priority_arg(request.form)
    if path and os.path.isdir(path):
        path = scanner.true_case(path)  # see scanner.true_case -- wrong case breaks writes on the share
        db.add_watch_dir(path, recursive=recursive, profile=profile, priority=priority)
        _scan_in_background(path, recursive, profile, priority)
        log.info("Watching %s (profile %s, priority %d)", path, profile, priority)
    else:
        log.warning("Rejected add_directory for invalid path: %r", path)
    return redirect(request.form.get("next") or url_for("dashboard"))


@app.route("/watch_dirs/<int:dir_id>/settings", methods=["POST"])
def watch_dir_settings(dir_id):
    wd = db.get_watch_dir(dir_id)
    if wd:
        profile, priority = _profile_arg(request.form), _priority_arg(request.form)
        db.update_watch_dir(dir_id, profile, priority)
        db.set_priority_for_folder(wd["path"], priority)
        # rescan so files skipped for their format get picked up if the profile now includes them
        _scan_in_background(wd["path"], bool(wd["recursive"]), profile, priority)
    return redirect(request.form.get("next") or url_for("folders_view"))


@app.route("/folders")
def folders_view():
    rows, disk_at, counting = folders.rows()
    if not disk_at and not counting:
        folders.refresh_disk_counts()
        counting = True
    return render_template(
        "folders.html", rows=rows, disk_at=disk_at, counting=counting, config=CONFIG,
        profiles=CONFIG["codec_profiles"], priority_names={v: k for k, v in PRIORITIES.items()},
    )


@app.route("/folders/recount", methods=["POST"])
def folders_recount():
    folders.refresh_disk_counts()
    return redirect(url_for("folders_view"))


@app.route("/rescan", methods=["POST"])
def rescan():
    threading.Thread(target=scanner.scan_all_watch_dirs, daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/watch_dirs/<int:dir_id>/remove", methods=["POST"])
def remove_watch_dir(dir_id):
    db.remove_watch_dir(dir_id)
    return redirect(request.form.get("next") or url_for("dashboard"))


@app.route("/jobs/<int:job_id>/retry", methods=["POST"])
def retry_job(job_id):
    # crf is reset to None (falls back to the global default) rather than
    # carrying over a previously-escalated value -- a manual retry means
    # "try again from scratch," not "keep whatever CRF a prior failed
    # attempt happened to end on."
    db.update_job(job_id, status="queued", error_message=None, attempts=0, progress_pct=0, crf=None, out_size=None)
    return redirect(url_for("dashboard"))


@app.route("/quarantine")
def quarantine_view():
    items = db.list_quarantined()
    return render_template("quarantine.html", items=items, config=CONFIG)


@app.route("/quarantine/purge_all", methods=["POST"])
def quarantine_purge_all():
    purged = quarantine.purge_all()
    log.info("Purged %d quarantined originals on request from the dashboard", purged)
    return redirect(url_for("quarantine_view"))


@app.route("/api/browse")
def api_browse():
    """Server-side directory listing for the folder-browse picker in the
    dashboard. Browsers can't expose real filesystem paths to a page for
    security reasons, so the picker asks this local server to list
    directories instead -- safe here since the dashboard only ever runs on
    this machine (or trusted LAN) and is being used to point at local paths
    anyway."""
    path = request.args.get("path", "").strip()
    if not path:
        # Root view: list available drives.
        drives = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
        return jsonify({"path": "", "parent": None, "entries": [{"name": d, "path": d} for d in drives]})

    if not os.path.isdir(path):
        return jsonify({"error": "Not a directory"}), 400

    try:
        entries = []
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append({"name": name, "path": full})
    except OSError as e:
        return jsonify({"error": str(e)}), 400

    parent = os.path.dirname(path.rstrip("\\/"))
    is_drive_root = len(path.rstrip("\\/")) <= 2  # e.g. "C:"
    return jsonify({
        "path": path,
        "parent": None if is_drive_root else (parent or None),
        "entries": entries,
    })


# Runs in its own process: Tk needs a thread of its own that nothing else uses,
# and a dialog left open must not hold up the dashboard.
_PICK_FOLDER_CODE = """
import json, os, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
print(json.dumps(filedialog.askdirectory(parent=root, title="Choose a folder to convert",
                                         initialdir=os.path.expanduser("~"), mustexist=True) or ""))
"""
_pick_folder_lock = threading.Lock()


@app.route("/api/pick_folder", methods=["POST"])
def api_pick_folder():
    """Opens the native Windows folder dialog. It appears on the PC running
    this server, so it's only offered to a browser on that same PC; anyone
    else gets the in-page picker (/api/browse) instead. The custom header
    keeps other websites from popping the dialog (they can't send it to this
    origin without a CORS approval this server never gives)."""
    if request.remote_addr not in ("127.0.0.1", "::1") or not request.headers.get("X-Requested-With"):
        return jsonify({"fallback": True})
    if not _pick_folder_lock.acquire(blocking=False):
        return jsonify({"busy": True})
    try:
        out = subprocess.run([sys.executable, "-c", _PICK_FOLDER_CODE], capture_output=True, text=True,
                             timeout=600, creationflags=subprocess.CREATE_NO_WINDOW)
        if out.returncode != 0:
            log.warning("Folder dialog failed, using the in-page picker: %s", out.stderr.strip()[-300:])
            return jsonify({"fallback": True})
        path = json.loads(out.stdout)
        return jsonify({"path": os.path.normpath(path) if path else ""})
    except subprocess.TimeoutExpired:
        return jsonify({"path": ""})
    finally:
        _pick_folder_lock.release()


@app.route("/jobs/<int:job_id>/restore", methods=["POST"])
def restore_job(job_id):
    job = db.get_job(job_id)
    if job and job["quarantine_path"] and os.path.exists(job["quarantine_path"]):
        try:
            if job["out_path"] and os.path.exists(job["out_path"]):
                os.remove(job["out_path"])
            quarantine.restore(job["quarantine_path"])
            db.update_job(job_id, status="restored", quarantine_path=None, quarantine_expires_at=None, out_path=None)
        except OSError:
            log.exception("Failed to restore job %d", job_id)
    return redirect(url_for("dashboard"))


@app.route("/api/jobs")
def api_jobs():
    return jsonify([dict(j) for j in db.list_jobs()])


@app.route("/api/status")
def api_status():
    stats = db.summary_stats()
    status = queue_manager.current_status()
    disks = {}
    for drive in {"C:\\", os.path.splitdrive(CONFIG["work_dir"])[0] + "\\"}:
        try:
            usage = shutil.disk_usage(drive)
            disks[drive] = {"free_gb": round(usage.free / 1024**3, 1), "total_gb": round(usage.total / 1024**3, 1)}
        except OSError:
            pass
    return jsonify({
        "stats": stats,
        "current": status,
        "disks": disks,
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_percent": psutil.virtual_memory().percent,
    })


# ---------------------------------------------------------------------------
# Remote worker PCs (see aiconverter/workers.py and worker.py)
# ---------------------------------------------------------------------------

WORKER_COMMANDS = ("pause_now", "pause_after", "resume", "stop_job")


def _key_ok(given):
    key = CONFIG.get("worker_api_key") or ""
    return bool(key) and hmac.compare_digest(str(given or ""), key)


def worker_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not _key_ok(request.headers.get("X-Worker-Key") or request.args.get("key")):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.route("/api/worker/heartbeat", methods=["POST"])
@worker_auth
def worker_heartbeat():
    body = request.get_json(force=True)
    name = body["name"]
    state = body.get("state") or {}
    db.upsert_worker(name, request.remote_addr, json.dumps(state))
    commands = []
    cmd = db.pop_worker_command(name)
    if cmd:
        commands.append(cmd)
    cur = state.get("current_job_id")
    if cur and not workers.holds(db.get_job(cur), name):
        # The job was taken back (dashboard "return to queue", or the worker
        # was presumed offline) -- tell the worker to drop it.
        commands.append("abort_job")
    return jsonify({"commands": commands})


@app.route("/api/worker/claim", methods=["POST"])
@worker_auth
def worker_claim():
    name = request.get_json(force=True)["name"]
    if name.upper() == queue_manager.LOCAL_NAME:
        # A worker install on the master PC itself would fight the master's
        # own job bookkeeping (and gain nothing -- same CPU).
        return jsonify({"error": "worker name matches the master PC; workers must run on another PC"}), 400
    job = db.claim_next_job(name, path_filter=lambda p: workers.to_worker_path(p) is not None)
    if job is None:
        return jsonify({"job": None})
    log.info("Job %d claimed by worker %s", job["id"], name)
    return jsonify({
        "job": workers.job_for_worker(job),
        "settings": {k: CONFIG[k] for k in SHARED_ENCODE_SETTINGS if k in CONFIG},
    })


@app.route("/api/worker/jobs/<int:job_id>/update", methods=["POST"])
@worker_auth
def worker_update(job_id):
    body = request.get_json(force=True)
    ok, reason = workers.apply_update(job_id, body["name"], body.get("fields") or {})
    if not ok:
        return jsonify({"error": reason}), 409
    return jsonify({"ok": True})


@app.route("/api/worker/jobs/<int:job_id>/release", methods=["POST"])
@worker_auth
def worker_release(job_id):
    body = request.get_json(force=True)
    job = db.get_job(job_id)
    if not workers.holds(job, body["name"]) or job["status"] == "finalizing":
        return jsonify({"error": "not releasable"}), 409
    db.release_job(job_id, body.get("reason") or f"Returned to queue by {body['name']}")
    log.info("Job %d returned to the queue by worker %s", job_id, body["name"])
    return jsonify({"ok": True})


@app.route("/workers/<name>/command", methods=["POST"])
def worker_command(name):
    cmd = request.form.get("cmd")
    if cmd in WORKER_COMMANDS and db.get_worker(name):
        db.set_worker_command(name, cmd)
    return redirect(url_for("dashboard"))


@app.route("/workers/<name>/forget", methods=["POST"])
def worker_forget(name):
    db.delete_worker(name)
    return redirect(url_for("dashboard"))


# Files that make up a worker install (everything else in the project --
# DB, logs, venv, config, work dir -- is master-only or per-machine).
_PACKAGE_FILES = ("worker.py", "worker_ctl.py", "worker_start.ps1", "worker_supervisor.ps1", "worker_install.ps1", "requirements.txt")


@app.route("/worker/setup.ps1")
@worker_auth
def worker_setup_script():
    """One-liner bootstrap for a new worker PC: downloads the package and
    runs the installer with this master's address and key filled in."""
    master_url = request.host_url.rstrip("/")
    script = "\r\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        f"$MasterUrl = '{master_url}'",
        f"$Key = '{CONFIG['worker_api_key']}'",
        "$tmp = Join-Path $env:TEMP 'aiconverter_worker_pkg'",
        "if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }",
        "New-Item -ItemType Directory -Force $tmp | Out-Null",
        'Invoke-WebRequest "$MasterUrl/worker/package.zip?key=$Key" -UseBasicParsing -OutFile "$tmp\\package.zip"',
        'Expand-Archive -Force "$tmp\\package.zip" $tmp',
        '& "$tmp\\worker_install.ps1" -MasterUrl $MasterUrl -Key $Key -PackageDir $tmp',
        "",
    ])
    return app.response_class(script, mimetype="text/plain")


@app.route("/worker/package.zip")
@worker_auth
def worker_package():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in _PACKAGE_FILES:
            z.write(os.path.join(PROJECT_ROOT, f), f)
        pkg = os.path.join(PROJECT_ROOT, "aiconverter")
        for root, dirs, files in os.walk(pkg):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, PROJECT_ROOT))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="aiconverter_worker.zip")


_ffmpeg_zip_lock = threading.Lock()


@app.route("/worker/ffmpeg.zip")
@worker_auth
def worker_ffmpeg():
    """The exact ffmpeg/ffprobe build the master uses (it's known to have a
    working libx265 + libvmaf), zipped once and cached."""
    out = os.path.join(os.path.dirname(CONFIG["db_path"]), "ffmpeg_for_workers.zip")
    with _ffmpeg_zip_lock:
        if not os.path.exists(out):
            tmp = out + ".part"
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                for exe in ("ffmpeg_bin", "ffprobe_bin"):
                    path = shutil.which(CONFIG[exe])
                    z.write(path, os.path.basename(path))
            os.replace(tmp, out)
    return send_file(out, mimetype="application/zip", as_attachment=True, download_name="ffmpeg.zip")


def run():
    from waitress import serve
    log.info("Starting dashboard on http://%s:%d", CONFIG["web_host"], CONFIG["web_port"])
    serve(app, host=CONFIG["web_host"], port=CONFIG["web_port"], _quiet=True)
