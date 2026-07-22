# Job Filter Pipeline — Raspberry Pi 5 + gemma3:4b

Scrapes job listings from multiple sources, scores them against your
skills/preferences with a local LLM, and tracks the matches in a SQLite database
you browse and edit from a small web app served over Tailscale. Runs unattended
as a systemd service so you just open the site to a fresh list. No cloud and no
API keys — the only dependencies are Ollama (the LLM), Flask + Gunicorn (the web
app), and openpyxl (the on-demand `.xlsx` export); everything else is Python's
standard library.

## Tech stack

Everything runs on the Pi; your other devices are just browsers pointed at it.

| Layer | What | Notes |
|---|---|---|
| **Data** | SQLite (`data/tracker.db`, WAL mode) | The single system of record. File-based, no DB server. `db.py` is the only code that touches it — hand-written SQL, no ORM. |
| **Scoring** | Ollama + `gemma3:4b` | Local LLM that scores each job. The heavy compute, and the reason it wants a Pi 5. |
| **Scraping** | Python 3 + `urllib` | Hits ATS APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Workday, Oracle) directly. No scraping framework. |
| **Web app** | Flask + Jinja2, served by Gunicorn | Server-rendered HTML. Plain CSS + a few lines of vanilla JS — no React, no npm, no build step. |
| **Access** | Tailscale (`tailscale serve`) | Puts the site on your private mesh over HTTPS. Being on the tailnet *is* the auth — no login. |
| **Process mgmt** | systemd | `jobfilter` (pipeline), `jobfilter-web` (site), `jobfilter-backup.timer` (nightly DB snapshot). |
| **Export** | openpyxl | Renders an `.xlsx` from the DB **on demand** at `/export.xlsx`. Not a data store — Excel is just an optional report format now. |

Outside Python's standard library the whole dependency footprint is Ollama,
Flask, Gunicorn, and openpyxl. Everything else is stdlib — deliberately, so it
runs on a Pi with no cloud and no API keys.

## How it runs

`orchestrator.py` is a long-running daemon (managed by systemd). Twice a day it
fires a 5-phase pipeline:

```
1. detect   — auto-detect ATS platforms for any new companies.txt entries
2. verify   — probe each watchlist company's job board for live openings
3. scrape   — scraper.py: public sources + verified watchlist, single pass
4. filter   — filter_jobs.py scores each job via the local Ollama LLM
5. store    — store_matches.py upserts new matches into the tracker DB
```

The **tracker database** (`data/tracker.db`, SQLite/WAL) is the single system of
record. The `store` phase inserts new matches with `ON CONFLICT(key) DO NOTHING`,
so the columns you fill in (Status, Notes, dates) are never overwritten and a
row you delete stays deleted — a deleted row is kept as a *tombstone* rather
than removed, so the pipeline can't resurrect it. You read and edit everything
through the web app (`web/app.py`), served on the tailnet via `tailscale serve`;
`export_workbook.py` renders a styled `.xlsx` from the DB on demand behind
`/export.xlsx`. (Earlier versions synced an Excel workbook to a laptop over
Tailscale using a watermark; that sync was removed once the DB became
authoritative — see `docs/PLAN_web_tracker.md`.)

Default schedule is **6 AM and 1 PM** local time. On first launch it runs one
cycle immediately, then settles into the schedule. Missed slots are caught up:
if the Pi was off (or a long cycle overran) when a slot fired, the next start
or idle-loop tick runs the pipeline immediately instead of waiting a day.

### Built for unattended operation

- **Survives restarts.** The current phase is checkpointed to
  `orchestrator_state.json` before each step. If the process is killed (watchdog,
  OOM, crash) systemd restarts it and it resumes from the last checkpoint — the
  `seen_jobs.txt` list means the LLM only scores jobs it hadn't reached yet.
- **No overlap.** A `flock` on `orchestrator.lock` guarantees only one instance
  runs; phases run sequentially; a long run never stacks a second cycle on top.
- **Only stops when you say so.** It exits cleanly on `systemctl stop` (no
  restart). Any other exit is treated as a failure and restarted.
- **Failed phases retry in place.** If a phase subprocess fails (e.g. Ollama
  down during filter), the orchestrator keeps the checkpoint and retries that
  phase every 15 minutes (`phase_retry_interval`) — no crash-restart loop,
  and completed phases are never redone.
- **Your edits are never clobbered.** The pipeline only writes pipeline-owned
  columns; Status, Notes, dates and the rest belong to you and the web app, a
  disjoint set, so `store` and your edits can't collide.
- **Deletions are respected.** A row you archive in the web app becomes a
  tombstone that stays in the DB, so the pipeline's `ON CONFLICT(key) DO
  NOTHING` never re-adds it. Restore it any time from the job's page.
- **The DB is backed up nightly.** `backup_db.py` (a systemd timer) takes a
  consistent online snapshot to `data/backups/` and can optionally `--push` one
  to another machine over Tailscale — the off-device copy the laptop used to
  provide.

## Layout

```
job_filter/
├── scripts/   the .py files + paths.py (shared directory layout)
├── web/       Flask web tracker (app.py, templates/, static/)
├── config/    config.json, scraper_config.json, companies.txt  (you edit these)
├── data/      runtime state + outputs (auto-created, mostly git-ignored)
├── docs/      handoff / working notes / PLAN_web_tracker.md
├── jobfilter.service          jobfilter-web.service
├── jobfilter-backup.service   jobfilter-backup.timer
└── jobfilter.logrotate
```

**Scripts** (`scripts/`)
- `orchestrator.py` — the daemon that drives everything (run this via systemd)
- `scraper.py` — pulls listings from job sources into `data/scraped_jobs.json`
- `filter_jobs.py` — scores jobs with the LLM, writes `data/matched_jobs.csv`
- `store_matches.py` — upserts the CSV's matches into `data/tracker.db`
- `db.py` — the SQLite tracker layer (schema, migrations, queries); stdlib only
- `backup_db.py` — nightly online snapshot of the DB to `data/backups/`
- `export_workbook.py` — renders a styled `.xlsx` from the DB (needs `openpyxl`)
- `prune_workbook.py` — MANUAL: soft-deletes all but the best 1–2 roles per company
- `detect_platforms.py` — one-time/manual full ATS detection from a company list
- `verify_watchlist.py` — manual helper to spot-check detected watchlist entries
- `paths.py` — defines `CONFIG_DIR` / `DATA_DIR` / `DB_PATH`; the one place paths are set

**Web app** (`web/`)
- `app.py` — read + edit the tracker over the tailnet; run under gunicorn by
  `jobfilter-web.service`, exposed with `tailscale serve`. See
  `docs/PLAN_web_tracker.md` for the design.

**You edit these** (`config/`)
- `config.json` — your skills, preferences, dealbreakers, LLM settings
- `scraper_config.json` — job sources, keyword/location filters, watchlist
- `companies.txt` — company names you want watched (one per line; `#` comments ok)
- `local.json` — schedule and other deployment settings (see **Configuration**)

**Created automatically** (`data/`)
- `scraped_jobs.json` — latest scrape output
- `matched_jobs.csv` — flat results log (the machine record + dedup source);
  `filter_jobs.py` appends to it incrementally, which is what makes the filter
  phase crash-resumable. `store` reads it into the DB
- `tracker.db` — **the tracker** (SQLite/WAL): pipeline-scored jobs plus your
  hand-edited columns, with tombstones for deletions. The web app reads and
  writes this; `.db-wal` / `.db-shm` are its WAL sidecar files
- `backups/` — nightly `tracker-YYYYMMDD.db` snapshots (kept 14 by default)
- `matched_jobs.xlsx` — only when you run/download the export; a styled snapshot
  of the DB's live jobs, not a source of truth
- `seen_jobs.txt` — fingerprints of already-scored jobs (dedup across runs)
- `orchestrator_state.json` — pipeline checkpoint for crash recovery
- `orchestrator.lock` — single-instance guard
- `watchlist_misses.txt` — companies whose ATS couldn't be auto-detected
- `filter.log` — combined service log

## One-time Pi setup

```bash
# 1. Install Ollama and pull the model (~1.5 GB)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:4b
ollama run gemma3:4b "Say hello in five words."   # sanity check

# 2. Dependencies. openpyxl (the .xlsx export) + the web app (Flask/gunicorn).
sudo apt install python3-openpyxl python3-flask gunicorn

# 3. Set the Pi's timezone so the schedule means local time (DST-safe), and
#    make boots WAIT for a synced clock. The Pi has no RTC battery: without
#    this, a post-outage boot can score jobs with a stale clock, giving them
#    wrong Date Found timestamps.
sudo timedatectl set-timezone America/New_York
sudo systemctl enable systemd-time-wait-sync.service

# 4. (Optional) Passwordless SSH to another machine, only if you want
#    `backup_db.py --push` to copy nightly snapshots off the Pi.
ssh-copy-id youruser@<other-tailscale-ip>
```

> The systemd units run with `/usr/bin/python3` and `/usr/bin/gunicorn`, so
> install these for the system interpreter (apt packages, or a venv the units
> point at — just keep them consistent). See `docs/PLAN_web_tracker.md` for
> installing `jobfilter-web.service`, `jobfilter-backup.timer`, and exposing the
> site with `tailscale serve`.

## Configuration

Edit your profile and sources:

```bash
nano config/config.json            # skills, preferences, dealbreakers, threshold
nano config/scraper_config.json    # which sources to use, keyword/location filters
nano config/companies.txt          # company names to watch
```

Then create `config/local.json` (gitignored) from the template:

```bash
cp config/local.example.json config/local.json
nano config/local.json
```

```json
{
  "scrape_hours_local":  [6, 13],
  "detect_delay":        0.5,
  "phase_retry_interval": 900,

  "remote_host": "100.x.y.z",
  "remote_user": "youruser",
  "remote_dir":  "/home/youruser/jobfilter-backups"
}
```

`scrape_hours_local` must list at least one hour (the orchestrator refuses to
start with an empty schedule). `phase_retry_interval` (seconds) paces retries
of a failed pipeline phase.

The `remote_*` keys are **optional** and only used by `backup_db.py --push` to
copy nightly DB snapshots off the Pi — the pipeline no longer syncs anything to
a laptop. `remote_host` can be a Tailscale IP (`tailscale ip -4`) or MagicDNS
name; `remote_dir` an existing folder to receive snapshots. Omit them if you
don't push backups.

## Install as a service

Three systemd units, all with `User=` / `WorkingDirectory=` you adjust to match
your setup: the **pipeline**, the **web app**, and the **nightly DB backup**.

```bash
# 1. Pipeline (scrape → score → store)
sudo cp jobfilter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobfilter

# 2. Web app (Flask under gunicorn, loopback-bound)
sudo cp jobfilter-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobfilter-web

# 3. Nightly DB backup (02:00, keeps 14 snapshots in data/backups/)
sudo cp jobfilter-backup.service jobfilter-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobfilter-backup.timer

# Log rotation (filter.log grows forever otherwise; adjust the path inside first)
sudo cp jobfilter.logrotate /etc/logrotate.d/jobfilter

# Watch it work
tail -f data/filter.log
sudo systemctl stop jobfilter        # clean shutdown, no restart
```

`jobfilter.service` pins the pipeline to 3 CPU cores (`CPUQuota=300%`) so the
Pi's watchdog is less likely to kill it; `jobfilter-web.service` is capped at
`CPUQuota=50%` so the site never competes with a running filter phase. Both
restart on failure but not on a clean stop.

### Exposing the site over Tailscale

The web app binds to `127.0.0.1:8000` — nothing is on the LAN or the public
internet. `tailscale serve` publishes it to your tailnet over HTTPS:

```bash
sudo tailscale serve --bg --https=443 localhost:8000   # or: --bg 8000 (plain HTTP)
sudo tailscale serve status                            # prints the URL
```

Then open that URL (e.g. `https://raspberrypi.<tailnet>.ts.net/`) from any
device signed into your tailnet — laptop, phone, whatever. Off the tailnet the
URL doesn't resolve; tailnet membership is the only "login."

## The watchlist (company career pages)

The watchlist scrapes specific companies' job boards directly via their ATS APIs
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Workday, Oracle).

**You don't run `detect_platforms.py` manually anymore** — just add company names
to `companies.txt`. On the next cycle the `detect` phase probes each new name,
finds its ATS, and adds it to `scraper_config.json` automatically. Names it can't
resolve are logged to `watchlist_misses.txt`.

If auto-detection misses a company, look up its slug from a job URL on their
careers page and add it as a hint using the pipe syntax:

```
# companies.txt
Stripe                         ← auto-detected fine
Weird Corp | weirdcorp         ← try "weirdcorp" as the slug first
Odd Inc | odd-inc | oddinc     ← try multiple slugs in order
```

Slug hints are tried before the auto-generated variants. If a company was
previously logged as a miss, adding a hint causes it to be re-probed on the
next cycle.

`detect_platforms.py` (full rebuild from a list) and `verify_watchlist.py`
(spot-check detected entries) remain available for manual use:

```bash
python3 scripts/detect_platforms.py config/companies.txt   # writes data/watchlist_found.json
python3 scripts/verify_watchlist.py                         # sanity-check the detected boards
```

### Workday companies

Large enterprises (IBM-scale) often run **Workday**, which has no single-slug public
API like the others — each employer lives at `{tenant}.{dc}.myworkdayjobs.com/{site}`
and is reached via an undocumented `wday/cxs/{tenant}/{site}/jobs` endpoint the hosted
career site itself calls. The scraper supports it as the `workday` platform, but the
`companies.txt` auto-`detect` phase can **not** resolve Workday employers (there's no
name→slug guess that works), so they're added with a dedicated helper.

The watchlist slug encodes the board as **`host/site`**, e.g.
`bitsight.wd1.myworkdayjobs.com/Bitsight`. The tenant is the host's first label; for
the rare tenant whose cxs name differs from its subdomain, append `|tenant` to the slug.

Resolve a company from either its Workday URL or its careers-page URL, verified against
the live API before you trust it (0-job boards are kept — they light up when the company
posts in a later hiring cycle):

```bash
# one company (a Workday URL or a careers URL that links to one)
python3 scripts/detect_workday.py https://www.bitsight.com/careers

# many at once — a file of "Label | careers-or-workday-URL" lines
python3 scripts/detect_workday.py --batch companies_with_urls.txt
```

It writes verified entries to `data/watchlist_workday.json`; paste them into the
watchlist `companies` array in `scraper_config.json`. From there the `verify` and
`scrape` phases treat Workday like any other platform.

Descriptions: the connector lists jobs (title/location/URL) with one request per 20
postings by default. To pull each posting's full description for the LLM (one extra
request per job), set `WORKDAY_FETCH_DESCRIPTIONS = True` in `scraper.py`.

### Oracle Cloud companies

Large finance/enterprise employers (JPMorgan, Akamai, …) often run **Oracle
Recruiting Cloud** (Candidate Experience). Like Workday it isn't name→slug
guessable and the `detect` phase can't resolve it. Each tenant lives at a host like
`{tenant}.fa.oraclecloud.com` (or a shared pod `fa-ext…saasfaprod1.fa.ocs.oraclecloud.com`)
with a career-site view identified by a `CX_####` site number, reached via the public
`recruitingCEJobRequisitions` REST endpoint. The scraper supports it as the `oracle`
platform; the watchlist slug is **`host/site`**, e.g. `jpmc.fa.oraclecloud.com/CX_1001`.

Resolve with the helper (from a careers URL or a direct Oracle URL). It samples job
titles so you can confirm identity — **important on shared pods**, where many tenants
share one host and the `CX_####` is what actually identifies the employer:

```bash
python3 scripts/detect_oracle.py https://www.jpmorganchase.com/careers
python3 scripts/detect_oracle.py --batch companies_with_urls.txt
```

It writes verified entries to `data/watchlist_oracle.json`; paste them into the
watchlist `companies` array. Descriptions are list-only by default — set
`ORACLE_FETCH_DESCRIPTIONS = True` in `scraper.py` for full text (one request per job).

## Running pieces by hand

Each script still works standalone — handy for testing:

```bash
# Test the filter without the model (instant)
python3 scripts/filter_jobs.py sample_jobs.json --dry-run --all

# Scrape once with a given config
python3 scripts/scraper.py --config config/scraper_config.json --out data/scraped_jobs.json

# Score a scrape into the CSV
python3 scripts/filter_jobs.py data/scraped_jobs.json

# Upsert the CSV's matches into the tracker DB (idempotent — ON CONFLICT DO
# NOTHING, so existing rows and tombstones are left untouched)
python3 scripts/store_matches.py

# Render a styled .xlsx snapshot of the DB's live jobs (also served at /export.xlsx)
python3 scripts/export_workbook.py
```

`filter_jobs.py` flags:
- `--all` — write every job to the CSV, not just matches (useful while tuning)
- `--rescore` — ignore `seen_jobs.txt` and re-evaluate everything
- `--csv path.csv` — custom output location
- `--dry-run` — skip the LLM entirely; tests file handling. Safe: writes to
  `data/dry_run_results.csv` (not the real tracker) and never marks jobs seen

If Ollama is down **or wedged**, `filter_jobs.py` aborts after 3 consecutive
failures (a wedged model would otherwise burn a full `timeout_seconds` per
job) and exits nonzero; the orchestrator keeps the checkpoint and retries the
filter phase every 15 minutes (`phase_retry_interval`). Already-scored jobs
stay in `seen_jobs.txt`, so retries only evaluate what's left.

## Pruning the tracker (manual)

The pipeline never deletes rows. When the tracker gets noisy, trim it to the
best 1–2 roles per company by hand:

```bash
python3 scripts/prune_workbook.py            # dry-run: report only, no writes
python3 scripts/prune_workbook.py --apply    # soft-delete (tombstone) the rest
```

Rows with anything hand-typed (Status, Notes, Date Applied, ...) are **never
deleted**. `--apply` writes a tombstone (`deleted_reason='prune'`) for each
pruned row in one transaction: the row leaves the site and the `.xlsx` export,
and the pipeline's `ON CONFLICT(key) DO NOTHING` never re-adds it — so there is
no suppress-list to maintain. If a prune was too aggressive, **Restore** the row
from its page in the web app. The fit/exclusion rules live at the top of
`prune_workbook.py` — retune them there when your preferences change.

## The scraper's job format

`scraper.py` writes — and `filter_jobs.py` reads — JSON shaped like this:

```json
{
  "jobs": [
    {
      "title": "...",        // required in practice
      "company": "...",
      "location": "...",
      "salary": "...",        // optional
      "url": "...",           // used for duplicate detection — include it
      "description": "..."    // the more text here, the better the scoring
    }
  ]
}
```

A bare JSON array `[ {...}, {...} ]` also works.

## Tuning tips

- Start with `--all` and a low threshold so you can see how the model scores
  everything, then tighten `threshold` in config.json once you trust it.
- Borderline scores (5-6) are where a small model is least reliable — skim those
  yourself rather than trusting the suitable=true/false flag blindly.
- Long descriptions are good, but if listings exceed ~3,000 words, raise
  `num_ctx` in config.json (costs RAM) or truncate in your scraper.
- Keep dealbreakers concrete ("requires security clearance") rather than vague
  ("bad culture") — small models follow explicit rules far better.
- After editing your profile, delete `seen_jobs.txt` (or run a manual
  `filter_jobs.py ... --rescore`) so existing jobs get re-scored under the new rules.
