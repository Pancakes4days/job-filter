"""Shared helpers for the matched-jobs records (data/matched_jobs.csv).

filter_jobs.py writes the CSV; export_workbook.py, pipeline_stats.py and
jobs_left.py read it back. The schema and the header-tolerant reader live
here so they can't drift apart again. Stdlib only — pipeline_stats must
stay importable without openpyxl.
"""

import csv

# Field order written by filter_jobs.py (QUOTE_ALL, with a header row).
CSV_FIELDS = [
    "date_processed", "title", "company", "location", "salary", "url", "source",
    "score", "suitable", "matched_skills", "concerns", "reason",
]

# date_processed format — ALSO the sync-watermark contract: export_workbook
# compares these strings lexically (ts <= mark), which is only a valid time
# ordering because this format is fixed-width, zero-padded, and
# most-significant-first. Changing it silently breaks the watermark
# (deletions resurrect or new matches get held).
TS_FORMAT = "%Y-%m-%d %H:%M"


def read_matches(csv_path):
    """Rows of the matches CSV (with or without a header row), skipping
    malformed rows: no title AND no url, or a non-numeric / out-of-range
    score (usually field misalignment from a hand-edited line)."""
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        first = f.readline(); f.seek(0)
        # filter_jobs.py writes with QUOTE_ALL, so the header arrives quoted
        has_header = first.strip().lstrip('"').startswith("date_processed")
        reader = csv.DictReader(f, fieldnames=None if has_header else CSV_FIELDS)
        rows = []
        for row in reader:
            title = (row.get("title") or "").strip()
            url   = (row.get("url")   or "").strip()
            if not title and not url:
                continue
            try:
                score = int(float(row.get("score", "") or 0))
            except (TypeError, ValueError):
                continue
            if not 0 <= score <= 10:
                continue
            rows.append(row)
        return rows


def month_day(d):
    """'Jul 3' from a date/datetime — cross-platform (strftime %-d is Linux-only)."""
    return f"{d:%b} {d.day}"
