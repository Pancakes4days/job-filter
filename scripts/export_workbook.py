#!/usr/bin/env python3
"""
Build/maintain matched_jobs.xlsx — a job-application tracker styled like a
hand-kept spreadsheet, fed from the pipeline's matched_jobs.csv.

Append-only by design. New matches are added as new rows; rows already in the
workbook are never rewritten, so the columns you fill in by hand (Status,
Notes, dates, ...) survive every run. Dedup is by Website URL (fallback
title|company), mirroring filter_jobs.job_fingerprint.

Usage:
    python3 export_workbook.py
    python3 export_workbook.py --csv matched.csv --out tracker.xlsx
    python3 export_workbook.py --no-color
"""

import argparse
import csv
import sys
from pathlib import Path

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font, PatternFill, Alignment
except ImportError:
    sys.exit("export_workbook.py needs openpyxl — run: pip install openpyxl")

from paths import DATA_DIR
from prune_workbook import prune_workbook

CSV_PATH    = DATA_DIR / "matched_jobs.csv"
XLSX_PATH   = DATA_DIR / "matched_jobs.xlsx"
PRUNED_PATH = DATA_DIR / "pruned_keys.txt"

MAX_VALIDATION_ROW = 5000

# Fields written by filter_jobs.py (no header row in CSV output)
CSV_FIELDS = [
    "date_processed", "title", "company", "location", "salary", "url", "source",
    "score", "suitable", "matched_skills", "concerns", "reason",
]

# ── column layout ─────────────────────────────────────────────────────────────
# (header, csv_field or None for manual columns, width)
COLUMNS = [
    ("Score",          "score",          8),
    ("Job Title",      "title",          50),
    ("Company",        "company",        20),
    ("Location",       "location",       18),
    ("Pay",            "salary",         12),
    ("Website",        "url",            16),
    ("Date Found",     "date_processed", 16),
    ("Date Applied",   None,             16),
    ("Why",            "reason",         45),
    ("Matched Skills", "matched_skills", 30),
    ("Concerns",       "concerns",       30),
    ("Application ID", ".",              16),   # auto-filled with "." to block overflow
    ("Cover Letter",   None,             16),
    ("Due Date",       None,             12),
    ("Round #",        None,             10),
    ("Status",         None,             18),
    ("As of",          None,             12),
    ("Notes",          None,             30),
]
HEADERS     = [c[0] for c in COLUMNS]
WEBSITE_COL = HEADERS.index("Website") + 1
TITLE_COL   = HEADERS.index("Job Title") + 1
COMPANY_COL = HEADERS.index("Company") + 1

DROPDOWNS = {
    "Cover Letter": ["Required", "Required - ChatGPT", "Optional",
                     "Not Required", "Submitted"],
    "Status":       ["Applied", "Interview Scheduled", "Offer",
                     "Rejected", "In Progress", "Withdrawn"],
}

HEADER_BG = "FF1F3864"
HEADER_FG = "FFFFFFFF"


# ── helpers ───────────────────────────────────────────────────────────────────

def row_key(website, title, company):
    key = (website or "").strip() or \
          f"{(title or '').strip()}|{(company or '').strip()}"
    return key.lower()


def fmt_date(val):
    """Format date_processed to clean 'Jun 17 2026' style."""
    if not val:
        return ""
    try:
        from datetime import datetime
        return datetime.strptime(str(val).strip()[:10], "%Y-%m-%d").strftime("%b %-d")
    except Exception:
        return str(val)[:10]


def read_matches(csv_path):
    """Read CSV with or without a header row, skipping malformed rows."""
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        first = f.readline(); f.seek(0)
        has_header = first.strip().startswith("date_processed")
        reader = csv.DictReader(
            f, fieldnames=None if has_header else CSV_FIELDS)
        rows = []
        for row in reader:
            # Skip rows with no title and no URL
            title = (row.get("title") or "").strip()
            url   = (row.get("url")   or "").strip()
            if not title and not url:
                continue
            # Skip rows where score is not numeric (field misalignment in CSV)
            try:
                score = int(float(row.get("score", "") or 0))
                if score < 0 or score > 10:
                    continue
            except (TypeError, ValueError):
                continue
            rows.append(row)
        return rows


def csv_row_to_cells(row):
    cells = []
    for _, field, _ in COLUMNS:
        if field is None:
            cells.append("")
        elif field == ".":
            cells.append(".")
        elif field == "score":
            try:
                cells.append(int(float(row.get("score", "") or 0)))
            except (TypeError, ValueError):
                cells.append(row.get("score", ""))
        elif field == "date_processed":
            cells.append(fmt_date(row.get("date_processed", "")))
        else:
            cells.append(row.get(field, ""))
    return cells


def style_website_cell(cell):
    """Use HYPERLINK formula — avoids openpyxl's broken inline xmlns:r on hyperlinks."""
    url = (cell.value or "").strip()
    if url:
        safe       = url.replace('"', '%22')
        cell.value = f'=HYPERLINK("{safe}","Link")'
        cell.font  = Font(color="0563C1", underline="single")


def existing_keys(ws):
    keys = set()
    for r in range(2, ws.max_row + 1):
        website = ws.cell(row=r, column=WEBSITE_COL).value
        title   = ws.cell(row=r, column=TITLE_COL).value
        company = ws.cell(row=r, column=COMPANY_COL).value
        # Strip HYPERLINK formula so key matches raw URL from CSV
        if isinstance(website, str) and website.startswith("=HYPERLINK"):
            try:
                website = website.split('"')[1]
            except IndexError:
                pass
        if website or title or company:
            keys.add(row_key(website, title, company))
    return keys


# ── workbook creation ─────────────────────────────────────────────────────────

def create_workbook(use_color):
    wb = Workbook()
    ws = wb.active
    ws.title = "Matches"

    # Header row with dark navy styling
    ws.append(HEADERS)
    for i, hdr in enumerate(HEADERS, start=1):
        cell            = ws.cell(row=1, column=i)
        cell.font       = Font(bold=True, color=HEADER_FG, name="Arial", size=10)
        cell.fill       = PatternFill("solid", fgColor=HEADER_BG)
        cell.alignment  = Alignment(horizontal="center", vertical="center")

    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"

    # Auto-filter (filter dropdowns without the problematic Table XML)
    last_col = get_column_letter(len(COLUMNS))
    ws.auto_filter.ref = f"A1:{last_col}1"

    # Column widths
    for i, (_, _, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    _apply_validations(ws)
    if use_color:
        _apply_score_color(ws)

    return wb, ws


def _apply_validations(ws):
    for header, values in DROPDOWNS.items():
        col = get_column_letter(HEADERS.index(header) + 1)
        dv  = DataValidation(
            type="list",
            formula1='"' + ",".join(values) + '"',
            allow_blank=True,
        )
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{MAX_VALIDATION_ROW}")


def _apply_score_color(ws):
    col  = get_column_letter(HEADERS.index("Score") + 1)
    rule = ColorScaleRule(
        start_type="num", start_value=0,  start_color="FF6B6B",
        mid_type="num",   mid_value=5,    mid_color="FFD966",
        end_type="num",   end_value=10,   end_color="7AD151",
    )
    ws.conditional_formatting.add(f"{col}2:{col}{MAX_VALIDATION_ROW}", rule)


# ── main ──────────────────────────────────────────────────────────────────────

def _load_pruned_keys():
    if not PRUNED_PATH.exists():
        return set()
    with open(PRUNED_PATH, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _append_pruned_keys(new_keys):
    with open(PRUNED_PATH, "a", encoding="utf-8") as f:
        for k in new_keys:
            f.write(k + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Build/append the job-tracker .xlsx from matched_jobs.csv.")
    ap.add_argument("--csv",      default=str(CSV_PATH))
    ap.add_argument("--out",      default=str(XLSX_PATH))
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    matches = read_matches(Path(args.csv))
    if not matches:
        print(f"No rows in {args.csv} — nothing to export.")
        return

    pruned = _load_pruned_keys()

    out = Path(args.out)
    if out.exists():
        wb   = load_workbook(out)
        ws   = wb["Matches"] if "Matches" in wb.sheetnames else wb.active
        seen = existing_keys(ws)
    else:
        wb, ws = create_workbook(use_color=not args.no_color)
        seen   = set()

    seen |= pruned  # don't re-add jobs that were previously pruned

    added = 0
    for row in matches:
        key = row_key(row.get("url"), row.get("title"), row.get("company"))
        if key in seen:
            continue
        seen.add(key)
        ws.append(csv_row_to_cells(row))
        style_website_cell(ws.cell(row=ws.max_row, column=WEBSITE_COL))
        added += 1

    # Update auto_filter range to cover all data rows
    last_col = get_column_letter(len(COLUMNS))
    ws.auto_filter.ref = f"A1:{last_col}1"

    deleted_keys, kept = prune_workbook(ws, row_key)
    if deleted_keys:
        _append_pruned_keys(deleted_keys)

    wb.save(out)
    print(f"{added} new row(s) added; {len(deleted_keys)} pruned; "
          f"workbook now has {kept} matches -> {out}")


if __name__ == "__main__":
    main()
