"""Walks watched directories looking for video files to queue. Runs
periodically (not just once) so directories can be dropped in once and files
added later get picked up automatically over the weeks this runs."""
import logging
import os
import threading

from . import db, filenames, heartbeat
from .config import CONFIG
from .ffmpeg_utils import probe, ProbeError

log = logging.getLogger("aiconverter.scanner")
_unreadable = {}  # path -> (size, mtime) of files that failed to probe

H264_NAMES = {"h264", "avc", "avc1"}


def true_case(path):
    """Return `path` with every folder/file name spelled exactly as it is on
    disk. Some network shares resolve names case-insensitively
    when READING, so a folder added as "a1" when it's really "A1" scans and
    encodes fine -- but CREATING a file through the wrong-case path fails with
    "The system cannot find the path specified", so every such job died at
    finalize. Anything that will later be written to must go
    through this first. Unresolvable parts are left as given."""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    current = drive.upper() + "\\"
    for part in [p for p in rest.split("\\") if p]:
        try:
            names = os.listdir(current)
        except OSError:
            return path
        if part not in names:
            match = next((n for n in names if n.lower() == part.lower()), None)
            if match is None:
                return path
            part = match
        current = os.path.join(current, part)
    return current


_case_cache = {}


def _true_case_dir(d):
    if d not in _case_cache:
        _case_cache[d] = true_case(d)
    return _case_cache[d]


def fix_path_case():
    """One-off repair (safe to re-run): correct the case of stored paths for
    watch dirs and every job that may still be written to. Returns the number
    of jobs corrected."""
    _case_cache.clear()
    for wd in db.list_watch_dirs():
        fixed = true_case(wd["path"])
        if fixed != wd["path"]:
            db.update_watch_dir_path(wd["id"], fixed)
            log.warning("Watch dir path case corrected: %s -> %s", wd["path"], fixed)
    n = 0
    for job in db.get_conn().execute("SELECT * FROM jobs WHERE status != 'purged'").fetchall():
        d = os.path.dirname(job["source_path"])
        fixed_dir = _true_case_dir(d)
        if fixed_dir == d:
            continue
        fields = {"source_path": os.path.join(fixed_dir, os.path.basename(job["source_path"]))}
        for f in ("out_path", "quarantine_path"):
            if job[f] and os.path.dirname(job[f]) == d:
                fields[f] = os.path.join(fixed_dir, os.path.basename(job[f]))
        db.update_job(job["id"], **fields)
        n += 1
    if n:
        log.warning("Corrected path case for %d jobs", n)
    return n


def _iter_video_files(root, recursive):
    exts = set(CONFIG["video_extensions"])
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() in exts and not name.endswith(CONFIG["quarantine_suffix"]):
                    yield os.path.join(dirpath, name)
    else:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in exts:
                yield full


def accepted_codecs(profile):
    profiles = CONFIG["codec_profiles"]
    return set(profiles.get(profile) or profiles["standard"])


def _probe_fields(info, codec):
    return dict(
        src_codec=codec,
        src_duration=info["duration"],
        src_size=info["size"],
        src_width=info["width"],
        src_height=info["height"],
        src_fps=info["fps"],
        src_bitrate=info["bitrate"],
        src_pix_fmt=info["pix_fmt"],
        src_color_primaries=info["color_primaries"],
        src_color_transfer=info["color_transfer"],
        src_color_space=info["color_space"],
        is_hdr=int(info["is_hdr"]),
    )


_scan_lock = threading.Lock()


def scan_directory(path, recursive=True, profile="standard", priority=0):
    """One scan at a time: adding a folder starts a scan while the periodic
    rescan may already be running -- two scans of the same folder at once
    raced to record the same files."""
    with _scan_lock:
        return _scan_directory(path, recursive, profile, priority)


def _scan_directory(path, recursive=True, profile="standard", priority=0):
    """Probe every video file under `path` and queue the ones whose format
    the folder's profile converts (see CONFIG["codec_profiles"]). Safe to call
    repeatedly -- already-known files are skipped instantly, except that a
    file skipped earlier for its format is queued if the folder's profile
    now includes that format."""
    accepted = accepted_codecs(profile)
    queued = 0
    requeued = 0
    skipped_existing = 0
    skipped_codec = 0
    errors = 0

    heartbeat.touch()
    for file_path in _iter_video_files(path, recursive):
        heartbeat.touch()
        if filenames.is_broken(file_path):
            # Emoji etc. in the name -- Windows/ffmpeg can't open it. Rename
            # it to a safe name and carry on with the new name; if that's
            # not possible, list it under Trouble files instead of failing
            # silently on every scan.
            new_path = filenames.repair(file_path)
            if new_path is None:
                if not db.get_job_by_source(file_path):
                    db.upsert_job(
                        file_path, status="bad_filename",
                        error_message="Filename contains characters (e.g. emoji) the network share can't "
                                      "handle, so it can't be read or renamed through the share. Add the "
                                      "share device's FTP login under ftp_rename in config.json and it will be "
                                      "renamed automatically, or rename the file by hand.",
                    )
                errors += 1
                continue
            old = db.get_job_by_source(file_path)
            if old:
                db.delete_job(old["id"])
            file_path = new_path

        existing = db.get_job_by_source(file_path)
        if existing:
            if (CONFIG["only_convert_h264"] and existing["status"] == "skipped_not_h264"
                    and (existing["src_codec"] or "").lower() in accepted):
                db.update_job(existing["id"], status="queued", priority=priority, attempts=0, error_message=None)
                requeued += 1
            else:
                skipped_existing += 1
            continue
        if db.is_output_of_job(file_path):
            continue  # our own converted output (e.g. x.avi -> x.mkv), not a new source

        # A file that can't be read (e.g. "moov atom not found" -- an
        # incomplete download) would otherwise be re-probed over the network
        # and logged on every scan. Remember it until its size/date changes.
        try:
            st = os.stat(file_path)
            sig = (st.st_size, st.st_mtime)
        except OSError:
            sig = None
        if sig is not None and _unreadable.get(file_path) == sig:
            errors += 1
            continue
        try:
            info = probe(file_path)
        except ProbeError as e:
            log.warning("Probe failed for %s (won't retry until the file changes): %s", file_path, e)
            if sig is not None:
                _unreadable[file_path] = sig
            errors += 1
            continue
        _unreadable.pop(file_path, None)

        codec = (info.get("codec") or "").lower()
        if CONFIG["only_convert_h264"] and codec not in accepted:
            db.upsert_job(file_path, status="skipped_not_h264", **_probe_fields(info, codec))
            skipped_codec += 1
            continue

        db.upsert_job(file_path, status="queued", priority=priority, **_probe_fields(info, codec))
        queued += 1

    log.info(
        "Scan of %s (%s) complete: %d queued, %d re-queued for new format profile, %d already known, %d format not selected, %d errors",
        path, profile, queued, requeued, skipped_existing, skipped_codec, errors,
    )
    return {"queued": queued, "requeued": requeued, "skipped_existing": skipped_existing,
            "skipped_codec": skipped_codec, "errors": errors}


def scan_all_watch_dirs():
    for wd in db.list_watch_dirs():
        try:
            scan_directory(wd["path"], recursive=bool(wd["recursive"]),
                           profile=wd["profile"] or "standard", priority=wd["priority"] or 0)
        except Exception:
            log.exception("Failed scanning watch dir %s", wd["path"])
        finally:
            db.touch_watch_dir(wd["id"])
