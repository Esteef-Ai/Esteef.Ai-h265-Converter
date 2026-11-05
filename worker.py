"""Worker entry point -- runs on a second PC to help work through the
master's queue. Run via worker_supervisor.ps1 (auto-restart), not directly.

The worker owns no queue of its own: it asks the master for the next job,
runs the exact same encode -> verify -> finalize pipeline the master uses
(queue_manager.process_job) against the shared network drive, and sends
every state change back to the master (RemoteStore). The master's dashboard
therefore shows everything in one place, and if this PC disappears the
master takes the job back (see aiconverter/workers.py).

Local controls (pause / resume / stop-and-return) are served on
http://127.0.0.1:8766 and can also be driven from the master's dashboard or
worker_ctl.py -- see aiconverter/control.py for what each one does."""
import ctypes
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import subprocess
import sys

# Started with some other Python (e.g. "python worker.py")? Re-run under this
# install's own venv, which is the one that has the required packages.
_VENV_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "Scripts", "python.exe")
if os.path.exists(_VENV_PY) and os.path.normcase(sys.prefix) != os.path.normcase(os.path.dirname(os.path.dirname(_VENV_PY))):
    sys.exit(subprocess.call([_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]]))

from flask import Flask, jsonify, redirect, render_template, request, url_for

from aiconverter import control, heartbeat, queue_manager, singleton
from aiconverter.config import CONFIG
from aiconverter.logging_setup import setup_logging

log = logging.getLogger("aiconverter.worker")

NAME = (CONFIG.get("worker_name") or os.environ.get("COMPUTERNAME") or "worker").upper()

_master = {"ok": False, "last_ok": None, "last_error": None}
_CLIENT = None


class JobReleased(Exception):
    """The job in progress is no longer ours: it was stopped here, or the
    master took it back. Drop it without recording any outcome."""


class MasterClient:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key

    def post(self, path, payload, timeout=30):
        """Returns (status_code, json_body). Network failures raise OSError."""
        req = urllib.request.Request(
            self.url + path, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-Worker-Key": self.key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            body = e.read()
            code = e.code
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        if code == 403:
            raise OSError("master rejected the worker key (403) -- check worker_api_key in config.json")
        return code, data


class RemoteStore:
    """queue_manager store that records job state on the master.

    - Progress-only updates are throttled and best-effort.
    - Every other update (status changes, results) is retried until the
      master accepts it -- the outcome of an encode must not get lost to a
      network blip. (If the master is down for long, it will recover the
      job itself from the shared drive when it comes back.)
    - A 409 means the master no longer considers the job ours -> JobReleased.
    - Once a job has reached finalizing, a local stop request is ignored:
      at that point the original is being swapped out and the safest thing
      is to let that finish and record it."""

    def __init__(self, client):
        self.client = client
        self._last_progress = 0.0
        self._released = set()
        self._finalizing = set()

    def begin_job(self, job_id):
        """A fresh claim of job_id: forget any "taken back from us" mark left
        over from an earlier claim of the same job. Without this, a job that
        was stopped/taken back and later handed to this worker again was
        dropped instantly every time -- an endless claim/drop loop."""
        self._released.discard(job_id)
        self._finalizing.discard(job_id)

    def update_job(self, job_id, **fields):
        if job_id not in self._finalizing and (job_id in self._released or control.abort_requested()):
            self._released.add(job_id)
            raise JobReleased()
        progress_only = set(fields) <= {"progress_pct"}
        if progress_only:
            if time.time() - self._last_progress < 5:
                return
            self._last_progress = time.time()
            try:
                code, _ = self.client.post(f"/api/worker/jobs/{job_id}/update", {"name": NAME, "fields": fields}, timeout=10)
            except OSError:
                return
            if code == 409:
                self._released.add(job_id)
                raise JobReleased()
            return

        # No give-up deadline: the master being unreachable (say it was
        # rebooted by Windows Update and is sitting at the login screen) is
        # not a reason to throw away a finished encode. If the
        # master takes the job back meanwhile, it answers 409 when it returns.
        delay = 5
        while True:
            try:
                code, body = self.client.post(f"/api/worker/jobs/{job_id}/update", {"name": NAME, "fields": fields})
                if code == 200:
                    if fields.get("status") == "finalizing":
                        self._finalizing.add(job_id)
                    return
                if code == 409:
                    self._released.add(job_id)
                    raise JobReleased()
                err = f"HTTP {code}: {body}"
            except OSError as e:
                err = str(e)
            if control.abort_requested() and job_id not in self._finalizing:
                self._released.add(job_id)
                raise JobReleased()
            log.warning("Job %d: master update failed (%s) -- retrying in %ds", job_id, err, delay)
            heartbeat.touch()
            time.sleep(delay)
            delay = min(delay * 2, 120)


def _set_awake(working):
    """Keep the PC from going to sleep while it's actually encoding (an
    encode can't survive sleep -- the master would time it out and hand it
    to another PC). Has no effect on the display or on a manual sleep."""
    if not CONFIG.get("prevent_sleep_while_working"):
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if working else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def _cleanup_work_dirs():
    root = CONFIG["work_dir"]
    os.makedirs(root, exist_ok=True)
    for name in os.listdir(root):
        if name.startswith("job_"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def worker_loop(client, store):
    log.info("Worker %s started, master %s", NAME, client.url)
    while True:
        heartbeat.touch()
        if control.is_paused() or _quitting.is_set():
            time.sleep(2)
            continue
        try:
            code, data = client.post("/api/worker/claim", {"name": NAME})
        except OSError as e:
            log.warning("Could not reach master to claim a job: %s", e)
            time.sleep(30)
            continue
        job = data.get("job") if code == 200 else None
        if not job:
            time.sleep(15)
            continue

        # Encode with the master's settings, so output is identical no
        # matter which PC did the work.
        CONFIG.update(data.get("settings") or {})
        job_id = job["id"]
        log.info("Claimed job %d: %s", job_id, job["source_path"])

        # If the share itself isn't reachable from here, process_job would
        # mark the file "Source file no longer exists" -- and then do the
        # same to every other file in the queue. Hand it back instead.
        share_root = os.path.splitdrive(job["source_path"])[0] + "\\"
        if not os.path.isdir(share_root):
            log.error("Cannot reach %s from this PC -- returning job %d and waiting", share_root, job_id)
            try:
                client.post(f"/api/worker/jobs/{job_id}/release", {"name": NAME, "reason": f"{NAME} could not reach {share_root}"})
            except OSError:
                pass
            time.sleep(120)
            continue
        control.begin_job()
        store.begin_job(job_id)
        queue_manager._current_job_id = job_id
        _set_awake(True)
        try:
            queue_manager.process_job(job)
        except JobReleased:
            log.info("Job %d dropped (stopped here or taken back by the master)", job_id)
            try:
                client.post(f"/api/worker/jobs/{job_id}/release", {"name": NAME, "reason": f"Stopped on {NAME} and returned to the queue"})
            except OSError:
                pass  # master will reclaim it via reconcile()
            time.sleep(5)  # never spin: give the master/other PC a moment before claiming again
        except Exception as e:
            log.exception("Unhandled error processing job %d", job_id)
            try:
                store.update_job(job_id, status="failed", error_message=f"Unhandled error on {NAME}: {e}")
            except Exception:
                pass
        finally:
            shutil.rmtree(os.path.join(CONFIG["work_dir"], f"job_{job_id}"), ignore_errors=True)
            try:
                queue_manager.sweep_work_dir()
            except Exception:
                log.exception("Work dir sweep failed")
            control.end_job()
            queue_manager._current_job_id = None
            queue_manager._current_progress = 0.0
            _set_awake(False)
            heartbeat.touch()


def status():
    qs = queue_manager.current_status()
    return {
        **control.state(),
        "current_job_id": qs["current_job_id"],
        "current_file": _current_file["name"] if qs["current_job_id"] else None,
        "progress": qs["current_progress"],
        "speed": qs["current_speed"],
        "eta_seconds": qs["current_eta_seconds"],
        "cpu_count": os.cpu_count(),
    }


_current_file = {"name": None}


def heartbeat_loop(client):
    handlers = {
        "pause_now": control.pause_now,
        "pause_after": control.pause_after_current,
        "resume": control.resume,
        "stop_job": lambda: control.stop_job(pause=True),
        "abort_job": lambda: control.stop_job(pause=False),
    }
    while True:
        try:
            code, data = client.post("/api/worker/heartbeat", {"name": NAME, "state": status()}, timeout=15)
            _master.update(ok=code == 200, last_ok=time.strftime("%H:%M:%S"), last_error=None if code == 200 else f"HTTP {code}")
            for cmd in data.get("commands", []):
                log.info("Command from master: %s", cmd)
                handlers.get(cmd, lambda: None)()
        except Exception as e:
            _master.update(ok=False, last_error=str(e))
        time.sleep(CONFIG["worker_heartbeat_seconds"])


# --- local control page ------------------------------------------------------

app = Flask("aiconverter_worker", template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "aiconverter", "templates"))


@app.route("/")
def page():
    return render_template("worker.html", name=NAME, s=status(), master=_master, master_url=CONFIG["master_url"])


@app.route("/api/status")
def api_status():
    return jsonify({**status(), "master": _master})


_quitting = threading.Event()


def _quit():
    """Hand back the current file (if any) and exit. Exit code 0 tells the
    supervisor this was deliberate, so it doesn't restart the worker."""
    _quitting.set()
    log.info("Quit requested")
    if queue_manager._current_job_id is not None:
        control.stop_job(pause=False)
        # Wait for worker_loop to release the job to the master. A job that
        # had already reached finalizing is allowed to finish instead.
        deadline = time.time() + 15 * 60
        while queue_manager._current_job_id is not None and time.time() < deadline:
            time.sleep(1)
    try:
        _CLIENT.post("/api/worker/heartbeat", {"name": NAME, "state": {**status(), "mode": "stopped"}}, timeout=10)
    except Exception:
        pass
    log.info("Worker shutting down")
    logging.shutdown()
    os._exit(0)


@app.route("/control/quit", methods=["POST"])
def do_quit():
    threading.Thread(target=_quit, name="quit", daemon=True).start()
    if request.args.get("json"):
        return jsonify({"quitting": True})
    return "<html><body style='font-family:Segoe UI;background:#111418;color:#e6e8eb;padding:24px'>Worker is handing back its current file and shutting down. You can close this tab.</body></html>"


@app.route("/control/<cmd>", methods=["POST"])
def do_control(cmd):
    {
        "pause_now": control.pause_now,
        "pause_after": control.pause_after_current,
        "resume": control.resume,
        "stop_job": lambda: control.stop_job(pause=True),
    }.get(cmd, lambda: None)()
    if request.args.get("json"):
        return jsonify(status())
    return redirect(url_for("page"))


def main():
    setup_logging()
    log.info("AIConverter worker %s starting", NAME)
    singleton.acquire_or_exit()
    if not CONFIG.get("master_url") or not CONFIG.get("worker_api_key"):
        raise SystemExit("config.json needs master_url and worker_api_key")
    control.init(os.path.join(os.path.dirname(CONFIG["db_path"]), "worker_state.json"))
    _cleanup_work_dirs()
    heartbeat.touch()
    heartbeat.start_watchdog(CONFIG["watchdog_max_idle_minutes"] * 60)

    global _CLIENT
    client = _CLIENT = MasterClient(CONFIG["master_url"], CONFIG["worker_api_key"])
    store = RemoteStore(client)
    queue_manager.set_store(store)

    # process_job only knows job ids; keep the filename around for the UI.
    _orig = queue_manager.process_job

    def _tracking(job):
        _current_file["name"] = job["filename"]
        return _orig(job)

    queue_manager.process_job = _tracking

    threading.Thread(target=heartbeat_loop, args=(client,), name="master-heartbeat", daemon=True).start()
    threading.Thread(target=worker_loop, args=(client, store), name="worker", daemon=True).start()

    from waitress import serve
    serve(app, host=CONFIG["worker_control_host"], port=CONFIG["worker_control_port"], _quiet=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Worker crashed")
        raise
