#!/usr/bin/env python3
"""
Render matched_jobs.xlsx — a job-application tracker styled like a hand-kept
spreadsheet — from the tracker database (data/tracker.db).

Pure renderer (phase 6 of docs/PLAN_web_tracker.md). The DB is the system of
record; this produces a snapshot of the live (non-tombstoned) jobs on demand.
The web app serves it behind /export.xlsx, and it can be run by hand:

    python3 export_workbook.py                 # -> data/matched_jobs.xlsx
    python3 export_workbook.py --out other.xlsx
    python3 export_workbook.py --no-color

History: this used to append CSV rows onto a workbook pulled from the laptop,
using a sync watermark to tell hand-deletions from new matches. All of that
(watermark, held-count, pruned-keys, the laptop pull/push) went away with the
sync in phase 6 — a soft-deleted DB row simply isn't in `live_jobs`, so there is
nothing to infer.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font, PatternFill, Alignment
except ImportError:
    sys.exit("export_workbook.py needs openpyxl — run: pip install openpyxl")

# row_key lives in matches.py (stdlib only) so db.py and the web app can key
# rows without importing openpyxl. Re-exported here for callers that still do
# `from export_workbook import row_key`.
from matches import month_day, row_key  # noqa: F401 — re-export
from paths import DATA_DIR

XLSX_PATH = DATA_DIR / "matched_jobs.xlsx"

MAX_VALIDATION_ROW = 5000

# ── column layout ─────────────────────────────────────────────────────────────
# (workbook header, jobs-table column, width). Every column maps to a DB column
# now — the pipeline-owned ones and the user-owned ones alike — so rendering is
# a straight projection of a live jobs row.
COLUMNS = [
    ("Score",          "score",          8),
    ("Job Title",      "title",          50),
    ("Company",        "company",        20),
    ("Location",       "location",       18),
    ("Pay",            "salary",         12),
    ("Website",        "url",            16),
    ("Date Found",     "date_processed", 16),
    ("Date Applied",   "date_applied",   16),
    ("Why",            "reason",         45),
    ("Matched Skills", "matched_skills", 30),
    ("Concerns",       "concerns",       30),
    ("Application ID", "application_id", 16),
    ("Cover Letter",   "cover_letter",   16),
    ("Due Date",       "due_date",       12),
    ("Round #",        "round_num",      10),
    ("Status",         "status",         18),
    ("As of",          "as_of",          12),
    ("Notes",          "notes",          30),
]
HEADERS     = [c[0] for c in COLUMNS]
WEBSITE_COL = HEADERS.index("Website") + 1
TITLE_COL   = HEADERS.index("Job Title") + 1
COMPANY_COL = HEADERS.index("Company") + 1

# Option lists live in db.py (stdlib) so the web UI's <select>s and this
# workbook's data-validation dropdowns share one definition. Header-keyed here
# for the data-validation loop below; db keys them by jobs-table column name.
from db import USER_FIELD_OPTIONS  # noqa: E402

DROPDOWNS = {
    "Cover Letter": USER_FIELD_OPTIONS["cover_letter"],
    "Status":       USER_FIELD_OPTIONS["status"],
}

HEADER_BG = "FF1F3864"
HEADER_FG = "FFFFFFFF"


# ── helpers ───────────────────────────────────────────────────────────────────

def fmt_date(val):
    """Format date_processed to clean 'Jun 17' style."""
    if not val:
        return ""
    try:
        from datetime import datetime
        return month_day(datetime.strptime(str(val).strip()[:10], "%Y-%m-%d"))
    except Exception:
        return str(val)[:10]


def db_row_to_cells(row):
    """Project one live jobs row (sqlite3.Row or dict) onto the workbook columns."""
    def get(field):
        try:
            return row[field]
        except (KeyError, IndexError):
            return None

    cells = []
    for _, field, _ in COLUMNS:
        val = get(field)
        if field == "score":
            cells.append(val if val is not None else "")
        elif field == "date_processed":
            cells.append(fmt_date(val))
        else:
            cells.append("" if val is None else str(val))
    return cells


def style_website_cell(cell):
    """Use HYPERLINK formula — avoids openpyxl's broken inline xmlns:r on hyperlinks."""
    url = (cell.value or "").strip()
    if url:
        safe       = url.replace('"', '%22')
        cell.value = f'=HYPERLINK("{safe}","Link")'
        cell.font  = Font(color="0563C1", underline="single")


def strip_hyperlink(value):
    """Raw URL from a Website cell, which style_website_cell wrote as
    =HYPERLINK("url","Link"). Needed anywhere the workbook is read back —
    export's dedup and bootstrap_from_workbook's import both key on the URL.

    Only correct when the workbook is loaded with data_only=False (the
    default): with data_only=True openpyxl returns the cached display text
    ("Link") and the URL is simply not there to recover.
    """
    if isinstance(value, str) and value.startswith("=HYPERLINK"):
        try:
            return value.split('"')[1]
        except IndexError:
            pass
    return value


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


# ── rendering ─────────────────────────────────────────────────────────────────

def render_workbook(rows, use_color=True):
    """A styled Workbook holding `rows` (live jobs, sqlite3.Row or dicts), best
    scores first. Pure — no I/O — so the web app can save it to a BytesIO and
    the CLI to a file. Callers pass already-filtered live rows; tombstones never
    reach here."""
    wb, ws = create_workbook(use_color=use_color)
    for row in rows:
        ws.append(db_row_to_cells(row))
        style_website_cell(ws.cell(row=ws.max_row, column=WEBSITE_COL))
    return wb


def main():
    ap = argparse.ArgumentParser(
        description="Render the job-tracker .xlsx from the tracker database.")
    ap.add_argument("--out",      default=str(XLSX_PATH))
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    import db  # local: keeps openpyxl-free importers of this module unaffected

    if not db.DB_PATH.exists():
        sys.exit(f"No tracker database at {db.DB_PATH} — run "
                 f"bootstrap_from_workbook.py first.")

    # Read-write, not mode=ro: a read-only connection can't build the WAL index
    # and fails against a DB the pipeline has open (same reason web/app.py does
    # this). The existence guard above means connect() won't create a new DB.
    conn = db.connect()
    try:
        rows = db.live_jobs(conn)
    finally:
        conn.close()

    out = Path(args.out)
    wb = render_workbook(rows, use_color=not args.no_color)
    wb.save(out)
    print(f"Rendered {len(rows)} live job(s) -> {out}")


if __name__ == "__main__":
    main()
