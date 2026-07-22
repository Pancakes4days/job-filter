#!/usr/bin/env python3
"""
prune_workbook.py — trim the tracker down to the best 1–2 jobs per company,
protecting rows you've started applying to.

MANUAL TOOL — run it by hand when the tracker gets noisy. The automated pipeline
never deletes rows.

    python3 scripts/prune_workbook.py            # dry-run report, no writes
    python3 scripts/prune_workbook.py --apply    # soft-delete the pruned rows

As of phase 6 (docs/PLAN_web_tracker.md) this operates on the tracker DB, not a
workbook: --apply writes a tombstone (deleted_reason='prune') for each pruned
row, in one transaction. A tombstoned row is excluded from the site and from
export_workbook's render, and the pipeline's ON CONFLICT(key) DO NOTHING never
re-adds it — so there is no suppress-list to maintain and no laptop to push to.
Restore any row from the job's page in the web UI if a prune was too aggressive.

Design notes:
  * plan_prunes() is pure — it decides which keys to delete; the DB write lives
    only in the CLI below, so the heuristics are testable in isolation.
  * Rows with a real user value in any PROTECT column are never deleted. The
    old workbook auto-filled "Application ID" with "." as a spacer, so "." still
    counts as empty (bootstrap carried that convention into the DB).

Candidate profile baked into the rules: May 2027 grad targeting Summer 2027
internships and new-grad roles. Strength order: security > Applied AI/ML >
DevOps/SRE/infra > backend/full-stack. Cuts senior/2+yr/PhD/quant/non-engineering
and unacceptable-location rows. Edit the keyword lists below to retune.
"""
import argparse
import re
from collections import defaultdict

import db

KEEP_PER_COMPANY = 2

# A real value in any of these user-owned columns protects a row from pruning.
# This is exactly db.USER_FIELDS — kept in sync by referencing it directly.
PROTECT_COLS = db.USER_FIELDS
# "application_id" was auto-filled with "." by the old workbook exporter as a
# spacer; "" and "." both count as empty so they never falsely protect a row.
PLACEHOLDERS = {"", "."}

# ── location ──────────────────────────────────────────────────────────────────
ACCEPTABLE_LOC = ["new york", "nyc", "long island", "manhattan", "brooklyn",
                  "florida", "virginia", "boston", "massachusetts", "colorado",
                  "denver", "switzerland", "zurich", "riva", "spain",
                  "san francisco", "bay area", "palo alto", "remote",
                  "united states"]
NYC = ["new york", "nyc", "long island", "manhattan", "brooklyn"]
MID = ["florida", "virginia", "boston", "massachusetts", "colorado", "denver",
       "switzerland", "zurich", "riva", "spain"]

# ── exclusion patterns ────────────────────────────────────────────────────────
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|\blead\b|manager|director|"
                    r"\bhead\b|\bvp\b|vice president|chief|architect|fellow)\b", re.I)
LEVEL3 = re.compile(r"engineer\s*(3|iii)\b", re.I)
YEARS  = re.compile(r"(\d+)\s*\+?\s*(?:[-–]\s*\d+\s*)?\s*(?:years|yrs|yoe)\b", re.I)
PHD_TITLE = re.compile(r"\bph\.?\s*d\b", re.I)
# New-grad/intern signal must be in the TITLE to override a senior title. The LLM's
# Why/Concerns text says "new grad" in NEGATIVE contexts ("outside the new grad
# timeframe"), so a blob match is not a reliable rescue.
NEWGRAD_TITLE = re.compile(r"new\s*grad|entry[- ]level|\bintern(ship)?\b|"
                           r"university (grad|program)|graduate (program|engineer|rotational)|"
                           r"\bgrad program\b|2027 grad|co[- ]?op\b", re.I)
WRONGCYCLE = re.compile(r"grad\w*\s+before\s+may\s+2027|before may 2027|"
                        r"fall\s*2026\s*start|summer\s*2026|experienced professionals", re.I)
DEGREE = re.compile(r"\b(ms|m\.s\.|master'?s|phd|ph\.d\.)\b[^.]*\brequired", re.I)
# HARD non-engineering: never an SWE role even if the title contains "engineer".
HARD_NONENG = re.compile(
    r"account executive|account manager|\bsales\b|sales engineer|solutions? engineer|"
    r"business development|partner development|\brepresentative\b|\brecruiter\b|\bsourcer\b|\btalent\b|"
    r"\battorney\b|\bcounsel\b|\blegal\b|\bmarketing\b|product designer|gtm engineer|"
    r"developer advocate|\badvocate\b|community growth|revenue enablement|\benablement\b|"
    r"\bconsultant\b|customer success|customer experience|people ops|ai trainer", re.I)
# SOFT non-engineering: non-eng UNLESS the title is a genuine engineering IC role.
SOFT_NONENG = re.compile(
    r"\banalyst\b|\bscientist\b|\bresearcher\b|\bassociate\b|\boperations\b|"
    r"\bcoordinator\b|\bspecialist\b|\bstrategist\b|\btrader\b|\btrading\b|\btrainer\b|"
    r"equity research|research analyst|data scientist|applied scientist|research scientist|"
    r"\bdesigner\b|\bfinance\b|fp&a|\binvestment\b|treasury|finops|fin ops|"
    r"business analyst|business systems|business automation|ops analyst|"
    r"\bbilling\b|accounts receivable|\bproducer\b|\beducator\b|\bcompliance\b|"
    r"product manage|program manage|customer data|\bsalesforce\b|"
    r"support engineer|technical support|solutions? architect", re.I)
SWE_OK = re.compile(r"software engineer|design engineer|full[- ]?stack|backend|"
                    r"front[- ]?end|web application", re.I)
QUANT = re.compile(r"quantitative (trader|researcher|analyst)|quant researcher", re.I)
EXPLICIT_2027 = re.compile(r"2027|summer 2027|2027 grad", re.I)

# ── fit tiers (lower = better) ───────────────────────────────────────────────
T_AI  = re.compile(r"applied ai|ai engineer|machine learning|\bml\b|ml ops|mlops|"
                   r"\bnlp\b|\bllm\b|gen ?ai|ai platform", re.I)
T_SWE = re.compile(r"software engineer|full[- ]?stack|backend|front[- ]?end|"
                   r"web application|developer", re.I)
T_INF = re.compile(r"devops|platform engineer|site reliability|\bsre\b|infrastructure|"
                   r"reliability engineer|linux engineer|environment platform|"
                   r"apollo|cloud operations", re.I)
T_SEC = re.compile(r"security|appsec|infosec|vulnerability", re.I)


def _title_newgrad(title):
    return bool(NEWGRAD_TITLE.search(title))


def _eng_ok(title):
    # genuine engineering IC role -> rescue from a SOFT non-engineering match
    return bool(SWE_OK.search(title) or T_AI.search(title) or T_SWE.search(title)
                or T_INF.search(title) or T_SEC.search(title))


def _is_senior(title):
    tl = title.lower()
    # "Member of Technical Staff" is an IC title at AI labs, not a senior level.
    chk = tl.replace("technical staff", "") if "technical staff" in tl else title
    return bool(SENIOR.search(chk)) or bool(LEVEL3.search(title))


def _hard_excluded(title, location, why, concerns):
    blob = " ".join([title, why, concerns])
    ng = _title_newgrad(title)
    loc = location.lower()
    if WRONGCYCLE.search(blob): return "wrong cycle"
    if PHD_TITLE.search(title): return "PhD role"
    if _is_senior(title) and not ng: return "too senior"
    m = YEARS.search(title)
    if m and int(m.group(1)) >= 2 and not ng: return "needs 2+ yrs"
    if DEGREE.search(blob): return "advanced degree required"
    if HARD_NONENG.search(title): return "non-engineering"
    if SOFT_NONENG.search(title) and not _eng_ok(title): return "non-engineering"
    if QUANT.search(title) and not re.search(r"developer|intern|new grad", title, re.I):
        return "quant specialist"
    if not any(k in loc for k in ACCEPTABLE_LOC): return "location"
    return None


def _fit_tier(title):
    if T_SEC.search(title):  return 0
    if T_AI.search(title): return 1
    if T_INF.search(title): return 2
    if T_SWE.search(title): return 3
    return 4


def _loc_rank(location):
    loc = location.lower()
    if any(k in loc for k in NYC): return 0
    if any(k in loc for k in MID): return 1
    return 2


def _score_val(s):
    try: return float(s)
    except (TypeError, ValueError): return 0.0


# ── pruning plan ──────────────────────────────────────────────────────────────

def _g(row, field):
    """Field of a jobs row (sqlite3.Row or dict) as a string, '' if absent/NULL."""
    try:
        v = row[field]
    except (KeyError, IndexError):
        v = None
    return "" if v is None else str(v)


def _is_protected(row):
    return any(_g(row, c).strip().lower() not in PLACEHOLDERS for c in PROTECT_COLS)


def _sort_key(row):
    title = _g(row, "title")
    blob  = " ".join([title, _g(row, "reason"), _g(row, "concerns")])
    cyc   = 0 if EXPLICIT_2027.search(blob) else 1
    return (_fit_tier(title), _loc_rank(_g(row, "location")), cyc,
            -_score_val(_g(row, "score")))


def plan_prunes(rows):
    """Decide which live rows to prune to the best KEEP_PER_COMPANY per company.

    `rows` are jobs rows (the DB's live set). Pure: returns (delete_keys,
    kept_count). Rows with any hand-typed user value are protected and never
    pruned; among the rest, hard-excluded rows are dropped and the best
    survivors per company are kept.
    """
    by_company = defaultdict(list)
    for row in rows:
        by_company[_g(row, "company")].append(row)

    delete_keys = []
    kept = 0
    for _company, items in by_company.items():
        protected = [r for r in items if _is_protected(r)]
        cand      = [r for r in items if not _is_protected(r)]
        survivors = [r for r in cand
                     if not _hard_excluded(_g(r, "title"), _g(r, "location"),
                                           _g(r, "reason"), _g(r, "concerns"))]
        survivors.sort(key=_sort_key)
        keep      = survivors[:KEEP_PER_COMPANY]
        keep_keys = {r["key"] for r in protected} | {r["key"] for r in keep}
        kept += len(keep_keys)
        for r in items:
            if r["key"] not in keep_keys:
                delete_keys.append(r["key"])
    return delete_keys, kept


# ── standalone CLI ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Trim the tracker to the best 1–2 roles per company by "
                    "soft-deleting the rest (rows with hand-typed values are "
                    "never deleted).")
    ap.add_argument("--apply", action="store_true",
                    help="Write the tombstones (default is a dry-run report)")
    args = ap.parse_args()

    if not db.DB_PATH.exists():
        raise SystemExit(f"No tracker database at {db.DB_PATH} — nothing to prune.")

    conn = db.connect()
    try:
        rows = db.live_jobs(conn)
        before = len(rows)
        delete_keys, _kept = plan_prunes(rows)
        print(f"  live rows: {before} -> {before - len(delete_keys)} "
              f"(prune {len(delete_keys)}; hand-edited rows are protected)")

        if not args.apply:
            print("Dry run — nothing written. Re-run with --apply to prune for real.")
            return

        with db.transaction(conn):
            deleted = sum(db.soft_delete(conn, k, reason="prune") for k in delete_keys)
        print(f"Soft-deleted {deleted} row(s) (deleted_reason='prune'). "
              f"Restore any of them from the job's page in the web UI.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
