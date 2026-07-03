# Job Filter Pipeline — Raspberry Pi 5 + gemma3:4b

Scrapes job listings from multiple sources, scores them against your
skills/preferences with a local LLM, keeps an Excel tracker of the matches, and
syncs it to your laptop over Tailscale. Runs unattended as a systemd service so
you just open your laptop to a fresh list. No cloud, no API keys, and one
optional Python package (`openpyxl`, only for the Excel export).

## How it runs

`orchestrator.py` is a long-running daemon (managed by systemd). Twice a day it
fires a 5-phase pipeline:

```
1. detect   — auto-detect ATS platforms for any new companies.txt entries
2. verify   — probe each watchlist company's job board for live openings
3. scrape   — scraper.py: public sources + verified watchlist, single pass
4. filter   — filter_jobs.py scores each job via the local Ollama LLM
5. sync     — pull the laptop's tracker, append new matches (export_workbook.py),
              push the .xlsx + .csv back to job_data over Tailscale
```

The workbook you hand-edit lives on the **laptop** (`job_data/matched_jobs.xlsx`).
The sync phase pulls it first, appends only new matches, and pushes it back — so
the columns you fill in (Status, Notes, dates) are never overwritten, and **rows
you delete stay deleted**: a sync watermark (`data/export_mark.txt`) tracks the
newest batch the laptop has confirmed receiving, so an older row missing from
the workbook is recognized as your deletion, not re-added from the cumulative
CSV. `matched_jobs.csv` rides along as a full rewrite each time.

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
- **Tolerates an offline laptop.** If your laptop isn't on the Tailnet, the sync
  step is skipped (not failed) and retried every 15 minutes until it succeeds.
  Because `matched_jobs.csv` is cumulative and the append dedupes by URL, a
  skipped cycle is caught up on the next successful sync.
- **Never clobbers your edits.** If the workbook can't be pulled from the laptop
  when it should exist (e.g. you have it open in Excel), the sync is deferred and
  retried rather than overwriting it with a fresh copy.
- **Deletions are respected.** Rows you remove from the workbook stay removed.
  The sync watermark separates your deletions (older than the last confirmed
  sync, missing from the workbook) from new matches (newer), and only advances
  once the laptop confirms a push — so a failed push never strands new rows.

## Layout

```
job_filter/
├── scripts/   the .py files + paths.py (shared directory layout)
├── config/    config.json, scraper_config.json, companies.txt  (you edit these)
├── data/      runtime state + outputs (auto-created, mostly git-ignored)
├── docs/      handoff / working notes
├── jobfilter.service
└── jobfilter.logrotate
```

**Scripts** (`scripts/`)
- `orchestrator.py` — the daemon that drives everything (run this via systemd)
- `scraper.py` — pulls listings from job sources into `data/scraped_jobs.json`
- `filter_jobs.py` — scores jobs with the LLM, writes `data/matched_jobs.csv`
- `export_workbook.py` — appends new matches to `matched_jobs.xlsx` (needs `openpyxl`)
- `prune_workbook.py` — MANUAL: trims the tracker to the best 1–2 roles per company
- `detect_platforms.py` — one-time/manual full ATS detection from a company list
- `verify_watchlist.py` — manual helper to spot-check detected watchlist entries
- `paths.py` — defines `CONFIG_DIR` / `DATA_DIR`; the one place paths are set

**You edit these** (`config/`)
- `config.json` — your skills, preferences, dealbreakers, LLM settings
- `scraper_config.json` — job sources, keyword/location filters, watchlist
- `companies.txt` — company names you want watched (one per line; `#` comments ok)
- top of `scripts/orchestrator.py` — laptop address, schedule (see **Configuration**)

**Created automatically** (`data/`)
- `scraped_jobs.json` — latest scrape output
- `matched_jobs.csv` — flat results log (the machine record + dedup source);
  pushed to the laptop's `job_data` as a full rewrite each cycle
- `matched_jobs.xlsx` — the styled tracker. The copy you open and edit lives on
  the laptop in `job_data`; the Pi keeps a transient working copy during sync.
  New matches are appended as rows; your hand-typed columns (Status, Notes,
  dates) are preserved
- `data/job_data/` — a local backup on the Pi. Each sync drops the latest
  `matched_jobs.xlsx` + `matched_jobs.csv` here *before* pushing to the laptop, so
  the Pi always keeps its own copy (set `LOCAL_COPY_DIR = None` in `orchestrator.py`
  to disable)
- `seen_jobs.txt` — fingerprints of already-scored jobs (dedup across runs)
- `export_mark.txt` — sync watermark: the newest batch the laptop confirmed;
  rows older than it that you delete from the workbook are never re-added
  (`.pending` is the candidate awaiting push confirmation)
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

# 2. Install openpyxl (the only non-stdlib dependency, used by the export phase)
pip install openpyxl       # or: sudo apt install python3-openpyxl

# 3. Set the Pi's timezone so the schedule means local time (DST-safe)
sudo timedatectl set-timezone America/New_York

# 4. Passwordless SSH from the Pi to your laptop (needed for the copy step,
#    which runs non-interactively under systemd)
ssh-copy-id youruser@<laptop-tailscale-ip>
```

> The systemd unit runs `orchestrator.py` with `/usr/bin/python3`, so install
> `openpyxl` for that interpreter (a plain `pip install openpyxl`, the apt
> package, or a venv the unit points at — just keep them consistent).

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
  "remote_host": "100.x.y.z",
  "remote_user": "youruser",
  "remote_dir":  "C:/Users/youruser/job_data",

  "scrape_hours_local":  [6, 13],
  "copy_retry_interval": 60,
  "detect_delay":        0.5,
  "phase_retry_interval": 900
}
```

`scrape_hours_local` must list at least one hour (the orchestrator refuses to
start with an empty schedule). `phase_retry_interval` (seconds) paces retries
of a failed pipeline phase.

`remote_host` can be the Tailscale IP (`tailscale ip -4` on the laptop) or its
MagicDNS name. `remote_dir` must be an existing folder (the sync pulls
`matched_jobs.xlsx` from it and pushes both files back). macOS paths look like
`/Users/youruser/job_data`; Windows via OpenSSH uses a drive-letter path like
`C:/Users/youruser/job_data` (forward slashes, no leading slash) — that's what
the tested setup syncs to. Create it once on the laptop before first run.

## Install as a service

```bash
# Adjust User= and WorkingDirectory= in jobfilter.service to match your setup
sudo cp jobfilter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable jobfilter
sudo systemctl start jobfilter

# Log rotation (filter.log grows forever otherwise; adjust the path inside first)
sudo cp jobfilter.logrotate /etc/logrotate.d/jobfilter

# Watch it work
tail -f data/filter.log

# Stop it (clean shutdown, no restart)
sudo systemctl stop jobfilter
```

The unit pins the process to 3 CPU cores (`CPUQuota=300%`) so the Pi's watchdog
is less likely to kill it, and restarts on any failure but not on a clean stop.

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

# Append the CSV's matches into the Excel tracker (idempotent — re-running
# adds only jobs not already in the workbook; never touches existing rows)
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

The pipeline is **append-only** — it never deletes workbook rows. When the
tracker gets noisy, trim it to the best 1–2 roles per company by hand:

```bash
python3 scripts/prune_workbook.py                  # dry-run: report only
python3 scripts/prune_workbook.py --apply          # pull → prune → push back
python3 scripts/prune_workbook.py --apply --local  # prune the local copy only
```

Rows with anything hand-typed (Status, Notes, Date Applied, ...) are **never
deleted**. The dry run pulls the laptop's workbook to a temp file so the
preview matches what `--apply` will actually do (falling back to the local
copy, with a warning, if the laptop is unreachable). `--apply` pulls the
laptop's workbook, prunes it, pushes it back, and only after a **confirmed
push** records what it cut in `data/pruned_keys.txt` (so the cumulative CSV
never re-adds those rows) — a failed push records nothing, leaving state
consistent. `--local` prunes of the live workbook are transient (the next
sync restores the laptop's copy) and record no keys; giving a custom workbook
path implies `--local`. The fit/exclusion rules live at the top of
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
