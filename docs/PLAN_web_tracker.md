# Plan: Pi-hosted web tracker, replacing the laptop Excel sync

Status: **phases 0–5 done** · bootstrap run against the real laptop workbook and
the site is live on the Pi over Tailscale (2026-07-22); the web app is now the
authoritative editor of the user-owned columns · phase 6 (delete the sync) not
started

Moves the system of record from `matched_jobs.xlsx` on the laptop to a SQLite
DB on the Pi, served over Tailscale. The Excel sync subsystem is deleted, not
refactored.

---

## Why

Every hard part of the current sync exists because the mutable state (Status,
Notes, dates) lives on a different machine than the writer, in a format that
leaves no trace when a row is deleted:

- `export_mark.txt` / `.pending` two-phase commit
- deletion inferred from *absence*, disambiguated by a watermark
- laptop-offline and workbook-locked deferral paths
- `prune_workbook.py`'s pull → prune → push → record-keys sequence
- `held_count.txt` and the backup-rollback warning
- ~45 lines of README documenting edge cases, two of them unfixable

With a DB the pipeline's insert becomes:

```sql
INSERT INTO jobs (...) VALUES (...) ON CONFLICT(key) DO NOTHING;
```

That one clause replaces the whole watermark. A tombstone row conflicts → no
resurrection. A live row conflicts → manual columns untouched. Deletion is
explicit instead of inferred, because a soft-deleted row is still *there*.

## Non-goals

- No auth system. Tailnet membership is the auth.
- No SPA / build step / Node on the Pi.
- Not changing scraping, detection, or LLM scoring. `seen_jobs.txt` stays
  exactly as-is — it gates *scoring*, is fingerprint-keyed, and is orthogonal
  to the sync problem.

---

## Schema

`data/tracker.db`, WAL mode.

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE jobs (
  key             TEXT PRIMARY KEY,   -- export_workbook.row_key(url, title, company)

  -- pipeline-owned (mirrors matches.CSV_FIELDS)
  url             TEXT,
  title           TEXT,
  company         TEXT,
  location        TEXT,
  salary          TEXT,
  source          TEXT,
  score           INTEGER,
  suitable        INTEGER,
  matched_skills  TEXT,
  concerns        TEXT,
  reason          TEXT,
  date_processed  TEXT,               -- matches.TS_FORMAT, "%Y-%m-%d %H:%M"

  -- user-owned (the COLUMNS entries with csv_field None)
  date_applied    TEXT,
  cover_letter    TEXT,
  due_date        TEXT,
  round_num       TEXT,
  status          TEXT,
  as_of           TEXT,
  notes           TEXT,
  application_id  TEXT,

  -- lifecycle
  deleted_at      TEXT,               -- NULL = live, else tombstone
  deleted_reason  TEXT,               -- user | prune | import-csv | import-pruned-keys
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX idx_jobs_live    ON jobs(deleted_at);
CREATE INDEX idx_jobs_company ON jobs(company);
CREATE INDEX idx_jobs_score   ON jobs(score);

CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
-- schema_version, bootstrapped_at
```

**Writer split** — disjoint column sets, so no merge logic is ever needed:

| Owner | Columns |
|---|---|
| pipeline | url, title, company, location, salary, source, score, suitable, matched_skills, concerns, reason, date_processed |
| web UI | date_applied, cover_letter, due_date, round_num, status, as_of, notes, application_id, deleted_at |

`key` reuses `export_workbook.row_key` verbatim — import it, don't reimplement
it. Same drift discipline as `paths.py` / `matches.py` / `remote.py`.

---

## Work phases

Ordered so a working tracker exists at every point. The Excel sync keeps
running until the final phase.

### Phase 0 — backup before anything — DONE

The laptop is currently an off-device backup. That property is about to be
given up, so replace it first.

- `scripts/backup_db.py` — online `sqlite3` backup API (not `cp`, which can
  capture a torn page set on a live WAL database) → `data/backups/tracker-YYYYMMDD.db`,
  retain 14. `--push` scp's the snapshot to the laptop via `remote.py`.
- `jobfilter-backup.service` + `.timer`, nightly at 02:00, `Persistent=true`.

Deviations from this plan as drafted:

- Snapshots go to `data/backups/`, not `data/job_data/`. The latter is the
  local mirror of the laptop sync dir and disappears in phase 6; backups must
  outlive it.
- Missing DB exits 0 with a message. Phase 0 ships before phase 2 creates any
  data, so the timer would otherwise report a failure every night until the
  bootstrap runs.

### Phase 1 — `scripts/db.py` — DONE

Schema, migration ladder keyed on `meta.schema_version`, connection helper with
`journal_mode=WAL` / `busy_timeout=5000`, `transaction()` (BEGIN IMMEDIATE),
`from_csv_row()` mapping, and the write primitives phases 2/4/5 need
(`insert_new`, `update_user_fields`, `soft_delete`, `restore`). Not wired into
the pipeline.

Deviation: **`row_key` moved from `export_workbook.py` to `matches.py`**, which
the plan had deferred to phase 2. `export_workbook` hard-exits when openpyxl is
missing, so leaving the key function there would have forced openpyxl on
`db.py`, the backup script, and the web app — breaking the stdlib-only rule
`pipeline_stats.py` already follows. `export_workbook` re-exports it, so
`prune_workbook`'s `from export_workbook import row_key` is unchanged and the
function is still defined exactly once.

Two implementation notes worth keeping:

- Migrations are lists of single statements, not `executescript()` scripts.
  `executescript` issues its own COMMIT, which silently breaks the enclosing
  transaction — a half-applied migration would be recorded as complete.
- `transaction()` uses `BEGIN IMMEDIATE`. A deferred transaction takes a read
  lock and can fail with SQLITE_BUSY when it upgrades to a write lock mid-way,
  after work is done; IMMEDIATE turns contention into a clean `busy_timeout` wait.

### Phase 2 — `scripts/bootstrap_from_workbook.py` (one-shot) — WRITTEN, NOT YET RUN

Implemented as planned. `--dry-run` reports the plan without writing;
`--workbook <path>` imports a local file instead of pulling. `build_plan()` is
pure, so the dry run reports exactly what the real run does.

Deviation: `strip_hyperlink()` was extracted from `export_workbook.existing_keys`
into a named helper both callers share, rather than the importer reimplementing
the formula parse.

**Run it against the real workbook before phase 3.** Until then the DB does not
exist and the backup timer is a no-op.



Seeds the DB from the laptop's current state, then disarms itself.

1. scp `matched_jobs.xlsx` from the laptop (`remote.py`). Abort loudly if
   unreachable — do not fall back to the Pi's stale local copy.
2. **Refuse to run if `jobs` has any rows**, `--force` or not. Record
   `meta.bootstrapped_at`. A second run after live editing would overwrite real
   work with a stale snapshot.
3. xlsx rows → live rows, manual columns preserved.
4. CSV rows whose key is **not** in the xlsx → tombstones,
   `deleted_reason='import-csv'`. These are past hand-deletions; without this
   the pipeline re-adds every one of them on the next cycle.
5. `pruned_keys.txt` → tombstones, `deleted_reason='import-prune'`.
6. Print a summary: live / tombstoned / skipped counts. Compare against
   `jobs_left.py` before trusting it.

**Import gotchas:**

- Website cells are `=HYPERLINK("url","Link")` formulas. Strip to the raw URL
  before keying — same logic as `export_workbook.existing_keys` (line ~152).
  Reuse it rather than writing a second parser.
- `Date Found` in the xlsx is `fmt_date` output (`"Jun 17"`) — year and time are
  lost. Recover full `date_processed` by joining to `matched_jobs.csv` on key;
  fall back to the parsed short date only when the CSV has no match.
- `Application ID` is auto-filled `"."` to block overflow. Treat `"."` as empty.
- Rows in the xlsx but *not* in the CSV (hand-added) are legitimate — import
  them, leave pipeline-owned fields blank.

### Phase 3 — read-only web app — DONE

Built as `web/app.py` + `web/templates/` + `web/static/style.css`, plus
`jobfilter-web.service`. Every route is GET; a test asserts the URL map exposes
no other method, so "read-only" is enforced rather than intended.

Deviations from this plan as drafted:

- **No HTMX yet.** Phase 3 needs exactly one dynamic behaviour — poll `/status`
  and update the strip — which is ~15 lines of vanilla JS. HTMX earns its place
  in phase 5, where inline edit forms actually benefit. Adding it now would mean
  vendoring a library to avoid a CDN the CSP would block anyway, for one poll.
- **`pipeline_stats.py` refactored** to extract `progress()` and `simplify_loc()`
  from `main()`. The plan said "import those functions, do not fork the logic",
  which was not possible while the arithmetic was interleaved with `print()`.
  `main()` is now a renderer over `progress()`, so the terminal report and the
  dashboard cannot diverge.
- **Job keys travel as a query parameter** (`/job?key=…`), not a path segment.
  Keys are raw URLs; `<path:key>` means fighting encoded slashes on every link.
- **The DB connection is opened read-write** even though nothing writes. A
  `mode=ro` connection cannot create or recover the WAL index and fails against
  a database the pipeline has open.
- **`db.search_jobs` takes a sort KEY**, not a SQL fragment. The earlier note
  said phase 3 "must whitelist sort keys"; doing it inside `db.py` makes the
  injection structurally impossible instead of dependent on caller discipline.

Dashboard splits its sources deliberately: progress/ETA/phase come from the
pipeline's files (`scraped_jobs.json`, `seen_jobs.txt`, `orchestrator_state.json`),
which stay file-based forever; the job table and all distributions come from the
DB, so they respect tombstones.

### Phase 3 — read-only web app (as drafted)

`web/app.py`, Flask + Jinja + HTMX. Bind `127.0.0.1:8000`.

| Route | Purpose |
|---|---|
| `GET /` | dashboard |
| `GET /jobs` | table: filter/sort by score, company, status; live vs archived |
| `GET /status` | JSON for the status strip |
| `GET /logs` | tail of `filter.log` |
| `GET /watchlist` | misses / unsupported health |
| `GET /export.xlsx` | download, reusing `export_workbook.py` |

The dashboard is `pipeline_stats.py` rendered as HTML — it already computes
score distribution, top companies, source breakdown, locations, unscored
backlog, and ETA. Import those functions; do not fork the logic.

**Status strip, not a splash screen.** The site stays up during cycles. A
persistent header shows live state:

> `Filtering — 47 / 210 scored · ~2h 10m remaining`

Sources: `orchestrator_state.json` (phase, next_run), `scraped_jobs.json` +
`seen_jobs.txt` (progress), existing ETA math. HTMX polls `/status` every 15s.

Rationale: at ~75s/job a 200-job backlog is a 4-hour filter phase, twice daily —
blocking the UI would black out a large fraction of the day, centered on the
hours the tracker is most useful. WAL gives readers a consistent snapshot with
no torn reads, so there is nothing to protect against. A splash screen also
needs its own liveness detection (did the pipeline die holding the lock?),
which the strip does not.

### Phase 4 — pipeline writes to the DB — DONE

`PHASES` is now `["detect", "verify", "scrape", "filter", "store", "sync"]`. The
`store` phase reads `matched_jobs.csv` and upserts with `ON CONFLICT(key) DO
NOTHING` in **one transaction**, so a reader sees the batch before-or-after,
never mid-write. Sync still runs; both tracks live in parallel until phase 6.

Implementation: `scripts/store_matches.py`, run by the orchestrator as a
subprocess (same pattern as scrape/filter, so a failure keeps the checkpoint and
is retried by the existing phase-retry logic). Also runnable by hand:
`python3 scripts/store_matches.py`.

Deviations / notes worth keeping:

- **`store` refuses an un-bootstrapped or missing DB** and does not create one.
  `matched_jobs.csv` is cumulative — every match ever scored — so upserting it
  into an empty DB would re-add every past hand-deletion as a *live* row, the
  exact resurrection the phase-2 `import-csv` tombstones prevent. It also leaves
  the web app's "run phase 2" 503 page intact instead of replacing it with a
  misleadingly empty tracker.
- **The first `store` after the bootstrap adds 0 rows, by construction.** The
  bootstrap already imported every CSV key (113 live + 1129 tombstones = 1242 =
  all CSV rows), so every key conflicts. New matches only appear once a later
  filter phase appends new rows to the CSV — which is exactly the steady state.
- Only `PIPELINE_FIELDS` are written (`db.from_csv_row`), and `DO NOTHING` means
  even an existing pipeline row is never rewritten — a re-scored/changed CSV row
  does not overwrite the DB. That's fine while sync is the authority for edits;
  revisit if the pipeline ever needs to *update* a live row's score.

### Phase 5 — web app becomes authoritative — DONE

The job detail page edits the user-owned columns; the DB is now the truth for
them. Routes (all guarded by a same-origin check, see below):

- `POST /job/update` — save USER_FIELDS for one job. `db.update_user_fields`
  rejects any non-user column, so the form can't reach a pipeline field.
- `POST /job/delete` — soft-delete, `deleted_reason='user'`.
- `POST /job/restore` — clear `deleted_at`.

Each handler is one `BEGIN IMMEDIATE` transaction and redirects to the detail
page (Post/Redirect/Get), so a refresh never re-submits. `updated_at` is stamped
by the db write primitives.

Deviations from this plan as drafted:

- **No HTMX; plain HTML form POSTs with Post/Redirect/Get.** Editing needs no
  per-field AJAX — one Save per job is fine for a single-user tool — so vendoring
  a ~14KB library (the CSP blocks a CDN) buys nothing over a `<form>` that works
  with zero JavaScript. Same reasoning phase 3 used to defer it; it never became
  worth it. Status/Cover Letter are `<select>`s; the rest are text/textarea.
- **Option lists moved to `db.py` as `USER_FIELD_OPTIONS`** (stdlib), and
  `export_workbook.DROPDOWNS` now re-exports them header-keyed. The plan said
  "seed from `export_workbook.DROPDOWNS`", but importing that pulls in openpyxl,
  which the web app is deliberately free of — so the canonical copy lives in the
  stdlib module the web app already imports, the same move `row_key` made. One
  definition; the workbook's data-validation and the web `<select>`s can't drift.
- **Keys travel in the form body** (`name="key"`), consistent with phase 3's
  `/job?key=…` query-param choice — keys are raw URLs, so no `<key>` path segment.
- **Added a same-origin guard** (`_reject_cross_origin`) on POST/PUT/DELETE.
  There's no login or cookie, so classic CSRF (riding an ambient session) doesn't
  apply — but without it *any* site the browser has open could POST to the
  tailnet URL, since there's no auth check at all. Browsers send `Origin` on such
  POSTs; requiring it to equal `Host` blocks them with no token scheme, while
  same-origin form posts and non-browser clients (no `Origin`) pass. This is the
  one gap tailnet membership doesn't cover.

The pipeline still never writes USER_FIELDS (disjoint from PIPELINE_FIELDS), and
a user delete becomes a tombstone that `store`'s `ON CONFLICT DO NOTHING` won't
resurrect — both verified by test. From here the laptop workbook is a stale
artifact; phase 6 removes the sync that maintains it.

### Phase 6 — delete the sync

Remove:

- the `sync` phase and its retry/deferral logic in `orchestrator.py`
  (~21 call sites reference scp/copy_pending/remote_*)
- `export_mark.txt`, `export_mark.pending`, `EXPORT_MARK_*` in `paths.py`
- `load_mark`, `bootstrap_mark`, the `ts <= mark` comparator, `held_count.txt`
  and the held-rows warning in `export_workbook.py`
- `pruned_keys.txt` and `prune_workbook.py`'s pull/push path — pruning becomes
  a bulk soft-delete in the UI, or a `--apply` that just writes tombstones
- `remote.py`, unless Phase 0's backup push keeps it alive

`export_workbook.py` survives as a pure DB → xlsx renderer behind
`/export.xlsx`. It loses the append/merge/watermark logic entirely.

Update the README: sections at lines 23–67 and 328–349 mostly disappear.

---

## Deployment

`/etc/systemd/system/jobfilter-web.service`:

```ini
[Unit]
Description=Job Filter Web Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bluke
WorkingDirectory=/home/bluke/job_filter
ExecStart=/usr/bin/gunicorn --workers 1 --threads 4 --bind 127.0.0.1:8000 web.app:app
Restart=always
RestartSec=5

# Must not compete with the filter phase (which holds CPUQuota=300%)
CPUQuota=50%

StandardOutput=append:/home/bluke/job_filter/data/web.log
StandardError=append:/home/bluke/job_filter/data/web.log

[Install]
WantedBy=multi-user.target
```

Separate unit from `jobfilter.service` on purpose: a web crash must not take
down scoring, and vice versa.

Exposure:

```bash
tailscale serve --bg --https=443 localhost:8000
```

HTTPS with a real cert, MagicDNS name, nothing listening on the LAN. No nginx,
no certbot, no firewall rules.

New dependencies: `flask`, `gunicorn`. HTMX is a single vendored JS file — no
CDN, no build step. Install for `/usr/bin/python3`, consistent with the
existing `openpyxl` note in the README.

Add to `.gitignore`: `tracker.db`, `tracker.db-wal`, `tracker.db-shm`, `web.log`.

---

## Concurrency

- WAL: readers never block on the writer; no torn reads.
- `busy_timeout=5000` on both sides.
- Pipeline writes one transaction per batch, held briefly. The filter phase runs
  for hours but should *not* hold a write txn for that duration — it writes to
  the CSV as it goes today, and the `store` phase commits once at the end.
- Web writes are single-row and instant.

---

## Open questions

1. Keep `matched_jobs.csv` after Phase 6? It's the pipeline's flat log and
   `filter_jobs.py` writes it incrementally, which is what makes the filter
   phase crash-resumable. Recommend keeping it as an append-only log and
   treating the DB as derived-plus-user-state.
2. Should `prune_workbook.py`'s fit/exclusion heuristics move into the web UI
   as a "suggest prunes" view, or stay a CLI that writes tombstones?
3. Retention for tombstones — probably never delete; they're small and they're
   what makes resurrection impossible.
