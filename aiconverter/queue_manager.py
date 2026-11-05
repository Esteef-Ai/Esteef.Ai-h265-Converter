"""The actual worker loop: pulls one queued job at a time, encodes it locally
on C:, verifies the result, then finalizes it into the source directory and
quarantines the original. Designed to survive being killed and restarted at
any point -- see resume_incomplete_jobs()."""
import logging
import os
import shutil
import socket
import threading
import time

from . import control, db, filenames, quarantine, heartbeat
from .config import CONFIG
from .ffmpeg_utils import probe, encode, decode_check, vmaf_compare, ProbeError, NoSizeGainDetected

log = logging.getLogger("aiconverter.queue")

_stop_event = threading.Event()
_current_job_id = None
_current_progress = 0.0
_current_speed = None
_current_eta_seconds = None

# Name this PC records against jobs it claims (see db.claim_next_job).
LOCAL_NAME = socket.gethostname().upper()


class LocalStore:
    """Where process_job() records job state. On the master that's the
    SQLite DB directly; a worker swaps in worker.RemoteStore, which sends
    the same updates to the master over HTTP."""
    def update_job(self, job_id, **fields):
        db.update_job(job_id, **fields)


_store = LocalStore()


def set_store(store):
    global _store
    _store = store


def _final_extension(source_path):
    ext = os.path.splitext(source_path)[1].lower()
    return CONFIG["container_fallback"].get(ext, ext)


def _job_work_dir(job_id):
    d = os.path.join(CONFIG["work_dir"], f"job_{job_id}")
    os.makedirs(d, exist_ok=True)
    return d


def _free_gb(path):
    drive_root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    return shutil.disk_usage(drive_root).free / (1024 ** 3)


def resume_incomplete_jobs():
    """Called once at startup. Any job crashed mid-encode is simply retried
    from scratch (source untouched at that point). Any job crashed mid
    finalize (rarest window: original already quarantined, new file present
    under a .h265tmp name) gets its rename completed or is flagged for
    manual review -- it is never left half-done silently."""
    db.reset_stuck_jobs(LOCAL_NAME)
    conn = db.get_conn()
    stuck = conn.execute(
        "SELECT * FROM jobs WHERE status='finalizing' AND (worker IS NULL OR worker=?)", (LOCAL_NAME,)
    ).fetchall()
    for job in stuck:
        recover_finalizing(job)


def recover_finalizing(job):
    """Finish (or undo) a job whose process died during finalize. Also used
    by the master for a remote worker that went offline mid-finalize -- the
    files live on the shared drive, so the master can see them too.

    Finalize order is: copy output to <final>.h265tmp -> rename original to
    the quarantine name -> rename .h265tmp to <final>. The quarantine file is
    what tells us which side of that line the crash happened on: the final
    name is usually the SAME as the source name, so "final file exists" on
    its own can't distinguish "finished" from "never got going"."""
    source_path = job["source_path"]
    dest_dir = os.path.dirname(source_path)
    final_ext = _final_extension(source_path)
    final_name = os.path.splitext(os.path.basename(source_path))[0] + final_ext
    final_path = os.path.join(dest_dir, final_name)
    tmp_path = final_path + ".h265tmp"
    trash_path = quarantine.trash_path_for(source_path)

    if not os.path.exists(trash_path):
        # Original never got quarantined -- it's untouched. Throw away any
        # half-copied output and just run the job again.
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        db.update_job(job["id"], status="queued", worker=None, progress_pct=0,
                      error_message="Interrupted before finalize completed; original untouched, requeued")
        log.warning("Job %d: interrupted before the original was touched -- requeued", job["id"])
        return

    expires_at = job["quarantine_expires_at"] or quarantine.expiry_from_now()
    if not os.path.exists(final_path) and os.path.exists(tmp_path):
        os.rename(tmp_path, final_path)
        log.warning("Recovered job %d: completed interrupted finalize rename", job["id"])
    if os.path.exists(final_path):
        db.update_job(job["id"], status="done", out_path=final_path, out_size=os.path.getsize(final_path),
                      quarantine_path=trash_path, quarantine_expires_at=expires_at,
                      finished_at=db.now(), error_message=None)
        log.info("Recovered job %d: finalize completed", job["id"])
    else:
        db.update_job(
            job["id"], status="failed", quarantine_path=trash_path, quarantine_expires_at=expires_at,
            error_message="Crashed mid-finalize and neither the final file nor the .h265tmp "
                          "file could be found afterward. Original is in quarantine -- "
                          "use Restore original before it expires.",
        )
        log.error("Job %d needs manual review: finalize state unrecoverable", job["id"])


def _set_progress(job_id, pct, speed, remaining_video_seconds):
    global _current_progress, _current_speed, _current_eta_seconds
    _current_progress = pct
    _current_speed = speed
    _current_eta_seconds = int(remaining_video_seconds / speed) if speed else None
    _store.update_job(job_id, progress_pct=pct)


def process_job(job):
    global _current_job_id
    job_id = job["id"]
    _current_job_id = job_id
    source_path = job["source_path"]

    if not os.path.isdir(os.path.dirname(source_path)):
        # Folder itself unreachable: the disk/share is missing (unplugged,
        # moved, network down) -- not a deleted file. Put the job back and
        # wait instead of failing it.
        _store.update_job(job_id, status="queued", worker=None, progress_pct=0,
                          error_message="Waiting: source folder unavailable (disk or share missing)")
        log.warning("Job %d: folder %s unavailable -- requeued, pausing 2 min", job_id, os.path.dirname(source_path))
        time.sleep(120)
        return
    if not os.path.exists(source_path):
        _store.update_job(job_id, status="failed", error_message="Source file no longer exists")
        return

    if _free_gb(CONFIG["work_dir"]) < CONFIG["min_free_c_drive_gb"]:
        _store.update_job(job_id, status="low_disk_space", error_message="Not enough free space on C: to start encode")
        log.warning("Pausing job %d: C: drive below configured free-space floor", job_id)
        return

    work_dir = _job_work_dir(job_id)
    final_ext = _final_extension(source_path)
    work_output = os.path.join(work_dir, "output" + final_ext)

    _store.update_job(job_id, status="probing", started_at=db.now())
    try:
        src_info = probe(source_path)
    except ProbeError as e:
        _fail_or_retry(job, f"Probe failed: {e}")
        return

    # Encode and verify from a LOCAL copy of the source, not straight off the
    # network share. Consumer shares can drop reads under load, and
    # ffmpeg doesn't stop on a failed read -- it skips ahead in the file and
    # carries on, producing an output with big gaps. One
    # sequential copy, checked against the original's size, also replaces
    # two long network reads (encode + VMAF) with one.
    local_src = os.path.join(work_dir, "source" + os.path.splitext(source_path)[1].lower())
    need_gb = 2 * os.path.getsize(source_path) / 1024 ** 3 + CONFIG["min_free_c_drive_gb"]
    if _free_gb(work_dir) < need_gb:
        _store.update_job(job_id, status="low_disk_space",
                          error_message=f"Not enough free space on the local drive to copy and encode this file (needs ~{need_gb:.0f} GB)")
        _cleanup_dir(work_dir)
        return
    try:
        _copy_local(source_path, local_src)
    except Exception as e:
        _cleanup_dir(work_dir)
        _fail_or_retry(job, f"Copying source to local disk failed: {e}")
        return

    effective_crf = job["crf"] if job["crf"] is not None else CONFIG["crf"]
    _store.update_job(job_id, status="encoding", progress_pct=0, crf=effective_crf)
    src_duration = src_info.get("duration") or 0

    def _on_progress(pct, speed):
        heartbeat.touch()
        remaining_video_seconds = src_duration * (1 - pct / 100)
        _set_progress(job_id, pct, speed, remaining_video_seconds)

    try:
        encode(
            local_src, work_output, src_info,
            on_progress=_on_progress,
            stall_timeout_s=CONFIG["stall_timeout_minutes"] * 60,
            crf=effective_crf,
            source_size=src_info["size"],
        )
    except NoSizeGainDetected as e:
        # Projected, partway through, that this was never going to be
        # smaller than the source -- bail out now instead of burning the
        # rest of the encode time on a result that would just get discarded
        # by the post-encode size check anyway. Original is never touched.
        _store.update_job(
            job_id, status="no_size_gain",
            error_message=f"Aborted at {e.at_pct:.0f}% through the CRF {effective_crf} encode: "
                          f"projected {e.projected_size} bytes vs {e.source_size} byte source "
                          f"(no space would have been saved) -- original left untouched",
        )
        _cleanup_dir(work_dir)
        log.warning(
            "Job %d: aborted encode early at %.0f%% -- projected %d bytes vs %d byte source, no gain",
            job_id, e.at_pct, e.projected_size, e.source_size,
        )
        return
    except Exception as e:
        _fail_or_retry(job, f"Encode failed: {e}")
        _cleanup_dir(work_dir)
        return
    finally:
        global _current_speed, _current_eta_seconds
        _current_speed = None
        _current_eta_seconds = None

    _store.update_job(job_id, status="verifying", progress_pct=100)
    try:
        ok, err = decode_check(work_output)
        if not ok:
            raise RuntimeError(f"Decode check failed on encoded output: {err[-500:]}")

        out_info = probe(work_output)
        duration_diff = abs((out_info["duration"] or 0) - (src_info["duration"] or 0))
        if out_info["width"] != src_info["width"] or out_info["height"] != src_info["height"]:
            raise RuntimeError(
                f"Resolution mismatch: source {src_info['width']}x{src_info['height']} "
                f"vs output {out_info['width']}x{out_info['height']}"
            )
        if duration_diff > CONFIG["max_duration_diff_seconds"]:
            raise RuntimeError(f"Duration mismatch: {duration_diff:.2f}s (source {src_info['duration']:.2f}s)")

        with heartbeat.keepalive():
            vmaf_score = vmaf_compare(local_src, work_output, src_info["width"], src_info["height"])
        if vmaf_score < CONFIG["min_vmaf_score"]:
            raise RuntimeError(f"VMAF score {vmaf_score:.2f} below required {CONFIG['min_vmaf_score']}")

        # CRF targets a quality level, not a file size -- it does NOT guarantee
        # the output is smaller than the source. Grainy/noisy or already
        # efficiently-encoded sources can occasionally need more bits at a
        # given quality bar than the original used. Since the entire point of
        # this tool is saving space, a file that wouldn't get smaller is left
        # alone rather than replaced with something bigger.
        out_size_check = os.path.getsize(work_output)
        if out_size_check >= src_info["size"]:
            _store.update_job(
                job_id, status="no_size_gain", vmaf_score=vmaf_score, duration_diff=duration_diff,
                out_size=out_size_check,
                error_message=f"Re-encode at CRF {effective_crf} came out {out_size_check} bytes vs "
                              f"{src_info['size']} bytes source (no space saved) -- original left untouched",
            )
            _cleanup_dir(work_dir)
            log.warning(
                "Job %d: CRF %d re-encode is not smaller than source (%d vs %d bytes) -- leaving original untouched",
                job_id, effective_crf, out_size_check, src_info["size"],
            )
            return
    except Exception as e:
        out_size = os.path.getsize(work_output) if os.path.exists(work_output) else None
        attempts = (job["attempts"] or 0) + 1
        size_ratio = (out_size / src_info["size"]) if out_size and src_info.get("size") else 0
        # Only a low VMAF score is a quality problem that a lower CRF can fix.
        # A wrong duration / resolution or a decode error means the encode
        # itself was broken -- typically the source read was cut off (e.g.
        # the network disk was switched mid-encode) -- so retry
        # at the SAME CRF, and don't judge it by its (truncated) size.
        quality_failure = "VMAF score" in str(e)
        if quality_failure and attempts < CONFIG["max_attempts"] and size_ratio > CONFIG["verify_retry_max_size_ratio"]:
            # A lower CRF means a bigger file; this one is already close to
            # the source size, so a retry can't save space. Leave it alone.
            _store.update_job(
                job_id, status="no_size_gain", attempts=attempts, out_size=out_size,
                error_message=f"Verification failed at CRF {effective_crf} ({e}) and the output was already "
                              f"{100 * size_ratio:.0f}% of the source size -- a higher-quality retry would not "
                              f"be smaller, so the original was left untouched",
            )
            _cleanup_dir(work_dir)
            log.warning("Job %d: failed verification at CRF %d with output at %.0f%% of source -- skipping retry, no gain",
                        job_id, effective_crf, 100 * size_ratio)
            return
        if attempts < CONFIG["max_attempts"]:
            retry_crf = max(effective_crf - CONFIG["verify_retry_crf_step"], 0) if quality_failure else effective_crf
            _store.update_job(
                job_id, status="queued", attempts=attempts, crf=retry_crf, progress_pct=0,
                out_size=out_size,
                error_message=f"Verification failed at CRF {effective_crf} ({e}); "
                              f"retrying at CRF {retry_crf} (attempt {attempts + 1}/{CONFIG['max_attempts']})",
            )
            log.warning(
                "Job %d failed verification at CRF %d (%s) -- retrying at CRF %d (attempt %d/%d)",
                job_id, effective_crf, e, retry_crf, attempts + 1, CONFIG["max_attempts"],
            )
            _cleanup_dir(work_dir)
        else:
            _store.update_job(
                job_id, status="verification_failed", attempts=attempts, out_size=out_size,
                error_message=f"Verification failed at CRF {effective_crf} after {attempts} attempts: {e}",
            )
            _quarantine_failed_output(work_dir, job_id)
            log.error("Job %d permanently failed verification after %d attempts: %s", job_id, attempts, e)
        return

    _store.update_job(job_id, status="finalizing", vmaf_score=vmaf_score, duration_diff=duration_diff)
    try:
        if _finalize_with_retries(job, source_path, work_output, final_ext):
            log.info("Job %d complete: %s (VMAF %.1f)", job_id, os.path.basename(source_path), vmaf_score)
    finally:
        _cleanup_dir(work_dir)


FINALIZE_TRIES = 4
SHARE_WAIT_SECONDS = 2 * 60 * 60


def _wait_for_folder(job_id, folder, max_seconds):
    """Wait for a network folder to come back. True if it's reachable."""
    waited = 0
    while not os.path.isdir(folder):
        if waited >= max_seconds or control.abort_requested():
            return False
        if waited % 600 == 0:
            log.warning("Job %d: %s unreachable -- holding the verified encode and waiting for the share", job_id, folder)
        heartbeat.touch()
        time.sleep(30)
        waited += 30
    return True


def _finalize_with_retries(job, source_path, work_output, final_ext):
    """Swap the verified encode in, riding out share problems.

    Network shares drop out (WinError 53/59, "Invalid Signature") and files
    get held open (WinError 32). The verified output is
    still on the local disk, so instead of failing: wait for the share,
    look at what actually happened (the quarantine file is the truth --
    "original missing" just means the share was down), and try again."""
    job_id = job["id"]
    dest_dir = os.path.dirname(source_path)
    trash_path = quarantine.trash_path_for(source_path)
    err = None
    for attempt in range(1, FINALIZE_TRIES + 1):
        try:
            _finalize(job_id, source_path, work_output, final_ext)
            return True
        except Exception as e:
            err = e
            log.warning("Job %d finalize attempt %d/%d failed: %s", job_id, attempt, FINALIZE_TRIES, e)
        if not _wait_for_folder(job_id, dest_dir, SHARE_WAIT_SECONDS):
            break
        time.sleep(15)  # let a just-returned share settle before judging it
        try:
            if os.path.exists(trash_path):
                # The original did get moved aside; only the last step failed.
                return _complete_after_quarantine(job_id, source_path, work_output, final_ext, trash_path)
        except Exception as e:
            err = e
            log.warning("Job %d: completing finalize failed: %s", job_id, e)
            continue
        if attempt < FINALIZE_TRIES:
            heartbeat.touch()
            time.sleep(60)

    # Out of tries. Decide from what's actually on disk, never from a share
    # that isn't answering.
    if not os.path.isdir(dest_dir):
        _store.update_job(job_id, status="queued", worker=None, progress_pct=0,
                          error_message=f"Waiting: share unavailable while finishing ({err}); original untouched")
        log.warning("Job %d: share still unavailable at finalize -- requeued", job_id)
    elif os.path.exists(trash_path):
        _store.update_job(job_id, status="failed", quarantine_path=trash_path,
                          quarantine_expires_at=quarantine.expiry_from_now(),
                          error_message=f"Finalize failed after the original was moved to quarantine -- "
                                        f"use Restore original: {err}")
        log.error("Job %d needs manual review: original quarantined but finalize incomplete: %s", job_id, err)
    else:
        attempts = (job["attempts"] or 0) + 1
        if attempts < CONFIG["max_attempts"]:
            _store.update_job(job_id, status="queued", attempts=attempts, progress_pct=0,
                              error_message=f"Finalize failed ({err}); original untouched, retrying "
                                            f"(attempt {attempts + 1}/{CONFIG['max_attempts']})")
            log.warning("Job %d finalize failed (%s) -- requeued, attempt %d/%d", job_id, err, attempts + 1, CONFIG["max_attempts"])
        else:
            _store.update_job(job_id, status="failed", attempts=attempts,
                              error_message=f"Finalize failed after {attempts} attempts (original untouched): {err}")
            log.error("Job %d permanently failed at finalize after %d attempts: %s", job_id, attempts, err)
    return False


def _complete_after_quarantine(job_id, source_path, work_output, final_ext, trash_path):
    """The original is already at its quarantine name; put the verified
    encode in place under the final name."""
    dest_dir = os.path.dirname(source_path)
    final_path = os.path.join(dest_dir, os.path.splitext(os.path.basename(source_path))[0] + final_ext)
    tmp_path = final_path + ".h265tmp"
    out_size = os.path.getsize(work_output)
    if os.path.exists(final_path):
        if os.path.getsize(final_path) != out_size:
            raise RuntimeError(f"{final_path} exists but isn't the verified encode ({os.path.getsize(final_path)} vs {out_size} bytes)")
    else:
        if not (os.path.exists(tmp_path) and os.path.getsize(tmp_path) == out_size):
            _remove_quietly(tmp_path)
            shutil.copy2(work_output, tmp_path)
            if os.path.getsize(tmp_path) != out_size:
                raise RuntimeError("Copy to destination directory produced a different file size than expected")
        os.rename(tmp_path, final_path)
    _store.update_job(
        job_id, status="done", out_path=final_path, out_size=out_size, error_message=None,
        quarantine_path=trash_path, quarantine_expires_at=quarantine.expiry_from_now(), finished_at=db.now(),
    )
    log.warning("Job %d: finished an interrupted finalize after the share recovered", job_id)
    return True


def _finalize(job_id, source_path, work_output, final_ext):
    dest_dir = os.path.dirname(source_path)
    final_name = os.path.splitext(os.path.basename(source_path))[0] + final_ext
    final_path = os.path.join(dest_dir, final_name)
    tmp_path = final_path + ".h265tmp"

    out_size = os.path.getsize(work_output)
    required_gb = (out_size / (1024 ** 3)) + CONFIG["dest_free_space_buffer_gb"]
    if _free_gb(dest_dir) < required_gb:
        raise RuntimeError(f"Not enough free space in destination {dest_dir} for finalized file")

    shutil.copy2(work_output, tmp_path)
    if os.path.getsize(tmp_path) != out_size:
        os.remove(tmp_path)
        raise RuntimeError("Copy to destination directory produced a different file size than expected")

    # Renaming the original fails with "file in use" (WinError 32) while
    # anything else has it open -- a media server, a player, a tagger. That's
    # usually momentary, so keep trying for a few minutes rather than
    # throwing away a finished, verified encode.
    for attempt in range(1, 10):
        try:
            trash_path, expires_at = quarantine.quarantine_original(source_path)
            break
        except PermissionError as e:
            if attempt == 9:
                # Still held open over SMB (often a media server). The
                # share device's FTP server, if set up, isn't bound by SMB share locks, and a
                # rename doesn't disturb a program that only has it open to read.
                if filenames._ftp_configured():
                    try:
                        trash_path = quarantine.trash_path_for(source_path)
                        if filenames._ftp_rename(source_path, os.path.basename(trash_path)):
                            for _ in range(20):  # SMB's view of the folder lags behind
                                if os.path.exists(trash_path):
                                    break
                                time.sleep(3)
                            if os.path.exists(trash_path):
                                log.warning("Job %d: original was locked over SMB -- quarantined it via FTP", job_id)
                                expires_at = quarantine.expiry_from_now()
                                break
                    except Exception as fe:
                        log.warning("Job %d: FTP rename fallback failed: %s", job_id, fe)
                _remove_quietly(tmp_path)
                raise RuntimeError(f"Original stayed in use by another program for ~4 minutes: {e}")
            log.warning("Job %d: original is in use by another program, retrying in 30s (%d/9)", job_id, attempt)
            heartbeat.touch()
            time.sleep(30)
        except Exception:
            _remove_quietly(tmp_path)
            raise
    os.rename(tmp_path, final_path)

    _store.update_job(
        job_id, status="done", out_path=final_path, out_size=out_size,
        quarantine_path=trash_path, quarantine_expires_at=expires_at, finished_at=db.now(),
    )


def _copy_local(src, dst, attempts=3):
    """Sequential copy of the source to the local work dir, verified against
    the original's size, with retries. Honours pause (waits) and stop."""
    expected = os.path.getsize(src)
    err = None
    for attempt in range(1, attempts + 1):
        try:
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    while control.is_frozen():
                        heartbeat.touch()
                        time.sleep(1)
                    if control.abort_requested():
                        raise RuntimeError("stopped by user")
                    chunk = fi.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    fo.write(chunk)
                    heartbeat.touch()
            got = os.path.getsize(dst)
            if got == expected:
                return
            err = f"copied {got} of {expected} bytes"
        except OSError as e:
            err = str(e)
        if control.abort_requested():
            raise RuntimeError("stopped by user")
        log.warning("Copy of %s failed (%s), attempt %d/%d", src, err, attempt, attempts)
        if attempt < attempts:
            time.sleep(30)
    raise RuntimeError(err)


def _quarantine_failed_output(work_dir, job_id):
    # the local source copy is only scratch -- don't keep GBs of it around
    for name in os.listdir(work_dir) if os.path.isdir(work_dir) else []:
        if name.startswith("source."):
            try:
                os.remove(os.path.join(work_dir, name))
            except OSError:
                pass
    failed_dir = os.path.join(CONFIG["work_dir"], "failed", f"job_{job_id}")
    try:
        if os.path.exists(failed_dir):
            shutil.rmtree(failed_dir)
        shutil.move(work_dir, failed_dir)
        log.info("Kept failed-verification output for job %d at %s for manual review", job_id, failed_dir)
    except OSError:
        log.exception("Could not preserve failed output for job %d", job_id)


def _remove_quietly(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        log.warning("Could not remove %s", path)


def _cleanup_dir(path):
    # A just-written file is often still held for a moment (antivirus
    # scanning it, ffmpeg's handle closing) and rmtree(ignore_errors) then
    # silently leaves it behind. Retry briefly; sweep_work_dir() catches the rest.
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(2)
    log.warning("Could not fully remove %s (will be swept later)", path)


def sweep_work_dir(max_failed_age_days=7):
    """Delete leftover job_* scratch folders that no running job is using,
    and kept failed-verification outputs older than a week."""
    root = CONFIG["work_dir"]
    freed = 0
    now = time.time()
    for name in os.listdir(root) if os.path.isdir(root) else []:
        full = os.path.join(root, name)
        if not (name.startswith("job_") and os.path.isdir(full)):
            continue
        if _current_job_id is not None and name == f"job_{_current_job_id}":
            continue
        if now - os.path.getmtime(full) < 3600:
            continue  # recently touched -- leave it
        size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(full) for f in fs)
        shutil.rmtree(full, ignore_errors=True)
        if not os.path.exists(full):
            freed += size
    failed = os.path.join(root, "failed")
    for name in os.listdir(failed) if os.path.isdir(failed) else []:
        full = os.path.join(failed, name)
        if now - os.path.getmtime(full) > max_failed_age_days * 86400:
            size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(full) for f in fs)
            shutil.rmtree(full, ignore_errors=True)
            freed += size
    if freed:
        log.info("Work dir sweep freed %.1f GB", freed / 1024 ** 3)
    return freed


def _fail_or_retry(job, message):
    attempts = (job["attempts"] or 0) + 1
    if attempts >= CONFIG["max_attempts"]:
        _store.update_job(job["id"], status="failed", error_message=message, attempts=attempts)
        log.error("Job %d permanently failed after %d attempts: %s", job["id"], attempts, message)
    else:
        _store.update_job(job["id"], status="queued", error_message=message, attempts=attempts, progress_pct=0)
        log.warning("Job %d failed (attempt %d/%d), requeued: %s", job["id"], attempts, CONFIG["max_attempts"], message)


def current_status():
    return {
        "current_job_id": _current_job_id,
        "current_progress": _current_progress,
        "current_speed": _current_speed,
        "current_eta_seconds": _current_eta_seconds,
    }


def worker_loop():
    log.info("Queue worker started")
    resume_incomplete_jobs()
    global _current_job_id
    while not _stop_event.is_set():
        heartbeat.touch()
        job = db.claim_next_job(LOCAL_NAME)
        if job is None:
            _current_job_id = None
            _stop_event.wait(5)
            continue
        try:
            process_job(job)
        except Exception:
            log.exception("Unhandled error processing job %d -- marking failed to avoid a crash loop", job["id"])
            _store.update_job(job["id"], status="failed", error_message="Unhandled internal error, see logs")
        heartbeat.touch()
        _current_job_id = None


def start():
    _stop_event.clear()
    t = threading.Thread(target=worker_loop, name="queue-worker", daemon=True)
    t.start()
    return t


def stop():
    _stop_event.set()
