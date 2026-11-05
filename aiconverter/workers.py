"""Master-side support for remote worker PCs.

The master stays the single owner of the queue (SQLite DB): workers claim
jobs over HTTP (see web.py /api/worker/*), process them against the shared
network drive, and report every state change back here. Nothing a worker
does is trusted blindly:
  - a job is only handed to one PC at a time (db.claim_next_job),
  - a worker's updates are only accepted for jobs it currently holds,
  - a worker that stops checking in has its job taken back (reconcile()).

Paths: the DB stores paths as this PC sees them (e.g. V:\\videos\\x.mp4).
Drive letters are per-PC, so jobs are sent to workers as UNC paths
(\\\\nas\\media\\videos\\x.mp4), which mean the same thing everywhere, and
paths coming back are translated to the drive-letter form before storing."""
import ctypes
import json
import logging
import time
from datetime import datetime, timedelta

from . import db, queue_manager
from .config import CONFIG

log = logging.getLogger("aiconverter.workers")

_started_at = time.time()
_unc_cache = {"at": 0, "map": {}}

# Fields a worker is allowed to set on a job it holds.
ALLOWED_FIELDS = {
    "status", "progress_pct", "crf", "preset", "error_message", "attempts", "out_size",
    "vmaf_score", "duration_diff", "out_path", "quarantine_path", "quarantine_expires_at",
    "finished_at", "started_at",
}
ALLOWED_STATUSES = {
    "probing", "encoding", "verifying", "finalizing", "queued", "failed", "done",
    "no_size_gain", "verification_failed", "low_disk_space",
}
WORKING_STATUSES = ("probing", "encoding", "verifying", "finalizing")
PATH_FIELDS = ("out_path", "quarantine_path")


def _network_drives():
    """{'V:\\': '\\\\nas\\media\\', ...} for every mapped network drive,
    refreshed every few minutes."""
    if time.time() - _unc_cache["at"] < 300:
        return _unc_cache["map"]
    mapping = {}
    mpr = ctypes.WinDLL("mpr")
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if mpr.WNetGetConnectionW(f"{letter}:", buf, ctypes.byref(size)) == 0:
            mapping[f"{letter}:\\"] = buf.value.rstrip("\\") + "\\"
    _unc_cache.update(at=time.time(), map=mapping)
    return mapping


def _path_map():
    m = dict(_network_drives())
    m.update(CONFIG.get("worker_path_map") or {})
    return m


def to_worker_path(path):
    """Master path -> path a worker can open, or None if it isn't on a share."""
    for prefix, target in _path_map().items():
        if path.lower().startswith(prefix.lower()):
            return target + path[len(prefix):]
    return None


def from_worker_path(path):
    if not path:
        return path
    for prefix, target in _path_map().items():
        if path.lower().startswith(target.lower()):
            return prefix + path[len(target):]
    return path


def job_for_worker(job):
    d = dict(job)
    d["source_path"] = to_worker_path(job["source_path"])
    for f in PATH_FIELDS:
        if d.get(f):
            d[f] = to_worker_path(d[f]) or d[f]
    return d


def holds(job, worker_name):
    return job is not None and job["worker"] == worker_name and job["status"] in WORKING_STATUSES


def apply_update(job_id, worker_name, fields):
    """Returns (ok, reason). Rejects updates for a job the worker no longer
    holds -- e.g. it was handed back to the queue from the dashboard -- which
    is the worker's signal to drop it."""
    job = db.get_job(job_id)
    if not holds(job, worker_name):
        return False, "job is not assigned to this worker"
    clean = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}
    if "status" in clean and clean["status"] not in ALLOWED_STATUSES:
        return False, f"status {clean['status']!r} not allowed"
    for f in PATH_FIELDS:
        if clean.get(f):
            clean[f] = from_worker_path(clean[f])
    if clean.get("status") == "queued":
        clean["worker"] = None  # back in the pool for any PC
    if clean:
        db.update_job(job_id, **clean)
    return True, ""


def offline_after():
    return timedelta(minutes=CONFIG["worker_offline_minutes"])


def is_online(worker_row):
    if worker_row is None:
        return False
    return datetime.now() - datetime.fromisoformat(worker_row["last_seen"]) < offline_after()


def worker_state(worker_row):
    try:
        return json.loads(worker_row["state"] or "{}")
    except ValueError:
        return {}


def reconcile():
    """Take back jobs from workers that can no longer be working on them:
      - the worker hasn't checked in for worker_offline_minutes (crashed,
        PC asleep or off, network gone), or
      - the worker is checking in fine but says it's working on something
        else (e.g. it restarted and lost the job).
    A job caught mid-finalize is finished/undone by inspecting the shared
    drive (queue_manager.recover_finalizing) rather than blindly requeued.

    Offline detection is skipped for the first offline window after the
    master itself starts: its last_seen timestamps are stale simply because
    the master was down, not because the workers were."""
    master_warm = time.time() - _started_at > offline_after().total_seconds()
    for job in db.remote_active_jobs(queue_manager.LOCAL_NAME):
        w = db.get_worker(job["worker"])
        online = is_online(w)
        reason = None
        if not online and master_warm:
            reason = f"worker {job['worker']} went offline"
        elif online:
            current = worker_state(w).get("current_job_id")
            claimed_long_ago = job["started_at"] and (
                datetime.now() - datetime.fromisoformat(job["started_at"]) > timedelta(minutes=2)
            )
            if current != job["id"] and claimed_long_ago:
                reason = f"worker {job['worker']} is no longer working on it"
        if not reason:
            continue
        if job["status"] == "finalizing":
            log.warning("Job %d: %s mid-finalize -- recovering from the shared drive", job["id"], reason)
            try:
                queue_manager.recover_finalizing(job)
            except OSError:
                log.exception("Could not recover job %d (will retry)", job["id"])
        else:
            log.warning("Job %d: %s -- returning it to the queue", job["id"], reason)
            db.release_job(job["id"], f"Returned to queue: {reason}")


def reconcile_loop():
    while True:
        time.sleep(60)
        try:
            reconcile()
        except Exception:
            log.exception("Worker reconcile failed")


def dashboard_rows():
    rows = []
    for w in db.list_workers():
        st = worker_state(w)
        job = db.get_job(st["current_job_id"]) if st.get("current_job_id") else None
        rows.append({
            "name": w["name"], "address": w["address"], "last_seen": w["last_seen"],
            "online": is_online(w), "state": st, "job": job,
            "pending_command": w["pending_command"],
        })
    return rows
