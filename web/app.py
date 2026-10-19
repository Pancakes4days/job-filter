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
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlencode

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))   # scripts/ import each other flatly

from flask import (Flask, abort, g, jsonify, make_response, redirect,
                   render_template, request, send_file, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import pipeline_stats as stats
from paths import DATA_DIR

app = Flask(__name__)

# Served under a sub-path by the reverse proxy (Caddy `handle_path /jobfilter*`
# strips the prefix so routes still match, then forwards it as X-Forwarded-Prefix).
# ProxyFix copies that header into SCRIPT_NAME so every url_for() — links, static
# assets, the status fetch — rebuilds with the /jobfilter prefix. x_prefix=0 (the
# default) makes this a no-op, so the app still runs correctly at the root, e.g.
# via the dev server or `gunicorn web.app:app` with no proxy in front.
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)


@app.context_processor
def _asset_helpers():
    """Cache-bust static links. In production (gunicorn, debug off) no no-store
    header is sent and Flask sets no max-age, so a browser's heuristic caching
    can keep serving a stale file after a deploy — edits then silently don't
    show up. A ?v=<mtime> query changes the URL whenever the file changes, so
    the browser is forced to refetch. Falls back to 0 if the file is missing.

    Stamped per file, not off one representative file: favicons in particular
    are cached hard by Chrome, and keying them to style.css's mtime would mean
    a new icon only appears if the CSS happened to change in the same deploy.
    """
    def static_url(filename):
        try:
            version = int((Path(app.static_folder) / filename).stat().st_mtime)
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    return {"static_url": static_url}


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
def require_current_schema():
    """Refuse to serve against a database older than this checkout.

    This app is the only component that writes user columns and the only one
    that never runs migrations — store_matches and `python3 scripts/db.py` do
    that. So a deploy carrying a new migration lands here first, and every save
    then builds an UPDATE naming a column the file doesn't have: a 500 on Save,
    with the detail page still rendering fine because sqlite3.Row lookups for
    the missing column come back Undefined in Jinja. That combination is
    genuinely baffling from the outside, so name it instead of raising.

    Blocks reads too, not just writes. A page that silently omits a column the
    template expects is a worse failure than an honest 503, and the fix is one
    command either way.
    """
    if request.endpoint in (None, "static") or not db.DB_PATH.exists():
        return None
    version = db.schema_version(get_db())
    if version < db.SCHEMA_VERSION:
        return render_template("stale_schema.html", db_path=db.DB_PATH,
                               found=version, expected=db.SCHEMA_VERSION), 503


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
        # Outreach you owe a reply to. On the dashboard because a follow-up
        # queue only works if it is in front of you without being navigated to.
        due         = db.follow_ups_due(conn, today_iso(), limit=8),
        recent      = db.search_jobs(conn, sort="date", limit=10),
    )


# ── returning to the list after a save ────────────────────────────────────────
# Saving redirects to the Jobs page rather than back to the job, because the
# job is finished with at that point and the next one is on the list. The
# filters that produced the list travel with it — detail link, then hidden
# inputs, then the redirect — so a save doesn't dump you back at an unfiltered
# page and make you re-pick company/status/sort for every application.
#
# The prefix is load-bearing, not decoration: the list filters on `status` and
# the job's own Status <select> is also named `status`, so an unprefixed hidden
# input would come first in request.form and job_update would silently write
# the filter's value into the job.
#
# The hidden inputs only exist when the job page was opened from the list. A
# job reached any other way — the dashboard, a contact, a reload after Restore —
# has none, and the save used to land on a bare /jobs. So the list also drops
# its filters into a cookie each time it renders, and a save or archive that
# arrives with nothing to go on returns to whatever the list last showed.

JOBS_ARGS   = ("q", "company", "status", "min_score", "sort", "archived")
BACK_PREFIX = "back_"
JOBS_COOKIE = "jobs_filters"


def carried_filters(source, *, prefixed):
    """The Jobs-page filters present in `source`, keyed with the prefix.

    `prefixed=False` reads them under their bare names (the Jobs page's own
    query string); True reads them already prefixed (a detail page or a form
    post that is passing them along).
    """
    out = {}
    for name in JOBS_ARGS:
        value = source.get(name if not prefixed else BACK_PREFIX + name)
        if value:
            out[BACK_PREFIX + name] = value
    return out


def jobs_url_from(form):
    """The Jobs URL a save or archive should return to: the filters the form
    carried, else the ones the list last rendered with (the cookie). Rebuilt
    through url_for from a whitelist rather than echoing back a caller-supplied
    URL, so this round trip can't be bent into an off-site redirect."""
    source = form
    if not any(form.get(BACK_PREFIX + name) for name in JOBS_ARGS):
        source = parse_qs(request.cookies.get(JOBS_COOKIE, ""))
        source = {BACK_PREFIX + k: v[0] for k, v in source.items()}
    params = {name: source.get(BACK_PREFIX + name) for name in JOBS_ARGS
              if source.get(BACK_PREFIX + name)}
    return url_for("jobs", **params)


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
        sort      = request.args.get("sort", db.JOBS_DEFAULT_SORT),
    )
    page = render_template(
        "jobs.html",
        rows       = rows,
        archived   = archived,
        companies  = db.distinct_values(conn, "company"),
        statuses   = db.distinct_values(conn, "status"),
        sort       = request.args.get("sort", db.JOBS_DEFAULT_SORT),
        sort_keys  = list(db.SORT_ORDERS),
        q          = request.args.get("q", ""),
        min_score  = min_score,
        sel_company= request.args.get("company", ""),
        sel_status = request.args.get("status", ""),
        counts     = db.counts(conn),
        status_bar = pipeline_status(),
        # Spread into each row's detail link so Save can find its way back here.
        back       = carried_filters(request.args, prefixed=False),
    )
    resp = make_response(page)
    # Remembered for jobs_url_from — see "returning to the list" above.
    resp.set_cookie(JOBS_COOKIE,
                    urlencode({n: request.args[n] for n in JOBS_ARGS
                               if request.args.get(n)}),
                    samesite="Lax", httponly=True)
    return resp


@app.route("/job")
def job_detail():
    """Key comes in as a query parameter, not a path segment: keys are raw URLs
    ("https://acme.io/1"), and embedding one in a path means wrestling with
    encoded slashes on every link."""
    key  = request.args.get("key", "")
    conn = get_db()
    row  = db.get_job(conn, key) if key else None
    if row is None:
        return render_template("not_found.html", key=key,
                               status_bar=pipeline_status()), 404
    return render_template("job.html", job=row, status_bar=pipeline_status(),
                           user_fields=db.USER_FIELDS,
                           options=db.USER_FIELD_OPTIONS,
                           # Who you know here — the reason to open the page
                           # before applying rather than after.
                           contacts=db.contacts_for_company(conn, row["company"]),
                           back=carried_filters(request.args, prefixed=True))


# ── writes (phase 5) ──────────────────────────────────────────────────────────
# The user owns these columns; the pipeline never touches them, so an edit here
# and a `store` upsert can't collide. Each handler is one BEGIN IMMEDIATE
# transaction and redirects (Post/Redirect/Get) — Save and Archive to the
# list, Restore to the job — so a refresh never re-submits.

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
    return redirect(jobs_url_from(request.form))


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
    # Back to the list, like Save: archiving is also "done with this one".
    return redirect(jobs_url_from(request.form))


@app.route("/job/restore", methods=["POST"])
def job_restore():
    key = request.form.get("key", "")
    conn = get_db()
    _require_job(conn, key)
    with db.transaction(conn):
        db.restore(conn, key)
    return redirect(url_for("job_detail", key=key))


# ── network (outreach contacts) ───────────────────────────────────────────────
# The people side of the same database. A job row tells you a company is hiring;
# a contact row tells you whether you know anyone there. Keeping both in
# tracker.db is what lets outreach_gaps() ask the one question a spreadsheet
# can't: which companies are hiring me right now that I have nobody at.
#
# Contacts are typed by hand, on purpose. The pipeline can read a public ATS
# API; it has no business scraping people, and the useful volume here is a
# dozen good messages a week, not a thousand rows.

CONTACT_ARGS = ("q", "company", "status", "school", "sort", "archived")


def today_iso():
    """Local date as 'YYYY-MM-DD', matching how the date inputs submit.

    Local, not UTC: "is this follow-up due" is a question about the user's day,
    and on an evening in America/New_York a UTC date is already tomorrow.
    """
    from datetime import date
    return date.today().isoformat()


def _contact_form(form):
    """The contact columns present in a submitted form. Anything not in
    CONTACT_FIELDS is ignored here and rejected by db._clean_contact, so a
    renamed input surfaces instead of silently writing nothing."""
    return {f: (form.get(f) or "").strip()
            for f in db.CONTACT_FIELDS if f in form}


def _contact_error(exc):
    """Form-level message for a rejected contact save.

    A ValueError is db's own validation and already reads as a sentence. An
    IntegrityError here can only be the job_key foreign key, and sqlite's
    wording for it ("FOREIGN KEY constraint failed") tells the user nothing
    about which field to change.
    """
    if isinstance(exc, sqlite3.IntegrityError):
        return "That job link no longer exists — pick another under “Re: job”."
    return str(exc)


def _require_contact(conn, contact_id):
    row = db.get_contact(conn, contact_id) if contact_id else None
    if row is None:
        abort(404)
    return row


def _contact_form_context(conn, **extra):
    """Everything contact.html needs besides the row itself."""
    ctx = {
        "status_bar": pipeline_status(),
        "options":    db.CONTACT_FIELD_OPTIONS,
        "schools":    db.distinct_contact_values(conn, "school"),
        "companies":  db.distinct_values(conn, "company"),
        "date_fields": db.CONTACT_DATE_FIELDS,
    }
    ctx.update(extra)
    return ctx


@app.route("/network")
def network():
    conn = get_db()
    archived = request.args.get("archived") == "1"

    rows = db.search_contacts(
        conn,
        q        = request.args.get("q") or None,
        company  = request.args.get("company") or None,
        status   = request.args.get("status") or None,
        school   = request.args.get("school") or None,
        archived = archived,
        # A CONTACT_SORT_ORDERS key, validated in db.search_contacts — never SQL.
        sort     = request.args.get("sort", db.CONTACT_DEFAULT_SORT),
    )
    return render_template(
        "network.html",
        rows        = rows,
        archived    = archived,
        stats       = db.contact_stats(conn),
        due         = db.follow_ups_due(conn, today_iso()),
        # Gapped at the same threshold the pipeline scores against, so this list
        # means "companies worth applying to" and not "every company on file".
        gaps        = db.outreach_gaps(conn, min_score=stats.load_threshold()),
        threshold   = stats.load_threshold(),
        today       = today_iso(),
        companies   = db.distinct_contact_values(conn, "company"),
        statuses    = db.distinct_contact_values(conn, "status"),
        schools     = db.distinct_contact_values(conn, "school"),
        sort        = request.args.get("sort", db.CONTACT_DEFAULT_SORT),
        sort_keys   = list(db.CONTACT_SORT_ORDERS),
        q           = request.args.get("q", ""),
        sel_company = request.args.get("company", ""),
        sel_status  = request.args.get("status", ""),
        sel_school  = request.args.get("school", ""),
        status_bar  = pipeline_status(),
    )


@app.route("/network/contact")
def contact_detail():
    conn = get_db()
    row  = _require_contact(conn, request.args.get("id", type=int))
    return render_template(
        "contact.html",
        **_contact_form_context(
            conn,
            contact = row,
            # Live reqs at this company, to link the outreach to a specific one.
            # Exact-match company (search_jobs) where contacts_for_company is
            # NOCASE, so a case-mismatched contact gets an empty picker rather
            # than wrong options — the already-linked job still renders below.
            jobs    = db.search_jobs(conn, company=row["company"], sort="score"),
            linked  = db.get_job(conn, row["job_key"]) if row["job_key"] else None,
        ),
    )


@app.route("/network/new")
def contact_new():
    """Blank add form. company/job_key may be prefilled by the link on a job
    page, which is the path that makes adding a contact a two-field job."""
    conn = get_db()
    key  = request.args.get("job_key", "")
    job  = db.get_job(conn, key) if key else None
    return render_template(
        "contact.html",
        **_contact_form_context(
            conn,
            contact = {
                "company":  request.args.get("company", "") or (job["company"] if job else ""),
                "school":   db.CONTACT_DEFAULT_SCHOOL,
                "status":   db.CONTACT_FIELD_OPTIONS["status"][0],
                "job_key":  key if job else "",
            },
            jobs   = db.search_jobs(conn, company=(job["company"] if job else None),
                                    sort="score") if job else [],
            linked = job,
        ),
    )


@app.route("/network/create", methods=["POST"])
def contact_create():
    """Create one contact. On a validation error the form is re-rendered with
    what was typed still in it — there is no session or flash in this app (no
    secret key, auth is the tailnet), so the error has to travel in the render
    rather than through a redirect."""
    conn   = get_db()
    fields = _contact_form(request.form)
    try:
        with db.transaction(conn):
            contact_id = db.create_contact(conn, fields)
    except (ValueError, sqlite3.IntegrityError) as exc:
        return render_template(
            "contact.html",
            **_contact_form_context(conn, contact=fields, error=_contact_error(exc),
                                    jobs=[], linked=None),
        ), 400
    return redirect(url_for("contact_detail", id=contact_id))


@app.route("/network/contact/update", methods=["POST"])
def contact_update():
    conn = get_db()
    contact_id = request.form.get("id", type=int)
    row = _require_contact(conn, contact_id)
    fields = _contact_form(request.form)
    try:
        with db.transaction(conn):
            db.update_contact(conn, contact_id, fields)
    except (ValueError, sqlite3.IntegrityError) as exc:
        merged = {**dict(row), **fields}
        return render_template(
            "contact.html",
            **_contact_form_context(
                conn, contact=merged, error=_contact_error(exc),
                jobs=db.search_jobs(conn, company=row["company"], sort="score"),
                linked=db.get_job(conn, row["job_key"]) if row["job_key"] else None),
        ), 400
    return redirect(url_for("contact_detail", id=contact_id))


@app.route("/network/contact/delete", methods=["POST"])
def contact_delete():
    """Soft-delete, for recoverability rather than for the reason jobs have it:
    nothing would re-add a contact, but a hand-typed row is gone for good if a
    misclick hard-deletes it."""
    conn = get_db()
    contact_id = request.form.get("id", type=int)
    _require_contact(conn, contact_id)
    with db.transaction(conn):
        db.soft_delete_contact(conn, contact_id, reason="user")
    return redirect(url_for("contact_detail", id=contact_id))


@app.route("/network/contact/restore", methods=["POST"])
def contact_restore():
    conn = get_db()
    contact_id = request.form.get("id", type=int)
    _require_contact(conn, contact_id)
    with db.transaction(conn):
        db.restore_contact(conn, contact_id)
    return redirect(url_for("contact_detail", id=contact_id))


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


@app.template_filter("isodate")
def isodate(value):
    """'2026-09-14' -> 'Sep 14'. The contact date columns are ISO (they are
    sorted and compared as strings), so they need their own filter — shortdate
    parses matches.TS_FORMAT and would fall through to its slice for these."""
    from datetime import datetime
    from matches import month_day
    try:
        return month_day(datetime.strptime(str(value), "%Y-%m-%d"))
    except (TypeError, ValueError):
        return str(value or "")


@app.template_filter("shortdate")
def shortdate(value):
    """'2026-07-21 09:30' (UTC) -> 'Jul 21' (local). Tolerates anything
    unparseable."""
    from matches import found_day
    try:
        return found_day(value)
    except (TypeError, ValueError):
        return str(value or "")[:10]


if __name__ == "__main__":
    # Dev only — systemd runs gunicorn. Loopback-bound to match production.
    app.run(host="127.0.0.1", port=8000, debug=True)
