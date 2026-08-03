#!/usr/bin/env python3
"""
Seed a throwaway tracker DB — plus the pipeline state files the web app reads —
so the site can be run and restyled on a machine that has no pipeline.

    python3 scripts/seed_dev_db.py          # seed, then: python3 web/app.py
    python3 scripts/seed_dev_db.py --reset  # wipe the dev data and re-seed

WHY THIS EXISTS
The web app refuses to render without data/tracker.db (see app.require_db), and
that file is gitignored and lives only on the Pi. Without it every page on a dev
box is the "not bootstrapped" notice, so CSS and template work had to be done
blind and verified after a deploy. This makes the whole UI reachable locally.

It seeds more than the jobs table on purpose: the states that are hardest to
style are the ones you never see by accident. The orchestrator state and a
half-scored scrape make the status strip render its `is-running` variant with a
live ETA; recruitment_alerts.json makes the dashboard's alert card appear; a
filter.log makes the log page non-empty. Tombstoned rows fill the Archive view.
The fixtures also carry deliberately awkward content — a 120-character title, a
company name with no spaces, an empty location, a null score, a job with no URL
— because those are what actually break a layout.

The row set is generated from a fixed random seed, so two runs produce the same
data and two screenshots are comparable.

SAFETY
This writes into data/, which on the Pi holds the real pipeline state. It
refuses to touch anything it did not create: a tracker.db with rows in it, or
existing state files, abort the run unless data/.dev_seed marks the directory as
a dev scratch copy. Never run this on the Pi.
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from matches import TS_FORMAT
from paths import DATA_DIR, DB_PATH

# Marks data/ as a dev scratch directory. Its absence next to real-looking files
# is what makes the guards below refuse — see the SAFETY note above.
SENTINEL = DATA_DIR / ".dev_seed"

SCRAPED = DATA_DIR / "scraped_jobs.json"
SEEN    = DATA_DIR / "seen_jobs.txt"
STATE   = DATA_DIR / "orchestrator_state.json"
ALERTS  = DATA_DIR / "recruitment_alerts.json"
LOG     = DATA_DIR / "filter.log"

STATE_FILES = (SCRAPED, SEEN, STATE, ALERTS, LOG)


# ── fixtures ──────────────────────────────────────────────────────────────────

COMPANIES = [
    "Palantir", "Anthropic", "Stripe", "Datadog", "Ramp", "Figma", "Vercel",
    "Cloudflare", "Databricks", "Jane Street", "Two Sigma", "Notion",
    "ReallyLongCompanyNameWithNoSpacesAtAllLtd",
]

TITLES = [
    "Software Engineer, New Grad",
    "Backend Engineer (Python)",
    "Platform Engineer — Infrastructure",
    "Data Engineer, Analytics",
    "Machine Learning Engineer I",
    "Full Stack Engineer",
    "Site Reliability Engineer, New Grad",
    "Associate Software Engineer",
    "Software Development Engineer, Distributed Storage Systems and Query "
    "Execution, University Graduate Program 2027 Start (Multiple Locations)",
]

LOCATIONS = [
    "Remote (US)", "New York, NY", "San Francisco, CA", "Boston, MA",
    "Austin, TX", "Seattle, WA", "Zurich, Switzerland", "Denver, CO", "",
]

SALARIES = ["$120k – $160k", "$140,000/yr", "", "$95k base + equity", ""]

NOTES = [
    "",
    "Referred by Sam — follow up if nothing by Friday.",
    "Recruiter said the team is still figuring out headcount. Worth a nudge in "
    "two weeks; the JD has been up since March and the posting keeps getting "
    "refreshed, which usually means they haven't filled it.",
    "OA sent, 90 minutes, expires in 5 days.",
]

REASONS = [
    "Strong overlap with the Python/SQL core and the posting explicitly wants "
    "new grads; no security clearance requirement.",
    "Mostly matches, but the stack is Java-first and the role leans senior.",
    "Listed as new-grad but the requirements read like 3–5 years of experience.",
]

SKILLS   = ["Python, SQL, Docker", "Go, Kubernetes, Terraform", "Python, PyTorch",
            "TypeScript, React, Postgres", ""]
CONCERNS = ["", "Requires US citizenship", "On-site 5 days/week",
            "No salary listed; posting is 6 months old"]
SOURCES  = ["greenhouse:stripe", "lever:ramp", "workday:datadog", "rss", "ashby:vercel"]


def _fixture_rows(rng, n_live=42, n_archived=6):
    """Generate (record, user_fields, deleted) tuples with a spread wide enough
    to exercise every branch the templates have: each score bucket, each status
    option plus untouched rows, every simplify_loc bucket, and the null/empty
    cases the real data hits rarely."""
    statuses = [None] + db.USER_FIELD_OPTIONS["status"]
    covers   = [None] + db.USER_FIELD_OPTIONS["cover_letter"]
    now      = datetime.now()

    rows = []
    for i in range(n_live + n_archived):
        company = rng.choice(COMPANIES)
        title   = rng.choice(TITLES)
        # A URL-less posting keys off "title|company" — worth having one so the
        # detail page's missing-link branch and the long key display get seen.
        url     = "" if i == 7 else f"https://jobs.{company.split()[0].lower()}.com/postings/{1000 + i}"
        found   = now - timedelta(days=rng.randint(0, 45), minutes=rng.randint(0, 1440))

        # One null score renders as the '—' branch; the rest weight toward the
        # threshold so the distribution chart has a believable shape.
        score = None if i == 3 else min(10, max(0, int(rng.gauss(6.5, 2))))

        rec = {
            "key":            db.row_key(url, title, company),
            "url":            url,
            "title":          "" if i == 11 else title,
            "company":        company,
            "location":       rng.choice(LOCATIONS),
            "salary":         rng.choice(SALARIES),
            "source":         rng.choice(SOURCES),
            "score":          score,
            "suitable":       None if score is None else int(score >= 6),
            "matched_skills": rng.choice(SKILLS),
            "concerns":       rng.choice(CONCERNS),
            "reason":         rng.choice(REASONS),
            "date_processed": found.strftime(TS_FORMAT),
        }

        status = rng.choice(statuses)
        user = {
            "status":         status or "",
            "notes":          rng.choice(NOTES),
            "cover_letter":   rng.choice(covers) or "",
            "date_applied":   (found + timedelta(days=2)).strftime("%Y-%m-%d") if status else "",
            "due_date":       (now + timedelta(days=rng.randint(1, 20))).strftime("%Y-%m-%d") if status else "",
            "round_num":      str(rng.randint(1, 3)) if status == "Interview Scheduled" else "",
            "as_of":          now.strftime("%Y-%m-%d") if status else "",
            # The pipeline parks "." here as a spacer; job.html hides it. Seed one.
            "application_id": "." if i == 5 else (f"APP-{2000 + i}" if status else ""),
        }
        rows.append((rec, user, i >= n_live))
    return rows


def seed_db(conn, rng):
    db.init_db(conn)
    conn.execute("DELETE FROM jobs")
    stamp = db.now_iso()
    live = archived = 0
    for rec, user, is_archived in _fixture_rows(rng):
        db.insert_new(
            conn, rec, user_fields=user,
            deleted_at=stamp if is_archived else None,
            # Both reasons appear so the Archive banner's wording is exercised.
            deleted_reason=("user" if archived % 2 == 0 else "import-csv") if is_archived else None,
        )
        archived += is_archived
        live += not is_archived
    db.set_meta(conn, "bootstrapped_at", stamp)
    db.set_meta(conn, "dev_seed", stamp)
    return live, archived


def seed_state_files(rng):
    """Write the non-DB inputs the dashboard and status strip read.

    scraped_jobs.json and seen_jobs.txt are deliberately inconsistent with each
    other — 140 scraped, 95 marked seen — because that is what makes
    pipeline_stats.progress() report a run in flight, which is the only way to
    see the strip's `is-running` styling and its ETA without waiting for a real
    scrape to start."""
    total, scored = 140, 95
    jobs = [{"url": f"https://example.com/j/{i}",
             "title": rng.choice(TITLES), "company": rng.choice(COMPANIES),
             "location": rng.choice(LOCATIONS), "source": rng.choice(SOURCES)}
            for i in range(total)]
    SCRAPED.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    # load_seen() reads whitespace-separated fingerprints; job_fingerprint is
    # imported here rather than at module scope to keep this script's imports
    # stdlib-shallow for the DB-only path.
    from filter_jobs import job_fingerprint
    SEEN.write_text("\n".join(job_fingerprint(j) for j in jobs[:scored]) + "\n",
                    encoding="utf-8")

    STATE.write_text(json.dumps({
        "phase":    "filter",
        "next_run": (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
    }), encoding="utf-8")

    today = datetime.now().date()
    ALERTS.write_text(json.dumps([
        {"company": "Jane Street", "count": 3,
         "first_seen": str(today - timedelta(days=1)),
         "expires":    str(today + timedelta(days=6)),
         "sample_roles": ["Software Engineer — New Grad 2027",
                          "Quantitative Trader, Campus",
                          "Systems Engineer (Graduate)"]},
        {"company": "Figma", "count": 1,
         "first_seen": str(today - timedelta(days=3)),
         "expires":    str(today + timedelta(days=4)),
         "sample_roles": ["Product Engineer, University Grad"]},
    ]), encoding="utf-8")

    lines = []
    for i in range(400):
        ts = (datetime.now() - timedelta(minutes=400 - i)).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} INFO  scored {rng.choice(TITLES)[:40]!r} "
                     f"@ {rng.choice(COMPANIES)} -> {rng.randint(0, 10)}/10")
    lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} WARN  ollama slow: 118s for one job")
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── guards ────────────────────────────────────────────────────────────────────

def check_safe_to_write(force):
    """Abort rather than overwrite anything that might be real pipeline state.

    The sentinel is the only thing that makes data/ writable here, and this
    script is the only thing that creates it — so a Pi, where it will never
    exist, fails on the first guard it hits."""
    if SENTINEL.exists():
        return

    if DB_PATH.exists():
        conn = db.connect()
        try:
            if db.is_bootstrapped(conn):
                raise SystemExit(
                    f"Refusing to seed: {DB_PATH} already holds tracker data and\n"
                    f"was not created by this script. If this really is a dev copy,\n"
                    f"delete it by hand first. NEVER run this on the Pi."
                )
        finally:
            conn.close()

    existing = [p.name for p in STATE_FILES if p.exists()]
    if existing and not force:
        raise SystemExit(
            f"Refusing to overwrite existing pipeline state in {DATA_DIR}:\n"
            f"  {', '.join(existing)}\n"
            f"Pass --force if this is a dev box and those files are disposable."
        )


def reset():
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm"), *STATE_FILES):
        path.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    ap.add_argument("--reset", action="store_true",
                    help="delete the dev DB and state files, then re-seed")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing state files in data/")
    ap.add_argument("--db-only", action="store_true",
                    help="seed only tracker.db, leave data/ state files alone")
    args = ap.parse_args()

    check_safe_to_write(args.force)
    if args.reset:
        reset()

    rng  = random.Random(20260722)      # fixed: two runs, identical fixtures
    conn = db.connect()
    try:
        live, archived = seed_db(conn, rng)
    finally:
        conn.close()

    if not args.db_only:
        seed_state_files(rng)
    SENTINEL.write_text("created by scripts/seed_dev_db.py — dev scratch data\n",
                        encoding="utf-8")

    print(f"{DB_PATH}")
    print(f"  {live} live jobs, {archived} tombstoned")
    if not args.db_only:
        print(f"  state files: {', '.join(p.name for p in STATE_FILES)}")
    # ASCII only: this script's reason for existing is the Windows dev laptop,
    # whose console encoding is cp1252 and raises on anything else.
    print("\nRun the site with:  python3 web/app.py  ->  http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
