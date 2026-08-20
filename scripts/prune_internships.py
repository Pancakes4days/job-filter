#!/usr/bin/env python3
"""prune_internships.py -- tombstone internship rows already in the tracker.

The scoring profile stopped accepting internships (config.json: the dealbreaker
list), but that only governs jobs scored from here on. Rows stored under the old
profile are still live in the DB, and their fingerprints are in seen_jobs.txt so
the pipeline will never re-score them. This retires them in one pass.

    python3 scripts/prune_internships.py            # dry run -- lists, changes nothing
    python3 scripts/prune_internships.py --apply    # tombstone them
    python3 scripts/prune_internships.py --undo     # restore what this script pruned

Dry run is the DEFAULT here, unlike bootstrap_from_workbook.py's --dry-run flag.
That script refuses to run against a populated DB; this one only ever acts on a
populated DB, so the safe direction is inverted.

Rows are SOFT-deleted with reason "prune" (db.DELETE_REASONS), never removed.
The tombstone is load-bearing twice over: it stops the store phase from
re-inserting the row on the next cycle, and it makes --undo possible because the
row and its hand-typed columns are still there.

Jobs you have engaged with -- anything with a Status or Date Applied set -- are
left alone unless --include-touched is passed. Silently archiving a job you have
already applied to loses real work, and no amount of pattern matching is worth
that risk.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from paths import DB_PATH

# One definition of "this is an internship", shared with the alert path so the
# two can't drift into disagreeing. It lives in recruitment_watch.py because that
# module is stdlib-only -- importing in this direction would drag db into the
# 15-minute newgrad_watch poller. See it there for why the word boundaries and
# the title-only matching are load-bearing.
from recruitment_watch import INTERNSHIP_TITLE  # noqa: E402

# User-owned columns whose presence means "I have touched this job".
ENGAGED_FIELDS = ("status", "date_applied")


def _engaged(row):
    return any((row[f] or "").strip() for f in ENGAGED_FIELDS)


def find_internships(conn, include_touched=False):
    """(to_prune, skipped) over live rows whose title looks like an internship."""
    to_prune, skipped = [], []
    for row in db.search_jobs(conn, sort="company"):
        if not INTERNSHIP_TITLE.search(row["title"] or ""):
            continue
        if _engaged(row) and not include_touched:
            skipped.append(row)
        else:
            to_prune.append(row)
    return to_prune, skipped


def find_pruned(conn):
    """Tombstones this script created, newest first."""
    return [r for r in db.search_jobs(conn, archived=True)
            if r["deleted_reason"] == "prune"]


def _show(row):
    score = row["score"] if row["score"] is not None else "-"
    title = (row["title"] or "(no title)")[:68]
    return f"  [{score:>2}] {title:<68}  {(row['company'] or '')[:28]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Actually tombstone the matches (default is a dry run)")
    parser.add_argument("--undo", action="store_true",
                        help="Restore every row this script previously pruned")
    parser.add_argument("--include-touched", action="store_true",
                        help="Also prune jobs with a Status or Date Applied set")
    args = parser.parse_args()

    if args.apply and args.undo:
        sys.exit("--apply and --undo are mutually exclusive.")
    if not DB_PATH.exists():
        sys.exit(f"No tracker DB at {DB_PATH}.")

    conn = db.connect()
    try:
        if not db.is_bootstrapped(conn):
            sys.exit(f"{DB_PATH} holds no jobs -- nothing to prune.")

        if args.undo:
            rows = find_pruned(conn)
            if not rows:
                print("No rows carry deleted_reason='prune' -- nothing to undo.")
                return
            for row in rows:
                print(_show(row))
            with db.transaction(conn):
                restored = sum(db.restore(conn, r["key"]) for r in rows)
            print(f"\nRestored {restored} row(s) to live.")
            return

        to_prune, skipped = find_internships(conn, args.include_touched)

        if skipped:
            print(f"Skipping {len(skipped)} internship(s) you have already engaged "
                  f"with (Status or Date Applied set) -- pass --include-touched "
                  f"to prune these too:")
            for row in skipped:
                print(_show(row))
            print()

        if not to_prune:
            print("No live internship rows to prune.")
            return

        print(f"{len(to_prune)} internship row(s) matched:")
        for row in to_prune:
            print(_show(row))

        if not args.apply:
            print(f"\nDry run -- nothing changed. Re-run with --apply to tombstone "
                  f"these {len(to_prune)} row(s).")
            return

        with db.transaction(conn):
            pruned = sum(db.soft_delete(conn, r["key"], reason="prune")
                         for r in to_prune)
        counts = db.counts(conn)
        print(f"\nTombstoned {pruned} row(s). Now {counts['live']} live, "
              f"{counts['deleted']} archived.")
        print("Reversible: python3 scripts/prune_internships.py --undo")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
