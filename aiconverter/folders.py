"""The Folders page: every folder in the library, and where each one stands --
never processed, being watched, or finished in an earlier run -- so nothing
gets missed as work is added folder by folder.

Two sources are combined:
  - the jobs table, for what the converter has done with each folder's files;
  - a background walk of the disk (cached, refreshed every 15 minutes or on
    demand) for what's actually there, so files the converter has never seen
    -- in a folder it's never processed, or added since a folder was finished
    -- show up as "not yet seen".
"""
import logging
import os
import threading
import time

from . import db
from .config import CONFIG

log = logging.getLogger("aiconverter.folders")

LEGACY_EXTS = {".wmv", ".avi", ".mpg", ".mpeg"}
LEGACY_CODECS = {"mpeg1video", "vc1", "mpeg4"}
REENCODED = {"done", "purged"}
PENDING = {"queued", "probing", "encoding", "verifying", "finalizing"}
TROUBLE = {"failed", "verification_failed", "low_disk_space", "bad_filename"}

_cache = {"at": 0, "data": {}, "running": False}
_lock = threading.Lock()


def library_roots():
    """Folders whose subfolders are listed: anything in config
    "library_roots", plus the parent of every watched folder -- so the page
    follows the library automatically if its drive letter changes."""
    roots = {}
    for r in CONFIG.get("library_roots") or []:
        roots[_norm(r)] = r
    for wd in db.list_watch_dirs():
        parent = os.path.dirname(wd["path"].rstrip("\\"))
        if parent and os.path.splitdrive(parent)[1] not in ("", "\\"):
            roots.setdefault(_norm(parent), parent)
    return [r for r in roots.values() if os.path.isdir(r)]


def _norm(p):
    return p.rstrip("\\").lower()


def _candidate_folders():
    """Top-level folders under each library root, plus every watched folder
    (which may live elsewhere)."""
    seen = {}
    for root in library_roots():
        try:
            for name in sorted(os.listdir(root), key=str.lower):
                full = os.path.join(root, name)
                if os.path.isdir(full) and not name.startswith(("$", ".")) and name != "System Volume Information":
                    seen[_norm(full)] = full
        except OSError:
            log.warning("Library root %s not reachable", root)
    for wd in db.list_watch_dirs():
        seen.setdefault(_norm(wd["path"]), wd["path"])
    return seen


def _walk_counts(folders, known_paths):
    exts = set(CONFIG["video_extensions"])
    out = {}
    for key, path in folders.items():
        n = size = unseen = unseen_size = legacy = 0
        for dirpath, _dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in exts or f.endswith(CONFIG["quarantine_suffix"]):
                    continue
                full = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    continue
                n += 1
                size += sz
                if ext in LEGACY_EXTS:
                    legacy += 1
                if full.lower() not in known_paths:
                    unseen += 1
                    unseen_size += sz
        out[key] = {"files": n, "size": size, "unseen": unseen, "unseen_size": unseen_size, "legacy_ext": legacy}
    return out


def refresh_disk_counts(block=False):
    def _run():
        try:
            folders = _candidate_folders()
            known = {r[0].lower() for r in db.get_conn().execute("SELECT source_path FROM jobs")}
            known |= {r[0].lower() for r in db.get_conn().execute("SELECT out_path FROM jobs WHERE out_path IS NOT NULL")}
            t0 = time.time()
            data = _walk_counts(folders, known)
            with _lock:
                _cache.update(at=time.time(), data=data)
            log.info("Folder disk counts refreshed in %.0fs", time.time() - t0)
        except Exception:
            log.exception("Folder disk count refresh failed")
        finally:
            with _lock:
                _cache["running"] = False

    with _lock:
        if _cache["running"]:
            return
        _cache["running"] = True
    if block:
        _run()
    else:
        threading.Thread(target=_run, name="folder-counts", daemon=True).start()


def refresh_loop():
    while True:
        refresh_disk_counts(block=True)
        time.sleep(15 * 60)


def rows():
    folders = _candidate_folders()
    watched = {_norm(wd["path"]): wd for wd in db.list_watch_dirs()}
    jobs = db.get_conn().execute("SELECT source_path, status, src_size, out_size, src_codec FROM jobs").fetchall()
    with _lock:
        disk, disk_at = dict(_cache["data"]), _cache["at"]

    keys = sorted(folders, key=lambda k: (k not in watched, k))
    stats = {k: {"jobs": 0, "reencoded": 0, "left_alone": 0, "pending": 0, "trouble": 0, "skipped_format": 0,
                 "legacy_skipped": 0, "src": 0, "saved": 0} for k in keys}
    # longest matching folder wins (a watched subfolder inside a root folder)
    order = sorted(keys, key=len, reverse=True)
    for j in jobs:
        sp = j["source_path"].lower()
        k = next((k for k in order if sp.startswith(k + "\\")), None)
        if k is None:
            continue
        s = stats[k]
        s["jobs"] += 1
        s["src"] += j["src_size"] or 0
        st = j["status"]
        if st in REENCODED:
            s["reencoded"] += 1
            s["saved"] += (j["src_size"] or 0) - (j["out_size"] or 0)
        elif st == "no_size_gain":
            s["left_alone"] += 1
        elif st in PENDING:
            s["pending"] += 1
        elif st in TROUBLE:
            s["trouble"] += 1
        elif st == "skipped_not_h264":
            s["skipped_format"] += 1
            if (j["src_codec"] or "").lower() in LEGACY_CODECS:
                s["legacy_skipped"] += 1

    out = []
    for k in keys:
        s = stats[k]
        w = watched.get(k)
        d = disk.get(k)
        if w is not None:
            state = "processing" if s["pending"] else "watching"
        elif s["jobs"]:
            state = "done_earlier"
        else:
            state = "never"
        out.append({"key": k, "path": folders[k], "state": state, "watch": w, "disk": d, **s})
    return out, disk_at, _cache["running"]
