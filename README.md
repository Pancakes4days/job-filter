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
| **Scraping** | Python 3 + `urllib` | Hits ATS APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Workday, Oracle) directly, plus job-board APIs/feeds and community GitHub job lists. No scraping framework. |
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
- `newgrad_watch.py` — 15-minute fast lane: polls `tier1` companies for new-grad postings and emails them, skipping the LLM entirely
- `notify.py` — the email sender behind it (stdlib SMTP; a printed dry run until configured)
- `export_workbook.py` — renders a styled `.xlsx` from the DB (needs `openpyxl`)
- `prune_workbook.py` — MANUAL: soft-deletes all but the best 1–2 roles per company
- `prune_internships.py` — MANUAL: tombstones internship rows the old profile let in (`--undo` reverses it)
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
# 1. Install Ollama and pull the model (3.3 GB resident on an 8 GB Pi).
#    Before swapping this for any other model, check `ollama list` SIZE against
#    the Pi's free RAM — a tag's parameter count is not its footprint. gemma4:e4b
#    is 9.6 GB despite the "4b", does not fit, and gets OOM-killed mid-load.
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

### How the location filter reads multi-city postings

`location_include` / `location_exclude` are matched **per place**, not against
the whole location string. A posting open in several cities arrives semicolon-
joined (`London, United Kingdom; New York, NY`), and it is kept if *any* single
place passes — exclude still beats include within one place, so `Remote (Canada)`
stays out.

Only `;` splits a list. A comma is ambiguous (`United Kingdom, Remote` is one
place; `New York, London` is two), so comma-joined lists are judged whole and a
few will still be dropped for a city you didn't want.

The `remote_*` keys are **optional** and only used by `backup_db.py --push` to
copy nightly DB snapshots off the Pi — the pipeline no longer syncs anything to
a laptop. `remote_host` can be a Tailscale IP (`tailscale ip -4`) or MagicDNS
name; `remote_dir` an existing folder to receive snapshots. Omit them if you
don't push backups.

## Install as a service

Four systemd units, all with `User=` / `WorkingDirectory=` you adjust to match
your setup: the **pipeline**, the **web app**, the **nightly DB backup**, and the
**new-grad fast lane**.

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

# 4. New-grad fast lane (every 15 min, 06:00–22:45)
sudo cp jobfilter-newgrad.service jobfilter-newgrad.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobfilter-newgrad.timer

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

## The new-grad fast lane

The main pipeline runs twice a day and spends most of that time in the LLM
filter. For a competitive new-grad req — where applying first is most of the
advantage — a posting that goes live at 14:00 wouldn't reach you until 06:00 the
next morning. `newgrad_watch.py` is the low-latency path around that.

Flag the companies worth interrupting you for with `"tier1": true` in the
watchlist:

```json
{
  "platform": "workday",
  "slug": "capitalone.wd12.myworkdayjobs.com/Capital_One",
  "label": "Capital One",
  "tier1": true
}
```

Every 15 minutes it polls just those boards, matches **raw** titles with
`recruitment_watch.is_new_grad()`, applies `location_include`/`location_exclude`,
and emails whatever it hasn't already told you about. No LLM, no DB write, no
full scrape — about 4 seconds per company. Workday boards are sorted
newest-first, so it reads 2 pages instead of 25.

Internships and co-ops are excluded. `is_new_grad()` requires a new-grad signal
**and** no internship signal, because dropping the intern patterns alone isn't
enough — "Summer Intern 2027" still matches on the year.

Matching raw titles matters: `recruitment_watch.py` reads the scraper's *output*,
so a posting whose title misses `include_keywords` (say "Associate Software
Engineer, Technology Development Program") is filtered away before it can raise
an alert. The fast lane sees it.

Alerted postings are remembered by URL in `data/newgrad_seen.json` for 120 days.
It's SMS-shaped on purpose but delivered as email: the only free SMS path is the
carrier email-to-text gateways, which carriers filter and retire without notice —
you'd find out it broke by *not* getting the alert you cared about.

### Setting up email

Gmail rejects account passwords over SMTP, so `notify.py` authenticates with a
**Google App Password** — a separate 16-character credential that works only for
mail, needs no 2FA prompt (a script can't type a code), and can be revoked on its
own. Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords);
2-step verification must be on first, since the app password stands in for it.

It is used in exactly one place: `config/local.json` → `notify.app_password`,
handed to `smtplib.login()` over TLS. Never logged, and that file is gitignored.

`local.json` is a single JSON object, so the block goes **inside** the outer
braces, after a comma — pasting it below the closing `}` is invalid JSON and the
most common way this fails:

```json
{
  "scrape_hours_local":  [6, 13],
  "detect_delay":        0.5,

  "notify": {
    "enabled":      true,
    "smtp_host":    "smtp.gmail.com",
    "smtp_port":    587,
    "username":     "you@gmail.com",
    "app_password": "abcdefghijklmnop",
    "to":           "you@gmail.com"
  }
}
```

Confirm it parses, then prove the mailbox works:

```bash
python3 -c "import json;print(list(json.load(open('config/local.json'))))"  # 'notify' should be listed
python3 scripts/notify.py           # reports live, or names what's missing
python3 scripts/notify.py --test    # actually sends
```

Until `notify.enabled` is true, every alert is a **printed dry run** and postings
are deliberately *not* marked as seen, so switching email on later can't skip
anything that queued up in the meantime. The first live run therefore emails the
whole current backlog — run `--dry-run` first if you want to see its size.

### Day-to-day

```bash
python3 scripts/newgrad_watch.py --list      # which companies are tier-1
python3 scripts/newgrad_watch.py --dry-run   # print the email, touch no state
python3 scripts/newgrad_watch.py --all       # every match, ignoring seen-state
tail -f data/newgrad.log                     # what the timer is doing
systemctl list-timers jobfilter-newgrad      # when it next fires
```

**If alerts go quiet and you're not sure whether that's real**, run `--all`. It
re-reports every current match ignoring the seen-file, which separates "nothing
new was posted" from "something broke". `--dry-run` is always safe to repeat: it
sends nothing and writes nothing.

To replay alerts from scratch, `rm data/newgrad_seen.json`.

`notify.py` degrades rather than failing silently. A malformed `local.json`,
`enabled: false`, or a blank `app_password` each name themselves; a wrong
password gives `SMTPAuthenticationError` and dumps the unsent alert to stdout,
and a send failure leaves the seen-file untouched so the next tick retries.

## Community job lists (GitHub repos)

The `github_list` source type reads the curated new-grad/internship lists people
maintain as GitHub repos — [speedyapply/2027-SWE-College-Jobs][speedy],
[vanshb03/New-Grad-2027][ng27], [SimplifyJobs/New-Grad-Positions][simplify], and
the many forks of all three. These are markdown files, so the "API" is just the
`raw.githubusercontent.com` URL; one request gets the whole list. It's the
cheapest source in the pipeline and the only one that surfaces companies you
never added to `companies.txt`.

[speedy]: https://github.com/speedyapply/2027-SWE-College-Jobs
[ng27]: https://github.com/vanshb03/New-Grad-2027
[simplify]: https://github.com/SimplifyJobs/New-Grad-Positions

Add one entry to the `sources` array in `scraper_config.json` per list:

```json
{
  "name": "GitHub list - 2027 SWE New Grad USA (speedyapply)",
  "type": "github_list",
  "repo": "speedyapply/2027-SWE-College-Jobs",
  "branch": "main",
  "path": "NEW_GRAD_USA.md",
  "enabled": true,
  "exclude_sections": ["quant"]
}
```

`repo` + `branch` + `path` build the raw URL; `branch` defaults to `main` and
`path` to `README.md`. Two things to check on any new repo: these lists often
live on a `dev` branch rather than `main`, and a repo may split its listings
across several files (`NEW_GRAD_USA.md`, `NEW_GRAD_INTL.md`, `Canada.md`, …) —
`path` is how you pick one, and you add a second source entry for a second file.

### Pasting a URL instead

You can skip `repo`/`branch`/`path` and put a `"url"` in — including the plain
`github.com` link straight from your browser's address bar. All of these are
normalised to the same `raw.githubusercontent.com` fetch and produce identical
rows:

```
https://github.com/speedyapply/2027-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md
https://github.com/speedyapply/2027-SWE-College-Jobs/raw/main/NEW_GRAD_USA.md
https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md
```

`?plain=1` and `#L40` fragments are ignored. A bare repo root
(`https://github.com/vanshb03/New-Grad-2027`) or a `/tree/<branch>` link works
too — those carry no filename, so they fall back to `branch`/`path`, which is
where you'd set `"path": "NEW_GRAD_INTL.md"`. A non-GitHub URL is fetched as-is,
so raw markdown hosted anywhere else still works.

**Why this matters:** fetching a `/blob/` URL without normalising it *looks*
like it works — GitHub renders the markdown, and the rendered `<table>` parses
fine — but every `## Section` heading comes back as an `<h3>`, so
`include_sections`/`exclude_sections` silently become no-ops (13 quant rows leak
into the speedyapply source) and you pull ~9× the bytes. Hence the rewrite.
Because the repo is recovered from the URL either way, the `source` field is
always `github_list/owner/name` — switching an entry between `url` and
`repo` form won't fork the source in `pipeline_stats.py`.

Optional keys:

| Key | Default | What it does |
|---|---|---|
| `include_sections` | *(all)* | Keep only rows under a heading containing one of these (case-insensitive substring), e.g. `["software engineering"]` |
| `exclude_sections` | *(none)* | Drop rows under a matching heading — `["quantitative", "hardware"]`. Beats `include_sections` |
| `skip_closed` | `true` | Drop rows marked 🔒 (application closed) |
| `skip_no_sponsorship` | `false` | Drop rows marked 🛂 (no visa sponsorship) |
| `skip_citizenship` | `false` | Drop rows marked 🇺🇸 (U.S. citizenship required) |
| `skip_advanced_degree` | `false` | Drop rows marked 🎓 (Master's/PhD/MBA required) |
| `tags` | `["new grad", "entry level"]` | Prepended to each description as a `TAGS:` line — see below |
| `max_jobs` | `0` (no cap) | Stop after N rows; handy for a quick test |

The three `skip_*` flags default to off because the LLM already judges those
against your `dealbreakers` — but the markers are written into the description
either way, so the model sees them. Turn a flag on to drop those rows before
they cost you any LLM time.

### What it can and can't give the LLM

The rows carry **Company / Role / Location / apply link / date (and sometimes a
salary) only — no job description**. The connector synthesises one from those
fields, the same list-only shape the Workday and Oracle connectors produce with
descriptions turned off. Scoring is therefore title-and-location driven; for the
companies you care about, the watchlist still gets you the full posting text.

That thinness is also why `tags` exists. `keyword_prefilter` matches against
title + description, and a bare "Software Engineer" title hits none of your
`include_keywords` — those rows would be dropped before the LLM ever saw them.
The `TAGS:` line makes them pass, which is correct here: these lists are curated
new-grad-only by construction. `exclude_keywords` (senior, principal, …) still
applies. Set `"tags": []` if you'd rather have the include filter judge these
rows on their titles alone.

### Adding a different repo

Both table dialects in the wild are handled — markdown pipe rows and the HTML
`<table>` rows SimplifyJobs switched to — as are the shared conventions: `↳`
repeats the company from the row above, `<details><summary>` collapses
multi-location cells, `</br>` separates them, and the apply cell's real link is
picked out from the aggregator trackers beside it.

Columns are matched **by header name**, not position, so a repo that renames,
reorders, or omits them still works, and a file with several tables of different
shapes is fine (each table re-reads its own header). Recognised labels:

| Field | Header labels |
|---|---|
| company | Company, Employer, Organization |
| title | Role, Position, Title, Job Title |
| location | Location, Locations |
| url | Application, Application/Link, Apply, Apply Link, Link, **Posting** |
| salary | Salary, Compensation, Pay |
| date | Date Posted, Date, Posted, Age |

A table whose header has no recognisable Company **and** Role column is skipped
entirely — which is also the failure mode to watch for on a new repo. If a list
uses a column name that isn't above, every row silently drops out (this is what
`Posting` did before it was added), so **always check the listing count against
the repo before trusting a new source**:

```bash
python3 scripts/scraper.py --config config/scraper_config.json \
        --out /tmp/test.json --include-seen --no-prefilter
```

That prints one line per source (`Fetching <name> ... N listings`). Compare `N`
to the number of table rows in the file; if it's 0 or obviously short, the
header labels or `include_sections` are the first thing to check. Add the
missing label to `_GH_COLUMNS` in `scraper.py` if it's a new one.

Tracking parameters (`utm_*`, `ref`) are stripped from apply links, so the URL —
which is the pipeline's dedup fingerprint — stays stable if a repo retags.

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
