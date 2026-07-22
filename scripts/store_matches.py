#!/usr/bin/env python3
"""store_matches.py — upsert matched_jobs.csv into the tracker DB.

Phase 4 of the web-tracker migration (docs/PLAN_web_tracker.md). Runs as the
orchestrator's `store` phase, immediately before `sync`, and is also usable by
hand:

    python3 scripts/store_matches.py            # upsert data/matched_jobs.csv
    python3 scripts/store_matches.py --csv PATH # a different CSV

Every row is inserted with ON CONFLICT(key) DO NOTHING, so the DB's live/user
state is authoritative and the CSV only ever *adds* genuinely-new matches:
  - a job the user deleted (a tombstone) is never resurrected,
  - a live job's hand-typed columns are never overwritten,
  - the pipeline only touches PIPELINE_FIELDS via db.from_csv_row anyway.
The whole batch is one transaction, so a reader (the web app) sees it
all-or-nothing — never a half-written cycle.

Refuses to touch a DB that has not been bootstrapped (phase 2). The CSV is
cumulative — every match ever scored — so importing it into an empty DB would
add every past hand-deletion back as a *live* row. That resurrection is the
exact failure the bootstrap's import-csv tombstones exist to prevent, so this
skips rather than risk it.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from matches import read_matches
from paths import DATA_DIR, DB_PATH

DEFAULT_CSV = DATA_DIR / "matched_jobs.csv"


def store_matches(csv_path=DEFAULT_CSV, *, db_path=None):
    """Upsert matched CSV rows into the tracker DB. Returns (added, rows_seen).

    added      — rows newly inserted this run (existing keys are left as-is)
    rows_seen  — CSV rows considered, after read_matches' validation

    A missing/empty CSV is a no-op. A missing or un-bootstrapped DB is skipped
    with a message rather than materialising historical deletions as live rows.
    """
    db_path = db_path or DB_PATH
    rows = read_matches(csv_path)
    if not rows:
        print(f"No matches in {csv_path} — nothing to store.")
        return 0, 0

    # Don't create the DB here. If the bootstrap hasn't run, the file is absent
    # and the web app is deliberately showing its "run phase 2" page; silently
    # creating an empty DB would replace that with a misleading empty tracker.
    if not db_path.exists():
        print(f"{db_path.name} does not exist yet — run bootstrap_from_workbook.py "
              f"first. Skipping store (the CSV would resurrect past deletions).")
        return 0, 0

    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        if not db.is_bootstrapped(conn):
            print(f"{db_path.name} is not bootstrapped — skipping store so that "
                  f"historical deletions aren't re-added as live rows.")
            return 0, 0

        added = 0
        with db.transaction(conn):
            for row in rows:
                if db.insert_new(conn, db.from_csv_row(row)):
                    added += 1
        print(f"Stored {added} new match(es) into {db_path.name} "
              f"({len(rows)} CSV row(s) seen; existing rows left untouched).")
        return added, len(rows)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Upsert matched_jobs.csv into the tracker DB (ON CONFLICT DO NOTHING).")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                    help="matched jobs CSV (default: data/matched_jobs.csv)")
    args = ap.parse_args()
    store_matches(args.csv)


if __name__ == "__main__":
    main()
