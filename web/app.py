#!/usr/bin/env python3
"""
Read-only web tracker for the job filter pipeline. Phase 3 of
docs/PLAN_web_tracker.md.

    python3 web/app.py                 # dev server on http://127.0.0.1:8000
    gunicorn --bind 127.0.0.1:8000 web.app:app     # how systemd runs it

Serves the job tracker and pipeline dashboard over the tailnet. READ-ONLY on
purpose: phase 3 runs alongside the still-live Excel sync, so nothing here may
write. Editing arrives in phase 5, once the workbook is no longer authoritative.

Exposure is via `tailscale serve` (see jobfilter-web.service), which is why
this binds to loopback and has no auth: tailnet membership IS the authentication,
and nothing should be reachable from the LAN.

WHY THERE IS NO "PIPELINE RUNNING" SPLASH SCREEN
A filter phase is hours long (~75s/job) and runs twice a day, so blocking the
UI during one would black out the site for much of the day — including the
hours it is most useful. SQLite in WAL mode gives readers a consistent snapshot
that never blocks on the pipeline's writer, so there is nothing to protect
against. The status strip reports what is happening instead. It also avoids a
failure mode a splash screen would introduce: needing to tell "pipeline running"
apart from "pipeline died holding the lock".
"""

import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))   # scripts/ import each other flatly

from flask import Flask, g, jsonify, render_template, request, send_file

import db
import pipeline_stats as stats
from paths import DATA_DIR

app = Flask(__name__)

LOG_PATH         = DATA_DIR / "filter.log"
MISSES_PATH      = DATA_DIR / "watchlist_misses.txt"
UNSUPPORTED_PATH = DATA_DIR / "watchlist_unsupported.txt"
XLSX_PATH        = DATA_DIR / "matched_jobs.xlsx"

LOG_TAIL_BYTES = 200_000        # read only the tail; filter.log rotates at 10M


# ── database ──────────────────────────────────────────────────────────────────
# One connection per request, closed on teardown: sqlite3 connections are not
# safe to share across gunicorn's worker threads.
#
# Opened read-write despite this being a read-only app. A `mode=ro` connection
# cannot recover or create the WAL index, so it fails outright against a
# database the pipeline has open. Read-only-ness is enforced by there being no
# write path in this module, not by the connection flags.

def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.before_request
def require_db():
    """A missing DB means phase 2 hasn't run. Say so plainly instead of
    surfacing a sqlite 'no such table' traceback."""
    if not db.DB_PATH.exists() and request.endpoint not in (None, "static"):
        return render_template("no_db.html", db_path=db.DB_PATH), 503


# ── pipeline status ───────────────────────────────────────────────────────────

def pipeline_status():
    """What the status strip shows. Never raises: the dashboard must render
    even mid-scrape, when scraped_jobs.json is being rewritten under us."""
    state = stats.load_state()
    out = {
        "phase":    state.get("phase", "unknown"),
        "next_run": state.get("next_run", "unknown"),
        "running":  state.get("phase", "idle") not in ("idle", "unknown"),
    }
    try:
        p = stats.progress()
        out.update({
            "total":     p["total"],
            "scored":    p["scored"],
            "remaining": p["remaining"],
            "matches":   p["scrape_matches"],
            "label":     p["scrape_label"],
            "eta":       humanize_eta(p["eta_seconds"]),
            "pct":       round(100 * p["scored"] / p["total"]) if p["total"] else 0,
        })
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        out["error"] = "progress unavailable (scrape in flight?)"
    return out


def humanize_eta(seconds):
    if seconds <= 0:
        return ""
    mins = seconds // 60
    if mins < 60:
        return f"~{mins}m"
    return f"~{mins // 60}h {mins % 60}m"


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    conn = get_db()
    live = db.live_jobs(conn)

    locations = Counter(stats.simplify_loc(r["location"]) for r in live)
    scores    = db.score_distribution(conn)

    return render_template(
        "dashboard.html",
        status_bar  = pipeline_status(),
        counts      = db.counts(conn),
        threshold   = stats.load_threshold(),
        scores      = scores,
        score_max   = max((n for _, n in scores), default=0),
        companies   = db.top_companies(conn, limit=15),
        locations   = locations.most_common(10),
        statuses    = db.status_breakdown(conn),
        alerts      = stats.load_active_alerts(),
        recent      = db.search_jobs(conn, sort="date", limit=10),
    )


@app.route("/jobs")
def jobs():
    conn = get_db()
    archived = request.args.get("archived") == "1"

    min_score = request.args.get("min_score", type=int)
    rows = db.search_jobs(
        conn,
        q         = request.args.get("q") or None,
        company   = request.args.get("company") or None,
        status    = request.args.get("status") or None,
        min_score = min_score,
        archived  = archived,
        # sort is a SORT_ORDERS key, validated in db.search_jobs — never SQL.
        sort      = request.args.get("sort", db.DEFAULT_SORT),
    )
    return render_template(
        "jobs.html",
        rows       = rows,
        archived   = archived,
        companies  = db.distinct_values(conn, "company"),
        statuses   = db.distinct_values(conn, "status"),
        sort       = request.args.get("sort", db.DEFAULT_SORT),
        sort_keys  = list(db.SORT_ORDERS),
        q          = request.args.get("q", ""),
        min_score  = min_score,
        sel_company= request.args.get("company", ""),
        sel_status = request.args.get("status", ""),
        counts     = db.counts(conn),
        status_bar = pipeline_status(),
    )


@app.route("/job")
def job_detail():
    """Key comes in as a query parameter, not a path segment: keys are raw URLs
    ("https://acme.io/1"), and embedding one in a path means wrestling with
    encoded slashes on every link."""
    key = request.args.get("key", "")
    row = db.get_job(get_db(), key) if key else None
    if row is None:
        return render_template("not_found.html", key=key,
                               status_bar=pipeline_status()), 404
    return render_template("job.html", job=row, status_bar=pipeline_status())


@app.route("/status")
def status():
    """Polled by the status strip. JSON so the page updates without a reload."""
    return jsonify(pipeline_status())


@app.route("/logs")
def logs():
    text = ""
    if LOG_PATH.exists():
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            # Tail starts mid-line after the seek; drop the partial first line.
            text = f.read().decode("utf-8", errors="replace").split("\n", 1)[-1]
    return render_template("logs.html", log=text, path=LOG_PATH,
                           status_bar=pipeline_status())


@app.route("/watchlist")
def watchlist():
    def read(path):
        if not path.exists():
            return []
        return [ln.rstrip() for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    return render_template("watchlist.html",
                           misses      = read(MISSES_PATH),
                           unsupported = read(UNSUPPORTED_PATH),
                           status_bar  = pipeline_status())


@app.route("/export.xlsx")
def export_xlsx():
    """The workbook the pipeline still produces. Becomes a DB-rendered download
    in phase 6, once export_workbook stops being a sync target."""
    if not XLSX_PATH.exists():
        return render_template("not_found.html", key=XLSX_PATH.name,
                               status_bar=pipeline_status()), 404
    return send_file(XLSX_PATH, as_attachment=True, download_name=XLSX_PATH.name)


@app.template_filter("shortdate")
def shortdate(value):
    """'2026-07-21 09:30' -> 'Jul 21'. Tolerates anything unparseable."""
    from datetime import datetime
    from matches import TS_FORMAT, month_day
    try:
        return month_day(datetime.strptime(str(value), TS_FORMAT))
    except (TypeError, ValueError):
        return str(value or "")[:10]


if __name__ == "__main__":
    # Dev only — systemd runs gunicorn. Loopback-bound to match production.
    app.run(host="127.0.0.1", port=8000, debug=True)
