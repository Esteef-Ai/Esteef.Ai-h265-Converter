"""Pause / resume / stop-job controls, used by worker mode so a PC that
someone is actively using can get out of the way on demand.

Three levers:
  - pause_after_current(): finish the file in progress, then take no more.
  - pause_now(): freeze the running ffmpeg processes in place (OS-level
    suspend -- zero CPU while frozen) and take no new work. resume() picks
    the same encode back up exactly where it was.
  - stop_job(): kill the file in progress and hand it back to the master's
    queue (another PC can take it), then pause.

Every ffmpeg/ffprobe child registers itself here (see ffmpeg_utils) so these
levers can reach it. Timeouts/stall detection in ffmpeg_utils consult
is_frozen() so time spent frozen is never mistaken for a hang.

Pause state is persisted to a small JSON file so a worker that was paused
stays paused across a reboot or supervisor restart."""
import json
import logging
import os
import threading

import psutil

from . import heartbeat

log = logging.getLogger("aiconverter.control")

_lock = threading.Lock()
_procs = set()
_paused = threading.Event()   # take no new jobs
_frozen = threading.Event()   # current job's processes are suspended
_abort = threading.Event()    # current job should be dropped and returned to the queue
_state_path = None


def init(state_path):
    """Load persisted pause state and start the keepalive that stops the
    hang watchdog from killing a process that's frozen on purpose."""
    global _state_path
    _state_path = state_path
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            if json.load(f).get("paused"):
                _paused.set()
                log.info("Starting paused (paused state carried over from before restart)")
    except (OSError, ValueError):
        pass

    def _keepalive():
        while True:
            threading.Event().wait(30)
            if _frozen.is_set():
                heartbeat.touch()

    threading.Thread(target=_keepalive, name="pause-keepalive", daemon=True).start()


def _save():
    if not _state_path:
        return
    try:
        with open(_state_path, "w", encoding="utf-8") as f:
            json.dump({"paused": _paused.is_set()}, f)
    except OSError:
        log.exception("Could not save pause state")


def register(proc):
    with _lock:
        _procs.add(proc)
        frozen = _frozen.is_set()
    if frozen:
        _suspend(proc)


def unregister(proc):
    with _lock:
        _procs.discard(proc)


def _tree(proc):
    try:
        p = psutil.Process(proc.pid)
        return [p] + p.children(recursive=True)
    except psutil.Error:
        return []


def _suspend(proc):
    for p in _tree(proc):
        try:
            p.suspend()
        except psutil.Error:
            pass


def _resume(proc):
    for p in _tree(proc):
        try:
            p.resume()
        except psutil.Error:
            pass


def _all_procs():
    with _lock:
        return list(_procs)


def is_paused():
    return _paused.is_set()


def is_frozen():
    return _frozen.is_set()


def abort_requested():
    return _abort.is_set()


def pause_after_current():
    _paused.set()
    _save()
    log.info("Pause requested: will finish the current file, then take no more")


def pause_now():
    _paused.set()
    _frozen.set()
    for proc in _all_procs():
        _suspend(proc)
    _save()
    log.info("Paused now: current encode frozen in place")


def resume():
    _paused.clear()
    _save()
    if _frozen.is_set():
        _frozen.clear()
        for proc in _all_procs():
            _resume(proc)
    log.info("Resumed")


def stop_job(pause=True):
    """Kill the job in progress so it can be handed back to the queue. With
    pause=True (the user-facing button) also stops taking new work, since
    the point is usually to free the PC up."""
    if pause:
        _paused.set()
        _save()
    _abort.set()
    _frozen.clear()
    for proc in _all_procs():
        _resume(proc)  # a suspended process can't always be killed cleanly
        try:
            for p in reversed(_tree(proc)):
                p.kill()
        except psutil.Error:
            pass
    log.info("Stop requested: killing current job and returning it to the queue%s", " (and pausing)" if pause else "")


def begin_job():
    _abort.clear()


def end_job():
    _abort.clear()
    _frozen.clear()


def state():
    if _frozen.is_set():
        mode = "paused_frozen"
    elif _paused.is_set():
        mode = "paused"
    else:
        mode = "running"
    return {"mode": mode, "paused": _paused.is_set(), "frozen": _frozen.is_set()}
