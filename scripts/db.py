"""SQLite layer for the web tracker (see docs/PLAN_web_tracker.md).

Owns the schema, the migration ladder, and the connection helper. Nothing in
the pipeline calls this yet — bootstrap_from_workbook.py (phase 2), the
orchestrator's `store` phase (phase 4) and the web app (phase 3/5) come later.

Stdlib only, deliberately: backup_db.py and the web app must be importable on
a Pi without openpyxl (same rule pipeline_stats.py follows).

The design in one line — the pipeline inserts with

    INSERT ... ON CONFLICT(key) DO NOTHING

which is what lets the export_mark watermark be deleted. A row the user
deleted is still present as a tombstone (deleted_at set), so the insert
conflicts and nothing is resurrected; a live row conflicts too, so the
hand-typed columns are never overwritten. Deletion is recorded, not inferred
from a row's absence — that inference is the entire reason the watermark and
its two-phase .pending handshake exist today.

Writer split, enforced by convention (the column lists below):
    pipeline owns PIPELINE_FIELDS   — never touched by the web UI
    the user owns USER_FIELDS       — never touched by the pipeline
Because the sets are disjoint there is no merge logic anywhere in the system.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from matches import row_key  # noqa: F401 — re-exported; callers key rows via db.row_key
from paths import DB_PATH

SCHEMA_VERSION = 1

# Pipeline-owned columns, mirroring matches.CSV_FIELDS (minus date_processed,
# which is listed last here to match the CSV's own ordering intent).
PIPELINE_FIELDS = [
    "url", "title", "company", "location", "salary", "source",
    "score", "suitable", "matched_skills", "concerns", "reason",
    "date_processed",
]

# User-owned columns — the export_workbook COLUMNS entries whose csv_field is
# None, plus application_id (the pipeline only ever writes "." there as an
# overflow spacer, so the user effectively owns it).
USER_FIELDS = [
    "date_applied", "cover_letter", "due_date", "round_num",
    "status", "as_of", "notes", "application_id",
]

# Canonical option lists for the two enumerated user-owned columns. The web UI
# (phase 5) renders these as <select>; export_workbook re-exports them,
# header-keyed, for the workbook's data-validation dropdowns. Defined here —
# stdlib, no openpyxl — so the web app and the workbook can't drift apart, the
# same single-source rule row_key follows. Keys are jobs-table column names.
USER_FIELD_OPTIONS = {
    "status":       ["Applied", "Interview Scheduled", "Offer",
                     "Rejected", "In Progress", "Withdrawn"],
    "cover_letter": ["Required", "Required - ChatGPT", "Optional",
                     "Not Required", "Submitted"],
}

# Set on soft-delete. 'user'/'prune' come from the UI; the 'import-*' values
# are written once by the phase-2 bootstrap to carry existing deletions over.
DELETE_REASONS = ("user", "prune", "import-csv", "import-prune")


# ── migrations ────────────────────────────────────────────────────────────────
# Append-only list; entry N holds the statements that move the schema to v(N+1).
# Never edit a shipped entry — add a new one. init_db() applies whatever is
# missing and records the result in meta.schema_version.
#
# Each migration is a LIST of single statements, not one script: executescript()
# issues its own COMMIT, which would silently break the enclosing transaction
# and leave a half-applied migration recorded as complete. Executed one at a
# time, SQLite's DDL is transactional and a failure rolls the whole step back.

MIGRATIONS = [
    # v1 — initial schema
    ["""
    CREATE TABLE jobs (
      key             TEXT PRIMARY KEY,

      -- pipeline-owned
      url             TEXT,
      title           TEXT,
      company         TEXT,
      location        TEXT,
      salary          TEXT,
      source          TEXT,
      score           INTEGER,
      suitable        INTEGER,
      matched_skills  TEXT,
      concerns        TEXT,
      reason          TEXT,
      date_processed  TEXT,          -- matches.TS_FORMAT, "%Y-%m-%d %H:%M"

      -- user-owned
      date_applied    TEXT,
      cover_letter    TEXT,
      due_date        TEXT,
      round_num       TEXT,
      status          TEXT,
      as_of           TEXT,
      notes           TEXT,
      application_id  TEXT,

      -- lifecycle
      deleted_at      TEXT,          -- NULL = live, else a tombstone
      deleted_reason  TEXT,
      created_at      TEXT NOT NULL,
      updated_at      TEXT NOT NULL
    )
    """,
     "CREATE INDEX idx_jobs_live    ON jobs(deleted_at)",
     "CREATE INDEX idx_jobs_company ON jobs(company)",
     "CREATE INDEX idx_jobs_score   ON jobs(score)",
     ],
]


# ── connection ────────────────────────────────────────────────────────────────

def connect(path=None, readonly=False):
    """Open the tracker DB with the pragmas the whole system depends on.

    WAL is what makes the "no splash screen" decision safe: readers get a
    consistent snapshot and never block on the pipeline's writer, so the site
    stays usable during a multi-hour filter phase. It is persisted in the file
    header, but setting it per-connection is idempotent and keeps a fresh DB
    correct. busy_timeout is per-connection and must be set every time.

    isolation_level=None puts the connection in autocommit mode so that
    transactions are explicit — see transaction() below.
    """
    path = path or DB_PATH
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn):
    """Explicit write transaction: commit on success, roll back on any error.

    BEGIN IMMEDIATE, not plain BEGIN: a deferred transaction takes a read lock
    first and can fail with SQLITE_BUSY when it tries to upgrade to a write
    lock mid-way, after work is already done. IMMEDIATE takes the write lock up
    front, so contention shows up as a clean busy_timeout wait instead.

    The store phase wraps a whole batch in one of these, so readers see the
    batch before or after — never half-applied.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ── schema ────────────────────────────────────────────────────────────────────

def _user_version(conn):
    row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    return int(row["v"]) if row else 0


def init_db(conn):
    """Create the schema if absent and apply any missing migrations.

    Safe to call on every startup. Returns the resulting schema version.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    current = _user_version(conn)

    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"{DB_PATH.name} is at schema v{current} but this code only knows "
            f"v{len(MIGRATIONS)} — you are running an older checkout against a "
            f"newer database. Update the code rather than downgrading the DB."
        )

    for version, statements in enumerate(MIGRATIONS[current:], start=current + 1):
        with transaction(conn):
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta (k, v) VALUES ('schema_version', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (str(version),),
            )
    return max(current, len(MIGRATIONS))


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, str(value)),
    )


# ── row mapping ───────────────────────────────────────────────────────────────

def now_iso():
    """UTC timestamp for created_at / updated_at / deleted_at."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def from_csv_row(row):
    """A matched_jobs.csv row (matches.CSV_FIELDS) → a jobs-table dict.

    Only pipeline-owned columns are produced; user columns are left for the UI
    so an upsert can never clobber them. matches.read_matches has already
    dropped rows with an unparseable score, but this is also called on rows
    read straight from the workbook during the phase-2 bootstrap, so the
    coercions stay defensive.
    """
    def clean(field):
        return (row.get(field) or "").strip()

    try:
        score = int(float(row.get("score") or 0))
    except (TypeError, ValueError):
        score = None

    # filter_jobs writes this as a Python bool repr ("True"/"False").
    suitable = clean("suitable").lower()
    suitable = 1 if suitable in ("true", "1", "yes") else 0 if suitable else None

    return {
        "key":            row_key(row.get("url"), row.get("title"), row.get("company")),
        "url":            clean("url"),
        "title":          clean("title"),
        "company":        clean("company"),
        "location":       clean("location"),
        "salary":         clean("salary"),
        "source":         clean("source"),
        "score":          score,
        "suitable":       suitable,
        "matched_skills": clean("matched_skills"),
        "concerns":       clean("concerns"),
        "reason":         clean("reason"),
        "date_processed": clean("date_processed"),
    }


# ── writes ────────────────────────────────────────────────────────────────────

_INSERT_SQL = """
INSERT INTO jobs ({cols}, created_at, updated_at)
VALUES ({placeholders}, :created_at, :updated_at)
ON CONFLICT(key) DO NOTHING
"""


def insert_new(conn, rec, *, user_fields=None, deleted_at=None, deleted_reason=None):
    """Insert a job if its key is unseen; do nothing if it already exists.

    Returns True if a row was created. The DO NOTHING is the load-bearing part:
    an existing live row keeps its hand-typed columns, and an existing tombstone
    stays deleted. Callers must be inside a transaction().

    user_fields/deleted_* are for the phase-2 bootstrap, which is the only
    caller that legitimately seeds user columns and tombstones. The pipeline
    passes neither.
    """
    rec = dict(rec)
    if user_fields:
        rec.update({k: v for k, v in user_fields.items() if k in USER_FIELDS})
    if deleted_at:
        if deleted_reason not in DELETE_REASONS:
            raise ValueError(f"unknown deleted_reason: {deleted_reason!r}")
        rec["deleted_at"]     = deleted_at
        rec["deleted_reason"] = deleted_reason

    stamp = now_iso()
    rec.setdefault("created_at", stamp)
    rec.setdefault("updated_at", stamp)

    cols = [c for c in rec if c not in ("created_at", "updated_at")]
    sql  = _INSERT_SQL.format(
        cols=", ".join(cols),
        placeholders=", ".join(f":{c}" for c in cols),
    )
    return conn.execute(sql, rec).rowcount > 0


def update_user_fields(conn, key, fields):
    """Write hand-edited columns for one job. Rejects pipeline-owned columns
    outright rather than silently dropping them, so a typo in a form field name
    surfaces instead of vanishing."""
    unknown = set(fields) - set(USER_FIELDS)
    if unknown:
        raise ValueError(f"not user-editable: {sorted(unknown)}")
    if not fields:
        return False
    assignments = ", ".join(f"{c} = :{c}" for c in fields)
    params = {**fields, "key": key, "updated_at": now_iso()}
    cur = conn.execute(
        f"UPDATE jobs SET {assignments}, updated_at = :updated_at WHERE key = :key",
        params,
    )
    return cur.rowcount > 0


def soft_delete(conn, key, reason="user"):
    """Tombstone a job. The row stays in the table — that is what stops the
    pipeline from re-adding it, and it is why no watermark is needed."""
    if reason not in DELETE_REASONS:
        raise ValueError(f"unknown deleted_reason: {reason!r}")
    stamp = now_iso()
    cur = conn.execute(
        "UPDATE jobs SET deleted_at = ?, deleted_reason = ?, updated_at = ? "
        "WHERE key = ? AND deleted_at IS NULL",
        (stamp, reason, stamp, key),
    )
    return cur.rowcount > 0


def restore(conn, key):
    cur = conn.execute(
        "UPDATE jobs SET deleted_at = NULL, deleted_reason = NULL, updated_at = ? "
        "WHERE key = ? AND deleted_at IS NOT NULL",
        (now_iso(), key),
    )
    return cur.rowcount > 0


# ── reads ─────────────────────────────────────────────────────────────────────

def get_job(conn, key):
    return conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()


# Sort keys the UI may ask for. Callers pass a KEY, never SQL — the ORDER BY
# clause is interpolated, so accepting a caller-supplied string here would put
# a query parameter straight into the statement.
SORT_ORDERS = {
    "score":   "score DESC, date_processed DESC",
    "date":    "date_processed DESC, score DESC",
    "company": "company COLLATE NOCASE, score DESC",
    "title":   "title COLLATE NOCASE",
    # NULL statuses last, so untouched rows don't bury the ones in flight.
    "status":  "status IS NULL, status COLLATE NOCASE, score DESC",
}
DEFAULT_SORT = "score"


def search_jobs(conn, *, q=None, company=None, status=None, min_score=None,
                archived=False, sort=DEFAULT_SORT, limit=None):
    """Filtered job rows. `sort` is a SORT_ORDERS key; anything unrecognised
    falls back to the default rather than raising, so a stale bookmark or a
    hand-edited query string degrades instead of erroring."""
    where  = ["deleted_at IS NOT NULL" if archived else "deleted_at IS NULL"]
    params = {}

    if q:
        where.append("(title LIKE :q OR company LIKE :q OR location LIKE :q "
                     "OR reason LIKE :q OR notes LIKE :q)")
        params["q"] = f"%{q}%"
    if company:
        where.append("company = :company")
        params["company"] = company
    if status:
        where.append("status = :status" if status != "__none__" else "status IS NULL")
        if status != "__none__":
            params["status"] = status
    if min_score is not None:
        where.append("score >= :min_score")
        params["min_score"] = min_score

    order = SORT_ORDERS.get(sort, SORT_ORDERS[DEFAULT_SORT])
    sql   = f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY {order}"
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return conn.execute(sql, params).fetchall()


def live_jobs(conn, sort=DEFAULT_SORT):
    return search_jobs(conn, sort=sort)


def score_distribution(conn):
    """[(score, count)] over live rows, highest first."""
    return [(r["score"], r["n"]) for r in conn.execute(
        "SELECT score, COUNT(*) AS n FROM jobs "
        "WHERE deleted_at IS NULL AND score IS NOT NULL "
        "GROUP BY score ORDER BY score DESC")]


def top_companies(conn, limit=15):
    return conn.execute(
        "SELECT company, COUNT(*) AS n, AVG(score) AS avg_score, MAX(score) AS max_score "
        "FROM jobs WHERE deleted_at IS NULL AND company != '' "
        "GROUP BY company ORDER BY n DESC, avg_score DESC LIMIT ?", (limit,)).fetchall()


def status_breakdown(conn):
    """Application status counts over live rows. Only meaningful once the web
    UI owns the Status column (phase 5) — before that everything is untouched."""
    return conn.execute(
        "SELECT COALESCE(NULLIF(status, ''), 'Not started') AS status, COUNT(*) AS n "
        "FROM jobs WHERE deleted_at IS NULL GROUP BY 1 ORDER BY n DESC").fetchall()


def distinct_values(conn, column):
    """Distinct non-empty values of a column, for filter dropdowns."""
    if column not in ("company", "status", "source"):
        raise ValueError(f"not a filterable column: {column!r}")
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {column} FROM jobs "
        f"WHERE deleted_at IS NULL AND {column} IS NOT NULL AND {column} != '' "
        f"ORDER BY {column} COLLATE NOCASE")]


def counts(conn):
    """{'live': n, 'deleted': n, 'total': n} — cheap enough for every page load."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(deleted_at IS NULL)     AS live, "
        "       SUM(deleted_at IS NOT NULL) AS deleted "
        "FROM jobs"
    ).fetchone()
    return {"total": row["total"], "live": row["live"] or 0, "deleted": row["deleted"] or 0}


def is_bootstrapped(conn):
    """True once the phase-2 import has run, or if any row exists at all.

    Two independent signals on purpose: the meta flag is the intent, the row
    count is the fact. The importer refuses to run if either is set, so a
    re-import can't overwrite live edits even if the flag was lost.
    """
    if get_meta(conn, "bootstrapped_at"):
        return True
    return conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None


if __name__ == "__main__":
    # `python3 scripts/db.py` — create/migrate the DB and report where it stands.
    conn = connect()
    version = init_db(conn)
    c = counts(conn)
    print(f"{DB_PATH}")
    print(f"  schema version : {version}")
    print(f"  jobs           : {c['total']} ({c['live']} live, {c['deleted']} tombstoned)")
    print(f"  bootstrapped   : {get_meta(conn, 'bootstrapped_at') or 'no'}")
