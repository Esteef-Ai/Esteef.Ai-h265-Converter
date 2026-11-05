"""Repair video filenames Windows can't handle (emoji etc.) by renaming them.

Some files on a NAS/router share can have emoji in their names (characters
outside the Basic Multilingual Plane, e.g. U+1F525). Some SMB servers list
them, but rejects every operation on that name -- stat, open, even
rename -- with WinError 123, so ffprobe/ffmpeg can never read them.

Renaming is tried in two ways:
  1. a normal rename through the share (works on ordinary Windows/NAS drives)
  2. a rename over the device's FTP server, which accepts these names, if FTP
     credentials are filled in under "ftp_rename" in config.json. The FTP
     folder matching each share is discovered automatically.
"""
import ftplib
import logging
import os
import posixpath
import threading
import time

from .config import CONFIG

log = logging.getLogger("aiconverter.filenames")

_WIN_RESERVED = set('<>:"/\\|?*')
_WIN_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name):
    """Windows-safe version of a bare filename (extension preserved)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    out = []
    for ch in stem:
        cp = ord(ch)
        if cp < 32 or cp == 127:
            continue  # control chars
        if cp > 0xFFFF:
            continue  # emoji / non-BMP
        if ch in _WIN_RESERVED:
            continue
        if ch in "\u2013\u2014\u2015":
            out.append("-")
            continue
        if ch in "\u2018\u2019":
            out.append("'")
            continue
        if ch in "\u201C\u201D":
            out.append("'")
            continue
        if 0x2190 <= cp <= 0x2BFF:
            continue  # arrows/symbols/dingbats
        if 0xFE00 <= cp <= 0xFE0F:
            continue  # variation selectors
        out.append(ch)
    clean = " ".join("".join(out).split()).rstrip(". ")
    if not clean:
        clean = "video"
    if clean.upper() in _WIN_RESERVED_NAMES:
        clean = "_" + clean
    return f"{clean}.{ext}" if ext else clean


def is_broken(path):
    """True if Windows can't work with this file under its current name.
    Only names that actually fail are touched -- accented letters, curly
    quotes etc. that read fine are left alone."""
    name = os.path.basename(path)
    if name.isascii():
        return False
    if any(ord(ch) > 0xFFFF for ch in name):
        return True
    try:
        os.stat(path)
        return False
    except OSError as e:
        return getattr(e, "winerror", None) == 123


def _free_target(directory, new_name, exists):
    stem, dot, ext = new_name.rpartition(".")
    if not dot:
        stem, ext = new_name, ""
    candidate, n = new_name, 1
    while exists(candidate):
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        n += 1
    return candidate


def repair(path):
    """Rename a broken file to a safe name. Returns the new path, or None if
    it couldn't be renamed (the reason is logged)."""
    directory, name = os.path.split(path)
    new_name = sanitize_filename(name)
    if new_name == name:
        return None
    new_name = _free_target(directory, new_name, lambda n: os.path.exists(os.path.join(directory, n)))
    new_path = os.path.join(directory, new_name)

    try:
        os.rename(path, new_path)
        log.warning("Renamed unsafe filename: %s -> %s", ascii(name), new_name)
        return new_path
    except OSError as e:
        smb_error = e

    if _ftp_configured():
        try:
            if _ftp_rename(path, new_name):
                log.warning("Renamed unsafe filename via FTP: %s -> %s", ascii(name), new_name)
                # Windows caches SMB directory listings for several seconds,
                # so the new name isn't visible through H: straight away --
                # probing it immediately fails with "No such file".
                for _ in range(30):
                    if os.path.exists(new_path):
                        break
                    time.sleep(1)
                return new_path
        except Exception:
            log.exception("FTP rename failed for %s", ascii(name))
    else:
        log.warning("Can't rename %s through the share (%s) and no FTP login is configured", ascii(name), smb_error)
    return None


# --- FTP fallback -------------------------------------------------------------

_ftp_lock = threading.Lock()
_ftp_roots = {}  # "\\host\share" (lower) -> ftp directory that holds the same files


def _ftp_cfg():
    return CONFIG.get("ftp_rename") or {}


def _ftp_configured():
    c = _ftp_cfg()
    return bool(c.get("user") and c.get("password"))


def _unc(path):
    from .workers import to_worker_path  # local import: workers imports queue_manager
    return to_worker_path(path) if not path.startswith("\\\\") else path


def _split_unc(unc):
    parts = unc.lstrip("\\").split("\\")
    return parts[0], parts[1], parts[2:]  # host, share, [dirs..., name]


def _ftp_exists_dir(ftp, d):
    try:
        ftp.cwd(d)
        return True
    except ftplib.all_errors:
        return False


def _find_root(ftp, rel_dirs):
    """Find the FTP directory where the share's top-level folders live, by
    looking for the file's first folder under / and up to two levels down."""
    probe = "/".join(rel_dirs[:1]) if rel_dirs else ""
    candidates = ["/"]
    for base in ("/",):
        try:
            for entry in ftp.nlst(base):
                top = posixpath.join("/", posixpath.basename(entry.rstrip("/")))
                candidates.append(top)
                try:
                    for sub in ftp.nlst(top):
                        candidates.append(posixpath.join(top, posixpath.basename(sub.rstrip("/"))))
                except ftplib.all_errors:
                    pass
        except ftplib.all_errors:
            pass
    for c in candidates:
        if not probe or _ftp_exists_dir(ftp, posixpath.join(c, probe)):
            return c
    return None


def _ftp_rename(path, new_name):
    unc = _unc(path)
    if not unc:
        log.warning("No network-share mapping for %s -- can't use FTP", ascii(path))
        return False
    host, share, rest = _split_unc(unc)
    rel_dirs, old_name = rest[:-1], rest[-1]
    cfg = _ftp_cfg()
    with _ftp_lock:
        ftp = ftplib.FTP()
        ftp.encoding = "utf-8"
        ftp.connect(cfg.get("host") or host, int(cfg.get("port") or 21), timeout=30)
        try:
            ftp.login(cfg["user"], cfg["password"])
            key = f"\\\\{host}\\{share}".lower()
            root = (cfg.get("share_roots") or {}).get(share.lower()) or _ftp_roots.get(key)
            if not root:
                root = _find_root(ftp, rel_dirs)
                if not root:
                    log.warning("Couldn't find the FTP folder for share %s", key)
                    return False
                _ftp_roots[key] = root
                log.info("FTP folder for %s is %s", key, root)
            folder = posixpath.join(root, *rel_dirs)
            ftp.cwd(folder)
            if old_name not in ftp.nlst():
                log.warning("FTP listing of %s doesn't show %s", folder, ascii(old_name))
                return False
            ftp.rename(old_name, new_name)
            return True
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                pass
