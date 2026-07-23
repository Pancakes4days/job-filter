#!/usr/bin/env python3
"""
Web tracker for the job filter pipeline. Phases 3 + 5 of
docs/PLAN_web_tracker.md.

    python3 web/app.py                 # dev server on http://127.0.0.1:8000
    gunicorn --bind 127.0.0.1:8000 web.app:app     # how systemd runs it

Serves the job tracker and pipeline dashboard over the tailnet. As of phase 5
the user-owned columns (Status, Notes, dates, ...) are editable here and this is
their system of record — the pipeline never writes them (disjoint from
PIPELINE_FIELDS), so no merge logic is needed. Writes are plain HTML form POSTs
with Post/Redirect/Get; no JavaScript is required to edit.

Exposure is via `tailscale serve` (see jobfilter-web.service), which is why
this binds to loopback and has no auth: tailnet membership IS the authentication,
and nothing should be reachable from the LAN. Writes add a same-origin check
(see _reject_cross_origin) so a page on some other site the browser has open
can't POST into the tracker — the one thing tailnet membership doesn't cover.

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

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, url_for)

import db
import pipeline_stats as stats
from paths import DATA_DIR

app = Flask(__name__)


@app.after_request
def _no_cache_in_dev(resp):
    """In debug mode only, tell the browser never to cache. Otherwise edits to
    templates/CSS keep showing stale pages behind a hard refresh. Production
    (gunicorn, debug off) is untouched, so static caching there is unaffected."""
    if app.debug:
        resp.headers["Cache-Control"] = "no-store"
    return resp


LOG_PATH         = DATA_DIR / "filter.log"
MISSES_PATH      = DATA_DIR / "watchlist_misses.txt"
UNSUPPORTED_PATH = DATA_DIR / "watchlist_unsupported.txt"

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


@app.before_request
def _reject_cross_origin():
    """Block cross-site writes. There is no login and no cookie — auth is the
    tailnet — so classic CSRF (riding an ambient session) doesn't apply, but a
    page on any other origin the browser has open could still POST to the
    tailnet URL. Browsers attach an Origin header to such POSTs, so requiring it
    to match our Host stops that without a token scheme. Same-origin form posts
    (Origin matches) and non-browser clients like curl (no Origin) pass."""
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin")
        if origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.host:
                abort(403)


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
    return render_template("job.html", job=row, status_bar=pipeline_status(),
                           user_fields=db.USER_FIELDS,
                           options=db.USER_FIELD_OPTIONS)


# ── writes (phase 5) ──────────────────────────────────────────────────────────
# The user owns these columns; the pipeline never touches them, so an edit here
# and a `store` upsert can't collide. Each handler is one BEGIN IMMEDIATE
# transaction and redirects back to the detail page (Post/Redirect/Get), so a
# refresh never re-submits.

def _require_job(conn, key):
    if not key or db.get_job(conn, key) is None:
        abort(404)


@app.route("/job/update", methods=["POST"])
def job_update():
    """Save the hand-edited user columns for one job. Only USER_FIELDS are read
    from the form; db.update_user_fields rejects anything else, so a stray field
    name surfaces as an error instead of silently writing a pipeline column."""
    key = request.form.get("key", "")
    conn = get_db()
    _require_job(conn, key)
    fields = {f: (request.form.get(f) or "").strip()
              for f in db.USER_FIELDS if f in request.form}
    with db.transaction(conn):
        db.update_user_fields(conn, key, fields)
    return redirect(url_for("job_detail", key=key))


@app.route("/job/delete", methods=["POST"])
def job_delete():
    """Soft-delete (tombstone) a job. The row stays, so the pipeline's
    ON CONFLICT(key) DO NOTHING never re-adds it — this is why no watermark is
    needed. reason='user' distinguishes it from the bootstrap's import-* rows."""
    key = request.form.get("key", "")
    conn = get_db()
    _require_job(conn, key)
    with db.transaction(conn):
        db.soft_delete(conn, key, reason="user")
    return redirect(url_for("job_detail", key=key))


@app.route("/job/restore", methods=["POST"])
def job_restore():
    key = request.form.get("key", "")
    conn = get_db()
    _require_job(conn, key)
    with db.transaction(conn):
        db.restore(conn, key)
    return redirect(url_for("job_detail", key=key))


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
    """Render the workbook from the DB's live jobs on demand. export_workbook is
    imported lazily so the app still boots on a box without openpyxl — only this
    one route needs it."""
    import io

    import export_workbook as ew

    rows = db.live_jobs(get_db())
    wb   = ew.render_workbook(rows)
    buf  = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="matched_jobs.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


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
