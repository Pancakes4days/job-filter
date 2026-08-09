#!/usr/bin/env python3
"""apply_archive.py -- tombstone the jobs named in a keys file.

This script makes NO judgments. It is the mechanical half of a review: the
decisions live in the keys file, which is written by hand (or by a model
reading the workbook against config/config.json), and this only applies them.
That split is the point -- what gets archived is reviewable as a plain text
file before anything touches the database.

    python3 scripts/apply_archive.py data/archive_keys.txt          # dry run
    python3 scripts/apply_archive.py data/archive_keys.txt --apply  # tombstone
    python3 scripts/apply_archive.py --undo                         # restore

Dry run is the DEFAULT, following prune_internships.py: this only ever acts on
a populated DB, so the safe direction is to show first and write second.

Keys file format -- one job per line:

    <key><TAB><reason>          # reason is optional, shown in the dry run
    # lines starting with '#' and blank lines are ignored

A key is matches.row_key: the lowercased URL, or "title|company" lowercased for
listings that have no URL. Keys are re-lowercased here so a hand-edited file
with mixed case still matches.

Rows are SOFT-deleted with reason "review" (db.DELETE_REASONS), never removed.
The tombstone stops the store phase from re-inserting the row next cycle, and
keeps --undo possible because the row and its hand-typed columns are still there.
"review" is deliberately distinct from prune_internships.py's "prune" so the two
scripts' --undo cannot step on each other.

Jobs you have engaged with -- anything with a Status or Date Applied set -- are
skipped unless --include-touched is passed, for the reason prune_internships.py
gives: silently archiving a job you already applied to loses real work.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from paths import DB_PATH

REASON = "review"

# User-owned columns whose presence means "I have touched this job".
ENGAGED_FIELDS = ("status", "date_applied")


def _engaged(row):
    return any((row[f] or "").strip() for f in ENGAGED_FIELDS)


def read_keys(path):
    """[(key, reason)] from the keys file, in file order, duplicates dropped."""
    seen, out = set(), []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, reason = line.partition("\t")
        key = key.strip().lower()
        if not key:
            sys.exit(f"{path}:{lineno}: line has no key")
        if key in seen:
            continue
        seen.add(key)
        out.append((key, reason.strip()))
    return out


def _show(row, reason=""):
    score = row["score"] if row["score"] is not None else "-"
    title = (row["title"] or "(no title)")[:58]
    company = (row["company"] or "")[:22]
    tail = f"  -- {reason}" if reason else ""
    return f"  [{score:>2}] {title:<58}  {company:<22}{tail}"


def classify(conn, entries, include_touched=False):
    """Sort the keys file into what this run would do to each row."""
    to_archive, engaged, already, missing = [], [], [], []
    for key, reason in entries:
        row = db.get_job(conn, key)
        if row is None:
            missing.append((key, reason))
        elif row["deleted_at"]:
            already.append((row, reason))
        elif _engaged(row) and not include_touched:
            engaged.append((row, reason))
        else:
            to_archive.append((row, reason))
    return to_archive, engaged, already, missing


def find_archived(conn):
    """Tombstones this script created, as returned by search_jobs."""
    return [r for r in db.search_jobs(conn, archived=True)
            if r["deleted_reason"] == REASON]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("keys_file", nargs="?", type=Path,
                        help="File of keys to archive (omit with --undo)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually tombstone the rows (default is a dry run)")
    parser.add_argument("--undo", action="store_true",
                        help="Restore every row this script archived")
    parser.add_argument("--include-touched", action="store_true",
                        help="Also archive jobs with a Status or Date Applied set")
    args = parser.parse_args()

    if args.apply and args.undo:
        sys.exit("--apply and --undo are mutually exclusive.")
    if not args.undo and args.keys_file is None:
        parser.error("a keys file is required unless --undo is given")
    if not DB_PATH.exists():
        sys.exit(f"No tracker DB at {DB_PATH}.")

    conn = db.connect()
    try:
        if not db.is_bootstrapped(conn):
            sys.exit(f"{DB_PATH} holds no jobs -- nothing to archive.")

        if args.undo:
            rows = find_archived(conn)
            if not rows:
                print(f"No rows carry deleted_reason={REASON!r} -- nothing to undo.")
                return
            for row in rows:
                print(_show(row))
            with db.transaction(conn):
                restored = sum(db.restore(conn, r["key"]) for r in rows)
            print(f"\nRestored {restored} row(s) to live.")
            return

        entries = read_keys(args.keys_file)
        if not entries:
            sys.exit(f"{args.keys_file} lists no keys.")
        to_archive, engaged, already, missing = classify(
            conn, entries, args.include_touched)

        if missing:
            print(f"{len(missing)} key(s) are not in the DB -- skipping. A stale "
                  f"export or an edited URL is the usual cause:")
            for key, _ in missing:
                print(f"  {key[:100]}")
            print()

        if already:
            print(f"{len(already)} row(s) are already archived -- leaving alone.\n")

        if engaged:
            print(f"Skipping {len(engaged)} job(s) you have already engaged with "
                  f"(Status or Date Applied set) -- pass --include-touched to "
                  f"archive these too:")
            for row, reason in engaged:
                print(_show(row, reason))
            print()

        if not to_archive:
            print("Nothing left to archive.")
            return

        print(f"{len(to_archive)} row(s) to archive:")
        for row, reason in to_archive:
            print(_show(row, reason))

        if not args.apply:
            print(f"\nDry run -- nothing changed. Re-run with --apply to tombstone "
                  f"these {len(to_archive)} row(s).")
            return

        with db.transaction(conn):
            archived = sum(db.soft_delete(conn, r["key"], reason=REASON)
                           for r, _ in to_archive)
        counts = db.counts(conn)
        print(f"\nTombstoned {archived} row(s). Now {counts['live']} live, "
              f"{counts['deleted']} archived.")
        print("Reversible: python3 scripts/apply_archive.py --undo")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
