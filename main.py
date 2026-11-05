"""Entry point. Starts the queue worker (encodes one file at a time), a
periodic directory-rescan + quarantine-sweep thread, the web dashboard, and
a self-recovery watchdog.

Meant to run unattended for weeks: internal per-job errors are caught and
recorded against that job without stopping the process (see queue_manager).
If something truly unexpected crashes this process anyway, main() re-raises
after logging so the exit code is non-zero.

Restart-on-exit is provided by supervisor.ps1, an external loop that needs
no administrator rights -- run that
(not main.py directly) for real unattended operation. It restarts this
process whenever it exits, for any reason, including the watchdog below
deliberately exiting after detecting a hang (see aiconverter/heartbeat.py) --
e.g. a blocked file read against a flaky network/mounted drive can freeze a
thread forever with no exception to catch.
"""
import logging
import threading
import time

from aiconverter import db, folders, heartbeat, quarantine, queue_manager, scanner, singleton, web, workers
from aiconverter.config import CONFIG
from aiconverter.logging_setup import setup_logging

log = logging.getLogger("aiconverter.main")


def maintenance_loop():
    """Periodically re-scans watched directories for newly-added files and
    purges any quarantined originals whose grace period has elapsed."""
    interval = CONFIG["rescan_interval_minutes"] * 60
    while True:
        time.sleep(interval)
        heartbeat.touch()
        try:
            scanner.scan_all_watch_dirs()
        except Exception:
            log.exception("Periodic rescan failed")
        try:
            quarantine.sweep()
        except Exception:
            log.exception("Quarantine sweep failed")
        try:
            quarantine.reconcile_manual_deletions()
        except Exception:
            log.exception("Quarantine manual-deletion reconcile failed")
        try:
            queue_manager.sweep_work_dir()
        except Exception:
            log.exception("Work dir sweep failed")
        heartbeat.touch()


def main():
    setup_logging()
    log.info("AIConverter starting")
    singleton.acquire_or_exit()  # refuses to start a second instance -- see aiconverter/singleton.py
    db.init_db()
    heartbeat.touch()
    heartbeat.start_watchdog(CONFIG["watchdog_max_idle_minutes"] * 60)

    try:
        scanner.fix_path_case()
    except Exception:
        log.exception("Path case repair failed")
    scanner.scan_all_watch_dirs()
    queue_manager.start()
    threading.Thread(target=maintenance_loop, name="maintenance", daemon=True).start()
    threading.Thread(target=workers.reconcile_loop, name="worker-reconcile", daemon=True).start()
    threading.Thread(target=folders.refresh_loop, name="folder-counts", daemon=True).start()

    web.run()  # blocks


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("AIConverter crashed")
        raise
