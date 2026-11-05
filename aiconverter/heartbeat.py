"""Liveness tracking + a self-recovery watchdog for hangs that aren't Python
exceptions -- e.g. a blocked file syscall against a flaky network/mounted
drive can freeze a thread forever with no traceback and nothing to catch.

Any code that does real work (encoding progress, a directory scan finishing)
calls touch() so the watchdog knows the process is actually alive and making
progress, not just technically still running. The watchdog thread itself
does nothing but sleep and compare timestamps -- deliberately kept free of
any file/DB/network access so the same kind of hang that freezes the worker
threads doesn't also freeze the thing meant to detect it.

If nothing has touched in over max_idle_seconds, the watchdog assumes the
process is truly stuck (not just idle -- normal idling still touches
periodically, see queue_manager.worker_loop) and calls os._exit() to kill it
outright. That's only useful paired with an external supervisor that
restarts main.py whenever it exits -- see supervisor.ps1."""
import logging
import os
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("aiconverter.heartbeat")

_lock = threading.Lock()
_last_touch = time.time()


def touch():
    global _last_touch
    with _lock:
        _last_touch = time.time()


def seconds_since_touch():
    with _lock:
        return time.time() - _last_touch


@contextmanager
def keepalive(interval_seconds=60):
    """For a single blocking call that can legitimately run far longer than
    the watchdog's idle threshold (e.g. single-threaded VMAF on a multi-hour
    file) but has no progress callback of its own to touch() from. Pings
    touch() on a timer for the duration of the `with` block so a merely-slow
    operation isn't mistaken for a hang. This does NOT itself detect whether
    that operation is stuck -- it only asserts "still waiting on it, as
    expected" -- so pair it with a subprocess-level timeout on the call
    itself wherever possible."""
    stop = threading.Event()

    def _ping():
        while not stop.wait(interval_seconds):
            touch()

    t = threading.Thread(target=_ping, name="heartbeat-keepalive", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


def start_watchdog(max_idle_seconds, check_interval_seconds=60):
    def _run():
        log.info("Watchdog started: max idle %ds, checking every %ds", max_idle_seconds, check_interval_seconds)
        while True:
            time.sleep(check_interval_seconds)
            idle = seconds_since_touch()
            if idle > max_idle_seconds:
                log.error(
                    "No activity for %.0fs (limit %ds) -- assuming the process is hung "
                    "(e.g. stuck on an unresponsive drive) and force-exiting so the "
                    "external supervisor can restart it.",
                    idle, max_idle_seconds,
                )
                os._exit(1)

    t = threading.Thread(target=_run, name="watchdog", daemon=True)
    t.start()
    return t
