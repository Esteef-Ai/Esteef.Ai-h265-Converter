"""SQLite persistence for the job queue, watched directories, and quarantine
records. Using SQLite (not in-memory) is what lets the whole queue survive a
crash or a Windows update reboot -- on restart the queue_manager just resumes
from whatever is still 'queued' or was mid-flight."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import CONFIG

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    src_codec TEXT,
    src_duration REAL,
    src_size INTEGER,
    src_width INTEGER,
    src_height INTEGER,
    src_fps REAL,
    src_bitrate INTEGER,
    src_pix_fmt TEXT,
    src_color_primaries TEXT,
    src_color_transfer TEXT,
    src_color_space TEXT,
    is_hdr INTEGER DEFAULT 0,
    out_path TEXT,
    out_size INTEGER,
    crf INTEGER,
    preset TEXT,
    vmaf_score REAL,
    duration_diff REAL,
    error_message TEXT,
    attempts INTEGER DEFAULT 0,
    progress_pct REAL DEFAULT 0,
    quarantine_path TEXT,
    quarantine_expires_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS watch_dirs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    recursive INTEGER DEFAULT 1,
    added_at TEXT NOT NULL,
    last_scanned_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- One row per remote worker PC. state is the JSON blob from its last
-- heartbeat (pause mode, current job, progress, speed...). pending_command
-- is a dashboard button press waiting to be delivered on the next heartbeat.
CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    address TEXT,
    state TEXT,
    pending_command TEXT
);
"""


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(CONFIG["db_path"], timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


_write_lock = threading.Lock()


@contextmanager
def write_lock():
    """Serialise writes within this process. If a write fails part-way, roll
    the transaction back: sqlite3 opens a transaction implicitly on the first
    write, and a failed statement (e.g. a duplicate-key insert) would
    otherwise leave it open -- holding the database lock so every other
    thread and request gets "database is locked"."""
    with _write_lock:
        try:
            yield
        except BaseException:
            try:
                get_conn().rollback()
            except sqlite3.Error:
                pass
            raise


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "worker" not in cols:
        # Which PC claimed/processed the job. NULL = unclaimed, or processed
        # by the master before multi-PC support existed.
        conn.execute("ALTER TABLE jobs ADD COLUMN worker TEXT")
    if "priority" not in cols:
        # Higher runs first. Copied from the job's watched folder at scan
        # time (see scanner) and updated when the folder's priority changes.
        conn.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 0")
    wcols = {r["name"] for r in conn.execute("PRAGMA table_info(watch_dirs)")}
    if "profile" not in wcols:
        # Which source formats this folder converts -- see CONFIG["codec_profiles"].
        conn.execute("ALTER TABLE watch_dirs ADD COLUMN profile TEXT DEFAULT 'standard'")
    if "priority" not in wcols:
        conn.execute("ALTER TABLE watch_dirs ADD COLUMN priority INTEGER DEFAULT 0")
    conn.commit()


def now():
    return datetime.now().isoformat(timespec="seconds")


def add_watch_dir(path, recursive=True, profile="standard", priority=0):
    """Add a folder, or update its settings if it's already watched."""
    conn = get_conn()
    with write_lock():
        conn.execute(
            "INSERT INTO watch_dirs (path, recursive, added_at, profile, priority) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET recursive=excluded.recursive, profile=excluded.profile, priority=excluded.priority",
            (path, int(recursive), now(), profile, int(priority)),
        )
        conn.commit()


def update_watch_dir(dir_id, profile, priority):
    conn = get_conn()
    with write_lock():
        conn.execute("UPDATE watch_dirs SET profile=?, priority=? WHERE id=?", (profile, int(priority), dir_id))
        conn.commit()


def get_watch_dir(dir_id):
    return get_conn().execute("SELECT * FROM watch_dirs WHERE id=?", (dir_id,)).fetchone()


def set_priority_for_folder(path, priority):
    """Re-prioritise a folder's not-yet-started files."""
    conn = get_conn()
    prefix = path.rstrip("\\") + "\\"
    with write_lock():
        conn.execute("UPDATE jobs SET priority=? WHERE status='queued' AND lower(substr(source_path, 1, ?)) = lower(?)",
                     (int(priority), len(prefix), prefix))
        conn.commit()


def is_output_of_job(path):
    """True if this file is a converted output we wrote (e.g. x.avi -> x.mkv),
    so the scanner doesn't treat it as a new source."""
    return get_conn().execute("SELECT 1 FROM jobs WHERE out_path=? AND source_path!=?", (path, path)).fetchone() is not None


def list_watch_dirs():
    return get_conn().execute("SELECT * FROM watch_dirs ORDER BY added_at").fetchall()


def touch_watch_dir(dir_id):
    conn = get_conn()
    with write_lock():
        conn.execute("UPDATE watch_dirs SET last_scanned_at=? WHERE id=?", (now(), dir_id))
        conn.commit()


def update_watch_dir_path(dir_id, path):
    conn = get_conn()
    with write_lock():
        conn.execute("UPDATE watch_dirs SET path=? WHERE id=?", (path, dir_id))
        conn.commit()


def remove_watch_dir(dir_id):
    conn = get_conn()
    with write_lock():
        conn.execute("DELETE FROM watch_dirs WHERE id=?", (dir_id,))
        conn.commit()


def upsert_job(source_path, **fields):
    conn = get_conn()
    existing = conn.execute("SELECT id FROM jobs WHERE source_path=?", (source_path,)).fetchone()
    with write_lock():
        if existing:
            if fields:
                cols = ", ".join(f"{k}=?" for k in fields)
                conn.execute(f"UPDATE jobs SET {cols} WHERE source_path=?", (*fields.values(), source_path))
                conn.commit()
            return existing["id"]
        else:
            fields.setdefault("status", "queued")
            fields.setdefault("created_at", now())
            fields["source_path"] = source_path
            fields.setdefault("filename", source_path.split("\\")[-1])
            cols = ", ".join(fields.keys())
            qs = ", ".join("?" for _ in fields)
            # OR IGNORE: if another thread recorded this file a moment ago,
            # keep that row rather than failing.
            cur = conn.execute(f"INSERT OR IGNORE INTO jobs ({cols}) VALUES ({qs})", tuple(fields.values()))
            conn.commit()
            if cur.rowcount == 0:
                return conn.execute("SELECT id FROM jobs WHERE source_path=?", (source_path,)).fetchone()["id"]
            return cur.lastrowid


def update_job(job_id, **fields):
    if not fields:
        return
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    with write_lock():
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        conn.commit()


def delete_job(job_id):
    conn = get_conn()
    with write_lock():
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()


def get_job(job_id):
    return get_conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def get_job_by_source(source_path):
    return get_conn().execute("SELECT * FROM jobs WHERE source_path=?", (source_path,)).fetchone()


def next_queued_job():
    return get_conn().execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
    ).fetchone()


def reset_stuck_jobs(local_name):
    """On startup, anything this PC left mid-flight from a crash goes back to
    queued so the encode is simply retried from scratch (the partial temp
    output is discarded, source file is untouched at that point in the
    pipeline). Jobs held by a remote worker are left alone -- that worker may
    still be busy with them; workers.reconcile() deals with ones that aren't."""
    conn = get_conn()
    with write_lock():
        conn.execute(
            "UPDATE jobs SET status='queued', progress_pct=0, worker=NULL "
            "WHERE status IN ('probing','encoding','verifying') AND (worker IS NULL OR worker=?)",
            (local_name,),
        )
        conn.commit()


def claim_next_job(worker_name, path_filter=None):
    """Atomically hand the next queued job (highest folder priority, then
    oldest) to worker_name (status ->
    probing) so two PCs can never pick up the same file. path_filter, if
    given, skips jobs that worker can't reach (e.g. on a drive that isn't a
    network share). Returns the claimed row or None."""
    conn = get_conn()
    with write_lock():
        rows = conn.execute(
            "SELECT id, source_path FROM jobs WHERE status='queued' ORDER BY priority DESC, created_at LIMIT 500"
        ).fetchall()
        for row in rows:
            if path_filter and not path_filter(row["source_path"]):
                continue
            cur = conn.execute(
                "UPDATE jobs SET status='probing', worker=?, started_at=? WHERE id=? AND status='queued'",
                (worker_name, now(), row["id"]),
            )
            conn.commit()
            if cur.rowcount:
                return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    return None


def release_job(job_id, reason):
    """Hand a claimed job back to the queue untouched (attempt count kept)."""
    update_job(job_id, status="queued", worker=None, progress_pct=0, error_message=reason)


def remote_active_jobs(local_name):
    return get_conn().execute(
        "SELECT * FROM jobs WHERE status IN ('probing','encoding','verifying','finalizing') "
        "AND worker IS NOT NULL AND worker != ?",
        (local_name,),
    ).fetchall()


def upsert_worker(name, address, state_json):
    conn = get_conn()
    with write_lock():
        conn.execute(
            "INSERT INTO workers (name, first_seen, last_seen, address, state) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, address=excluded.address, state=excluded.state",
            (name, now(), now(), address, state_json),
        )
        conn.commit()


def get_worker(name):
    return get_conn().execute("SELECT * FROM workers WHERE name=?", (name,)).fetchone()


def list_workers():
    return get_conn().execute("SELECT * FROM workers ORDER BY name").fetchall()


def set_worker_command(name, command):
    conn = get_conn()
    with write_lock():
        conn.execute("UPDATE workers SET pending_command=? WHERE name=?", (command, name))
        conn.commit()


def pop_worker_command(name):
    conn = get_conn()
    with write_lock():
        row = conn.execute("SELECT pending_command FROM workers WHERE name=?", (name,)).fetchone()
        if row and row["pending_command"]:
            conn.execute("UPDATE workers SET pending_command=NULL WHERE name=?", (name,))
            conn.commit()
            return row["pending_command"]
    return None


def delete_worker(name):
    conn = get_conn()
    with write_lock():
        conn.execute("DELETE FROM workers WHERE name=?", (name,))
        conn.commit()


ACTIVE_STATUSES = ("queued", "probing", "encoding", "verifying", "finalizing")


TROUBLE_STATUSES = ("failed", "verification_failed", "low_disk_space", "bad_filename")
# Trouble statuses a requeue can actually help with (bad_filename is fixed by
# renaming the file, which the scanner retries on every scan).
REQUEUEABLE_STATUSES = ("failed", "verification_failed", "low_disk_space")

VIEW_STATUSES = {
    "active": ACTIVE_STATUSES,
    "trouble": TROUBLE_STATUSES,
}


def list_jobs(limit=10000, include_purged=False, view="all"):
    """view: "all", "active" (queued or being worked on), or "trouble"
    (failed / verification_failed / low_disk_space -- the ones needing a
    human decision to requeue or leave alone)."""
    where = []
    params = []
    if not include_purged:
        where.append("status != 'purged'")
    statuses = VIEW_STATUSES.get(view)
    if statuses:
        where.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    return get_conn().execute(
        f"SELECT * FROM jobs {clause} ORDER BY created_at DESC LIMIT ?", params
    ).fetchall()


def requeue_trouble_jobs():
    """Bulk version of the per-row Retry button: resets every trouble job to
    a fresh attempt at the default CRF. Returns how many were requeued."""
    conn = get_conn()
    with write_lock():
        cur = conn.execute(
            f"UPDATE jobs SET status='queued', error_message=NULL, attempts=0, progress_pct=0, "
            f"crf=NULL, out_size=NULL WHERE status IN ({','.join('?' for _ in REQUEUEABLE_STATUSES)})",
            REQUEUEABLE_STATUSES,
        )
        conn.commit()
        return cur.rowcount


REENCODED_STATUSES = ("done", "purged")
UNTOUCHED_STATUSES = ("no_size_gain", "skipped_not_h264", "failed", "verification_failed", "restored", "low_disk_space", "bad_filename")


def library_summary():
    """Whole-library totals for the dashboard summary panel. Every job has
    been probed, so src_size is known for all of them; out_size is only
    meaningful for files actually re-encoded and swapped in."""
    conn = get_conn()

    def agg(statuses=None):
        if statuses is None:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(src_size),0), COALESCE(SUM(out_size),0) FROM jobs").fetchone()
        else:
            ph = ",".join("?" for _ in statuses)
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(src_size),0), COALESCE(SUM(out_size),0) FROM jobs WHERE status IN ({ph})",
                statuses,
            ).fetchone()
        return {"count": row[0], "src": row[1], "out": row[2]}

    analyzed = agg()
    reencoded = agg(REENCODED_STATUSES)
    untouched = agg(UNTOUCHED_STATUSES)
    pending = agg(ACTIVE_STATUSES)
    saved = reencoded["src"] - reencoded["out"]
    return {
        "analyzed": analyzed,
        "untouched": untouched,
        "reencoded": reencoded,
        "pending": pending,
        "saved": saved,
        "saved_pct": (100 * saved / reencoded["src"]) if reencoded["src"] else 0,
    }


def count_by_view():
    conn = get_conn()
    counts = {"all": conn.execute("SELECT COUNT(*) FROM jobs WHERE status != 'purged'").fetchone()[0]}
    for view, statuses in VIEW_STATUSES.items():
        counts[view] = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' for _ in statuses)})", statuses
        ).fetchone()[0]
    return counts


def quarantined_jobs_with_paths():
    """All jobs that still have a quarantine_path set, regardless of whether
    their expiry has passed -- used to notice a file the user deleted by hand
    before the automatic sweep got to it."""
    return get_conn().execute(
        "SELECT * FROM jobs WHERE quarantine_path IS NOT NULL"
    ).fetchall()


def list_quarantined():
    return get_conn().execute(
        "SELECT * FROM jobs WHERE quarantine_path IS NOT NULL ORDER BY quarantine_expires_at"
    ).fetchall()


def jobs_needing_purge():
    return get_conn().execute(
        "SELECT * FROM jobs WHERE status='done' AND quarantine_path IS NOT NULL "
        "AND quarantine_expires_at IS NOT NULL AND quarantine_expires_at <= ?",
        (now(),),
    ).fetchall()


def summary_stats():
    conn = get_conn()
    row = conn.execute(
        "SELECT "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN status IN ('done','purged') THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued, "
        "SUM(CASE WHEN status IN ('probing','encoding','verifying','finalizing') THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN status LIKE '%failed%' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status IN ('done','purged') THEN src_size ELSE 0 END) AS src_bytes_done, "
        "SUM(CASE WHEN status IN ('done','purged') THEN out_size ELSE 0 END) AS out_bytes_done "
        "FROM jobs"
    ).fetchone()
    return dict(row)
