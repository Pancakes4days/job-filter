#!/usr/bin/env python3
"""
Build/maintain matched_jobs.xlsx — a job-application tracker styled like a
hand-kept spreadsheet, fed from the pipeline's matched_jobs.csv.

Runs as the 'export' phase, between filter and copy:
    scrape -> filter (writes matched_jobs.csv) -> export (this) -> copy (.xlsx)

Append-only by design. New matches are added as new rows; rows already in the
workbook are never rewritten, so the columns you fill in by hand (Status,
Notes, dates, ...) survive every run. Dedup is by Website URL (fallback
title|company), mirroring filter_jobs.job_fingerprint.

Needs openpyxl (the one non-stdlib dependency in this project):
    pip install openpyxl

Usage:
    python3 export_workbook.py                       # csv + xlsx in this folder
    python3 export_workbook.py --csv matched.csv --out tracker.xlsx
    python3 export_workbook.py --no-color            # skip the Score color scale
"""

import argparse
import csv
import sys
from pathlib import Path

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font
except ImportError:
    sys.exit("export_workbook.py needs openpyxl — run: pip install openpyxl")

from paths import DATA_DIR  # noqa: E402

CSV_PATH = DATA_DIR / "matched_jobs.csv"
XLSX_PATH = DATA_DIR / "matched_jobs.xlsx"

TABLE_NAME = "JobMatches"
TABLE_STYLE = "TableStyleMedium2"   # blue accent (blue/white header + light-blue stripes)
# Data validations and the Score color scale are applied once over this whole
# range so appends never need to extend them (empty rows are simply ignored).
MAX_VALIDATION_ROW = 5000

# ── column layout ───────────────────────────────────────────────────────────────
# (header, csv_field or None for manual, width). csv_field maps a column to a
# matched_jobs.csv field; None means a blank column you fill in by hand.
COLUMNS = [
    ("Score",          "score",          8),
    ("Job Title",      "title",          50),
    ("Company",        "company",        20),
    ("Location",       "location",       18),
    ("Pay",            "salary",         12),
    ("Website",        "url",            16),   # rendered as a hyperlink
    ("Source",         "source",         18),
    ("Date Found",     "date_processed", 16),
    ("Why",            "reason",         45),
    ("Matched Skills", "matched_skills", 30),
    ("Concerns",       "concerns",       30),
    ("Application ID", None,             16),
    ("Cover Letter",   None,             16),
    ("Application",    None,             16),
    ("Date Applied",   None,             16),
    ("Due Date",       None,             12),
    ("Round #",        None,             10),
    ("Status",         None,             18),
    ("As of",          None,             12),
    ("Notes",          None,             30),
]
HEADERS = [c[0] for c in COLUMNS]
WEBSITE_COL = HEADERS.index("Website") + 1   # 1-based column index
TITLE_COL = HEADERS.index("Job Title") + 1
COMPANY_COL = HEADERS.index("Company") + 1

# Dropdowns carried over from the old tracker. Values must not contain commas
# (Excel's inline-list separator).
DROPDOWNS = {
    "Cover Letter": ["Required", "Required - ChatGPT", "Optional",
                     "Not Required", "Submitted"],
    "Application":  ["Applied", "Easy Applied"],
    "Status":       ["Applied", "Interview Scheduled", "Offer",
                     "Rejected", "In Progress", "Withdrawn"],
}


# ── helpers ─────────────────────────────────────────────────────────────────────

def row_key(website, title, company):
    """Dedup key for a row: URL if present, else title|company (lowercased)."""
    key = (website or "").strip() or f"{(title or '').strip()}|{(company or '').strip()}"
    return key.lower()


def read_matches(csv_path):
    """Read matched_jobs.csv (utf-8-sig, as filter_jobs.py writes it)."""
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def csv_row_to_cells(row):
    """Map a CSV record to a full-width list of cell values (manual cols blank)."""
    cells = []
    for _, field, _ in COLUMNS:
        if field is None:
            cells.append("")
        elif field == "score":
            try:
                cells.append(int(float(row.get("score", "") or 0)))
            except (TypeError, ValueError):
                cells.append(row.get("score", ""))
        else:
            cells.append(row.get(field, ""))
    return cells


def style_website_cell(cell):
    url = (cell.value or "").strip()
    if url:
        cell.hyperlink = url
        cell.font = Font(color="0563C1", underline="single")


# ── workbook creation / append ──────────────────────────────────────────────────

def create_workbook(use_color):
    wb = Workbook()
    ws = wb.active
    ws.title = "Matches"
    ws.append(HEADERS)
    # Header styling (bold white-on-blue) comes from the table style itself.
    ws.freeze_panes = "A2"
    for i, (_, _, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    _apply_validations(ws)
    if use_color:
        _apply_score_color(ws)
    return wb, ws


def _apply_validations(ws):
    for header, values in DROPDOWNS.items():
        col = get_column_letter(HEADERS.index(header) + 1)
        dv = DataValidation(type="list",
                            formula1='"' + ",".join(values) + '"',
                            allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{MAX_VALIDATION_ROW}")


def _apply_score_color(ws):
    col = get_column_letter(HEADERS.index("Score") + 1)
    rule = ColorScaleRule(
        start_type="num", start_value=0, start_color="FF6B6B",   # softened red
        mid_type="num",   mid_value=5,   mid_color="FFD966",     # softened yellow
        end_type="num",   end_value=10,  end_color="7AD151",     # softened lime green
    )
    ws.conditional_formatting.add(f"{col}2:{col}{MAX_VALIDATION_ROW}", rule)


def existing_keys(ws):
    """Set of dedup keys already present in the sheet (data rows only)."""
    keys = set()
    for r in range(2, ws.max_row + 1):
        website = ws.cell(row=r, column=WEBSITE_COL).value
        title = ws.cell(row=r, column=TITLE_COL).value
        company = ws.cell(row=r, column=COMPANY_COL).value
        if website or title or company:
            keys.add(row_key(website, title, company))
    return keys


def set_table_range(ws):
    """Create or resize the Excel Table to bound exactly the current data."""
    last_col = get_column_letter(len(COLUMNS))
    ref = f"A1:{last_col}{ws.max_row}"
    table = ws.tables.get(TABLE_NAME) if hasattr(ws.tables, "get") else None
    if table is not None:
        table.ref = ref
    else:
        table = Table(displayName=TABLE_NAME, ref=ref)
        table.tableStyleInfo = TableStyleInfo(
            name=TABLE_STYLE, showRowStripes=True, showColumnStripes=False)
        ws.add_table(table)


def main():
    ap = argparse.ArgumentParser(description="Build/append the job-tracker .xlsx.")
    ap.add_argument("--csv", default=str(CSV_PATH))
    ap.add_argument("--out", default=str(XLSX_PATH))
    ap.add_argument("--no-color", action="store_true",
                    help="Skip the red->green color scale on the Score column")
    args = ap.parse_args()

    matches = read_matches(Path(args.csv))
    if not matches:
        print(f"No rows in {args.csv} — nothing to export.")
        return

    out = Path(args.out)
    if out.exists():
        wb = load_workbook(out)
        ws = wb["Matches"] if "Matches" in wb.sheetnames else wb.active
        seen = existing_keys(ws)
    else:
        wb, ws = create_workbook(use_color=not args.no_color)
        seen = set()

    added = 0
    for row in matches:
        key = row_key(row.get("url"), row.get("title"), row.get("company"))
        if key in seen:
            continue
        seen.add(key)
        ws.append(csv_row_to_cells(row))
        style_website_cell(ws.cell(row=ws.max_row, column=WEBSITE_COL))
        added += 1

    set_table_range(ws)
    wb.save(out)
    print(f"{added} new row(s) added; workbook now has {ws.max_row - 1} matches "
          f"-> {out}")


if __name__ == "__main__":
    main()
