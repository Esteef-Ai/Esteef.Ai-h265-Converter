"""Central configuration. Defaults reflect decisions made at project setup;
override any of them by editing config.json in the project root (created on
first run with these defaults)."""
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# AICONVERTER_CONFIG lets a second instance (e.g. a test master/worker pair)
# run from the same code with its own config, DB, work dir and ports.
CONFIG_PATH = os.environ.get("AICONVERTER_CONFIG") or os.path.join(PROJECT_ROOT, "config.json")

DEFAULTS = {
    # Encoding
    "video_codec": "libx265",
    "crf": 20,
    "preset": "medium",
    "encoder_threads": 0,  # 0 = let x265 decide

    # Quality verification gate
    "min_vmaf_score": 95.0,
    "max_duration_diff_seconds": 1.0,
    # With position-based frame pairing (see ffmpeg_utils.vmaf_compare) 8
    # threads scored identically to 1 thread in testing, about 5x faster.
    "vmaf_threads": 8,
    "vmaf_n_subsample": 1,
    "vmaf_max_height": 1080,  # cap the comparison resolution for larger sources (e.g. 4K) -- resolution match is already verified separately via ffprobe
    "verify_retry_crf_step": 3,
    # ...but only if a lower-CRF retry could still come out smaller than the
    # source: dropping CRF by 3 grows the file roughly 25-40%, so if this
    # attempt's output is already above this fraction of the source size,
    # the retry is pointless and the file is left alone as no_size_gain.
    "verify_retry_max_size_ratio": 0.80,  # on verification failure, retry at (current CRF - this) instead of giving up immediately

    # Early-abort a doomed encode instead of running it to completion
    "no_gain_check_min_pct": 25,  # don't judge the projection until at least this far in (early content, e.g. intros, can be unrepresentative)
    "no_gain_margin": 1.03,  # only abort once projected size exceeds source by more than this fraction (3% buffer against noise)

    # Which source files get queued
    "video_extensions": [".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".m4v"],
    "only_convert_h264": True,
    # Which source formats a watched folder converts, chosen per folder
    # ("profile" on the folder). Names are ffprobe codec names.
    #   standard: H.264 only.
    #   legacy:   H.264 plus older formats that shrank well enough in testing
    #             to be worth it: MPEG-1 (~27-51% saved), VC-1
    #             advanced profile (mixed), DivX/Xvid "mpeg4" (~20-28%).
    #             WMV3/WMV2 deliberately left out -- both samples grew.
    "codec_profiles": {
        "standard": ["h264", "avc", "avc1"],
        "legacy": ["h264", "avc", "avc1", "mpeg1video", "vc1", "mpeg4"],
    },

    # Hard subprocess timeouts. Without these, a hung ffprobe/ffmpeg call
    # (e.g. a flaky network/mounted drive that stops responding mid-read)
    # blocks forever with no exception -- previously this froze the whole
    # worker thread and, since the scanner thread hit the same drive too,
    # the entire process, with nothing to catch it.
    "subprocess_timeout_probe": 120,
    "subprocess_timeout_decode_check": 1800,
    "subprocess_timeout_vmaf": 14400,  # generous: single-threaded VMAF on a long file legitimately takes hours

    # Internal watchdog: force-exit the whole process if nothing has
    # happened in this long, so the external supervisor (supervisor.ps1)
    # can restart it. Paired with the timeouts above -- those turn a hang
    # into an exception the retry logic handles; this is the backstop for
    # anything that still manages to hang somewhere else.
    "watchdog_max_idle_minutes": 30,

    # Containers that can't hold HEVC natively -> re-mux into this container instead
    "container_fallback": {
        ".avi": ".mkv",
        ".wmv": ".mkv",
        ".flv": ".mkv",
        ".mpg": ".mkv",
        ".mpeg": ".mkv",
    },

    # Local scratch space used while encoding (fast local disk)
    "work_dir": os.path.join(PROJECT_ROOT, "work"),
    "min_free_c_drive_gb": 15,  # abort/pause queue if C: free space drops below this
    "dest_free_space_buffer_gb": 2,  # required headroom on destination drive beyond new file size

    # Safety net before permanent delete of originals
    "quarantine_suffix": ".aiconverter-trash",
    "quarantine_days": 5,

    # Concurrency

    "max_parallel_jobs": 1,

    # Directory watching (auto re-scan for newly added files)
    "rescan_interval_minutes": 30,

    # Stall detection: kill+retry a job if ffmpeg makes no progress for this long
    "stall_timeout_minutes": 10,
    "max_attempts": 3,

    # Web dashboard
    # 127.0.0.1 = only this PC can reach the dashboard. The dashboard has no
    # login, so set 0.0.0.0 only on a trusted network, and only when adding
    # worker PCs (setup.ps1 -EnableWorkers does this).
    "web_host": "127.0.0.1",
    "web_port": 8765,

    # Paths
    "db_path": os.path.join(PROJECT_ROOT, "data", "aiconverter.db"),
    "log_dir": os.path.join(PROJECT_ROOT, "logs"),
    "ffmpeg_bin": "ffmpeg",
    "ffprobe_bin": "ffprobe",

    # Priority for ffmpeg/ffprobe child processes: "normal", "below_normal"
    # or "idle". Workers on a PC someone is actively using default to
    # below_normal so encoding yields to whatever they're doing.
    "process_priority": "normal",

    # Distributed processing (see aiconverter/workers.py / worker.py).
    # "master" owns the DB, scanning, quarantine sweep and dashboard; a
    # "worker" owns none of that -- it just claims jobs from the master over
    # HTTP, encodes/verifies/finalizes them against the shared network drive,
    # and reports results back.
    "role": "master",
    "worker_api_key": "",  # shared secret; worker endpoints are disabled while empty
    "worker_offline_minutes": 15,  # master requeues a worker's job after this long without a heartbeat
    "worker_path_map": {},  # extra master-path-prefix -> worker-path-prefix mappings (network drives are detected automatically)

    # Worker-only settings
    "master_url": "",
    "worker_name": "",
    "worker_control_host": "127.0.0.1",
    "worker_control_port": 8766,
    "worker_heartbeat_seconds": 10,
    "prevent_sleep_while_working": True,

    # Renaming files whose names Windows can't handle (emoji) -- see
    # aiconverter/filenames.py. Some NAS/router SMB shares refuse to rename
    # them, but their FTP server can. Optional: fill in host/user/password
    # to enable.
    "ftp_rename": {"host": "", "port": 21, "user": "", "password": "", "share_roots": {}},
}

# Settings a worker takes from the master on every job so encodes are
# identical no matter which PC does them.
SHARED_ENCODE_SETTINGS = (
    "video_codec", "crf", "preset", "encoder_threads", "min_vmaf_score",
    "max_duration_diff_seconds", "vmaf_threads", "vmaf_n_subsample",
    "vmaf_max_height", "verify_retry_crf_step", "verify_retry_max_size_ratio", "no_gain_check_min_pct",
    "no_gain_margin", "container_fallback", "quarantine_suffix",
    "quarantine_days", "max_attempts", "stall_timeout_minutes",
    "dest_free_space_buffer_gb", "subprocess_timeout_probe",
    "subprocess_timeout_decode_check", "subprocess_timeout_vmaf",
)


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


CONFIG = load_config()
