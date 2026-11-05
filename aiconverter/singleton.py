"""Prevents two copies of the app from ever running at once. This has bitten
us twice: someone (or a Startup-folder relaunch) starts a second instance
while one's already running, and both end up racing on the same SQLite DB
and encoding different files at the same time, halving throughput and
occasionally corrupting shared state. An OS-level exclusive file lock is
used rather than just checking for an existing PID/process, since a PID
check has an unavoidable race (two processes can both check "is anyone
running?" and both see no, then both proceed) -- the OS lock is atomic."""
import logging
import msvcrt
import os

log = logging.getLogger("aiconverter.singleton")

from .config import CONFIG

# Next to the DB, so a separately-configured instance (a worker install, or a
# test instance with its own config) gets its own lock.
_LOCK_PATH = os.path.join(os.path.dirname(CONFIG["db_path"]), "aiconverter.lock")

_lock_file = None  # kept open for the process lifetime -- closing releases the lock


def acquire_or_exit():
    """Call once at startup. If another instance already holds the lock,
    logs why and exits immediately (clean exit code 0 -- this is not a
    crash, so the supervisor should NOT treat it as one to restart)."""
    global _lock_file
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    _lock_file = open(_LOCK_PATH, "w")
    try:
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error(
            "Another AIConverter instance already holds the lock at %s -- "
            "refusing to start a second one. Exiting cleanly (this is not a crash).",
            _LOCK_PATH,
        )
        print(f"Another AIConverter instance is already running (lock: {_LOCK_PATH}). Exiting.")
        raise SystemExit(0)
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()
    log.info("Acquired singleton lock (PID %d)", os.getpid())
