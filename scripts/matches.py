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


def row_key(website, title, company):
    """The tracker's dedup key: lowercased URL, falling back to 'title|company'
    when a listing has no URL. Mirrors filter_jobs.job_fingerprint.

    Lives here (stdlib only) rather than in export_workbook.py so db.py and the
    web app can key rows the same way without pulling in openpyxl.
    export_workbook re-exports it for the callers that already import it there.
    """
    key = (website or "").strip() or \
          f"{(title or '').strip()}|{(company or '').strip()}"
    return key.lower()


def simplify_loc(loc):
    """Collapse free-text locations into the buckets worth counting.

    Lives here, next to row_key and for the same reason: postings spell one
    city a dozen ways ("New York, NY", "NYC", "New York, NY, United States"),
    so the dashboard's location counts and the Jobs page's location filter have
    to agree on what "New York" means. db.py registers this as a SQLite
    function (see db.connect) to filter on it in SQL; pipeline_stats re-exports
    it. One definition, or the chip says 23 and the filtered list shows 19.

    Order matters: the first match wins, so a "Remote — New York, NY" posting
    counts as Remote. Unmatched locations fall through truncated, which keeps
    the dropdown from growing a separate entry per "…, United States" variant.
    """
    loc = (loc or "").strip()
    if not loc:             return "Unknown"
    if "Remote" in loc:     return "Remote (US)"
    if "New York" in loc or "NYC" in loc: return "New York, NY"
    if "San Francisco" in loc or "SF" in loc: return "San Francisco, CA"
    if "Boston" in loc:     return "Boston, MA"
    if "Austin" in loc:     return "Austin, TX"
    if "Seattle" in loc:    return "Seattle, WA"
    if "Switzerland" in loc or "Zurich" in loc: return "Switzerland"
    return loc[:35]


def month_day(d):
    """'Jul 3' from a date/datetime — cross-platform (strftime %-d is Linux-only)."""
    return f"{d:%b} {d.day}"


def found_day(value):
    """A stored date_processed ('2026-10-15 23:40', UTC) as the local day it
    happened on, 'Oct 15'. The pipeline stamps UTC, and an evening filter run
    in New York is already tomorrow in UTC — shown raw, a job found at 9 pm
    read as found the next day. Converted here, at display, so the stored
    values stay comparable and sort the same. Raises ValueError/TypeError on
    anything that isn't TS_FORMAT."""
    from datetime import datetime, timezone
    utc = datetime.strptime(str(value).strip(), TS_FORMAT).replace(tzinfo=timezone.utc)
    return month_day(utc.astimezone())
