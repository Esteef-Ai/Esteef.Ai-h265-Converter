"""Safety-net deletion. Instead of deleting (or moving elsewhere) an original
file the moment its replacement is verified, we rename it in place with a
trash suffix and only actually delete it after a configurable grace period.
Renaming in place is used deliberately -- it is instant even for huge files
(no data copy) since it stays on the same volume, and it means both the old
and new file are momentarily side-by-side in the source directory, which is
easy to inspect/recover from manually if anything looks wrong."""
import logging
import os
import time
from datetime import datetime, timedelta

from . import db
from .config import CONFIG

log = logging.getLogger("aiconverter.quarantine")


def trash_path_for(original_path):
    return original_path + CONFIG["quarantine_suffix"]


def expiry_from_now():
    return (datetime.now() + timedelta(days=CONFIG["quarantine_days"])).isoformat(timespec="seconds")


def quarantine_original(original_path):
    trash_path = trash_path_for(original_path)
    os.rename(original_path, trash_path)
    return trash_path, expiry_from_now()


def restore(trash_path):
    """Manual escape hatch: undo a quarantine before it's purged."""
    if not trash_path.endswith(CONFIG["quarantine_suffix"]):
        raise ValueError("Not a quarantine path")
    original_path = trash_path[: -len(CONFIG["quarantine_suffix"])]
    os.rename(trash_path, original_path)
    return original_path


def sweep():
    """Permanently delete quarantined originals whose grace period has
    elapsed. Called periodically from the main loop."""
    purged = 0
    for job in db.jobs_needing_purge():
        try:
            if job["quarantine_path"] and os.path.exists(job["quarantine_path"]):
                os.remove(job["quarantine_path"])
            db.update_job(job["id"], quarantine_path=None, quarantine_expires_at=None, status="purged")
            purged += 1
            log.info("Purged quarantined original for job %d (%s)", job["id"], job["filename"])
        except OSError:
            log.exception("Failed to purge quarantined file for job %d", job["id"])
    return purged


def purge_all():
    """Delete every quarantined original right now, regardless of how much
    of its grace period is left. Same bookkeeping as sweep(), just without
    the expiry filter."""
    purged = 0
    for job in db.quarantined_jobs_with_paths():
        try:
            if os.path.exists(job["quarantine_path"]):
                os.remove(job["quarantine_path"])
            db.update_job(job["id"], quarantine_path=None, quarantine_expires_at=None, status="purged")
            purged += 1
        except OSError:
            log.exception("Failed to purge quarantined file for job %d", job["id"])
    return purged


def reconcile_manual_deletions():
    """A user can delete a .aiconverter-trash file by hand instead of waiting
    for the automatic sweep. When that happens the job would otherwise sit
    forever in the dashboard's main list still claiming to be quarantined.
    Detect the missing file and finish the same bookkeeping sweep() would
    have done, immediately, so the job drops off the main list right away
    instead of waiting out the rest of the grace period."""
    reconciled = 0
    missing = [j for j in db.quarantined_jobs_with_paths() if not os.path.exists(j["quarantine_path"])]
    if missing:
        # H: can briefly stop responding, making a present file look missing.
        # Only trust "missing" if it's still missing a moment later.
        time.sleep(10)
    for job in missing:
        # If the whole folder is gone, the disk/share is unavailable (e.g.
        # unplugged or moved) -- that is NOT a manual delete. Only a missing file in a present folder counts.
        if not os.path.isdir(os.path.dirname(job["quarantine_path"])):
            continue
        if not os.path.exists(job["quarantine_path"]):
            db.update_job(job["id"], quarantine_path=None, quarantine_expires_at=None, status="purged")
            reconciled += 1
            log.info("Job %d (%s): quarantine file was deleted manually, marked purged", job["id"], job["filename"])
    return reconciled
