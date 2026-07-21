#!/usr/bin/env python3
"""
backup_db.py — nightly snapshot of the web tracker's SQLite DB.

    python3 scripts/backup_db.py                # snapshot + prune old ones
    python3 scripts/backup_db.py --keep 30      # retain more
    python3 scripts/backup_db.py --push         # also scp the snapshot to the laptop
    python3 scripts/backup_db.py --list         # show what's on disk

WHY THIS EXISTS FIRST (phase 0 of docs/PLAN_web_tracker.md): today the job data
survives an SD-card failure because the laptop holds a copy of the workbook.
The web tracker makes the laptop irrelevant — and with it, that redundancy.
This restores the property before the sync is removed, not after.

Uses sqlite3's online backup API, not a file copy: cp/rsync of a live SQLite DB
can capture a torn page set (worse in WAL mode, where recent commits live in a
separate -wal file), producing a backup that looks fine until you need it. The
backup API takes a consistent snapshot of a database being written to.

Each snapshot is verified with PRAGMA quick_check before it replaces the day's
file, so a corrupt backup fails loudly on the night it happens rather than the
day you try to restore.
"""

import argparse
import sqlite3
import sys
from datetime import datetime

from paths import BACKUP_DIR, DB_PATH

PREFIX = "tracker-"
SUFFIX = ".db"


def snapshot_path(stamp=None):
    stamp = stamp or datetime.now().strftime("%Y%m%d")
    return BACKUP_DIR / f"{PREFIX}{stamp}{SUFFIX}"


def existing_backups():
    """Snapshots on disk, oldest first. The %Y%m%d name sorts lexically as a
    date, so a plain sort is a chronological sort."""
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob(f"{PREFIX}*{SUFFIX}"))


def make_backup(dest):
    """Snapshot DB_PATH to dest via a temp file + rename, so an interrupted run
    never leaves a half-written file sitting where a good backup should be."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()

    check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity check: {result}")

    tmp.replace(dest)          # atomic within the same filesystem
    return dest


def prune(keep, protect):
    """Delete all but the newest `keep` snapshots. `protect` (the one just
    written) is never removed, so a misconfigured --keep can't discard the run
    that is currently succeeding."""
    backups = [b for b in existing_backups() if b != protect]
    excess  = len(backups) + 1 - keep
    removed = []
    for old in backups[:max(0, excess)]:
        old.unlink()
        removed.append(old)
    return removed


def push(path):
    """Copy a snapshot to the laptop — off-device backup without re-coupling
    the tracker to the laptop the way the workbook sync does. Imported lazily
    so a missing config/local.json only matters when --push is actually used."""
    from remote import load_local_config, remote_base, scp

    cfg = load_local_config()
    res = scp([str(path), remote_base(cfg)])
    return res.returncode == 0


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    ap = argparse.ArgumentParser(description="Back up the web tracker database.")
    ap.add_argument("--keep", type=int, default=14, help="Snapshots to retain (default 14)")
    ap.add_argument("--push", action="store_true", help="Also scp the snapshot to the laptop")
    ap.add_argument("--list", action="store_true", help="List snapshots and exit")
    args = ap.parse_args()

    if args.list:
        backups = existing_backups()
        if not backups:
            print(f"No snapshots in {BACKUP_DIR}")
            return
        for b in backups:
            print(f"  {b.name:<24} {human(b.stat().st_size):>8}")
        print(f"\n{len(backups)} snapshot(s) in {BACKUP_DIR}")
        return

    if args.keep < 1:
        sys.exit("--keep must be at least 1")

    # Phase 0 lands before phase 2 creates any data, so the timer will fire for
    # a while with no DB. That is expected, not a failure — exit 0 so systemd
    # doesn't report a nightly error until the tracker actually exists.
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH} yet — nothing to back up.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = make_backup(snapshot_path())
    print(f"Backed up {DB_PATH.name} -> {dest.name} ({human(dest.stat().st_size)})")

    for old in prune(args.keep, protect=dest):
        print(f"  pruned {old.name}")

    if args.push:
        if push(dest):
            print(f"  pushed {dest.name} to the laptop")
        else:
            # Don't fail the run: the local snapshot succeeded, and the laptop
            # being off the tailnet is routine. The next night retries.
            print("  push failed (laptop unreachable?) — local snapshot kept")


if __name__ == "__main__":
    main()
