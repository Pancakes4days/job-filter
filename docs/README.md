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
The sync phase pulls it first, appends only jobs it doesn't already contain, and
pushes it back — so the columns you fill in (Status, Notes, dates) are never
overwritten. `matched_jobs.csv` rides along as a full rewrite each time.

Default schedule is **6 AM and 1 PM** local time. On first launch it runs one
cycle immediately, then settles into the schedule.

### Built for unattended operation

- **Survives restarts.** The current phase is checkpointed to
  `orchestrator_state.json` before each step. If the process is killed (watchdog,
  OOM, crash) systemd restarts it and it resumes from the last checkpoint — the
  `seen_jobs.txt` list means the LLM only scores jobs it hadn't reached yet.
- **No overlap.** A `flock` on `orchestrator.lock` guarantees only one instance
  runs; phases run sequentially; a long run never stacks a second cycle on top.
- **Only stops when you say so.** It exits cleanly on `systemctl stop` (no
  restart). Any other exit is treated as a failure and restarted.
- **Tolerates an offline laptop.** If your laptop isn't on the Tailnet, the sync
  step is skipped (not failed) and retried every 15 minutes until it succeeds.
  Because `matched_jobs.csv` is cumulative and the append dedupes by URL, a
  skipped cycle is caught up on the next successful sync.
- **Never clobbers your edits.** If the workbook can't be pulled from the laptop
  when it should exist (e.g. you have it open in Excel), the sync is deferred and
  retried rather than overwriting it with a fresh copy.

## Layout

```
job_filter/
├── scripts/   the .py files + paths.py (shared directory layout)
├── config/    config.json, scraper_config.json, companies.txt  (you edit these)
├── data/      runtime state + outputs (auto-created, mostly git-ignored)
├── docs/      README.md, CHANGES.md
└── jobfilter.service
```

**Scripts** (`scripts/`)
- `orchestrator.py` — the daemon that drives everything (run this via systemd)
- `scraper.py` — pulls listings from job sources into `data/scraped_jobs.json`
- `filter_jobs.py` — scores jobs with the LLM, writes `data/matched_jobs.csv`
- `export_workbook.py` — appends new matches to `matched_jobs.xlsx` (needs `openpyxl`)
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

Then edit the settings block at the top of `scripts/orchestrator.py`:

```python
REMOTE_HOST = "100.64.0.1"                          # your laptop's Tailscale IP
REMOTE_USER = "youruser"                             # SSH user on the laptop
REMOTE_DIR  = "C:/Users/youruser/job_data"           # job_data folder on the laptop

SCRAPE_HOURS_LOCAL  = [6, 13]   # 6 AM and 1 PM local (Pi timezone)
COPY_RETRY_INTERVAL = 900       # seconds between copy retries when laptop is offline
DETECT_DELAY        = 0.5       # seconds between ATS probes during auto-detection
```

`REMOTE_HOST` can be the Tailscale IP (`tailscale ip -4` on the laptop) or its
MagicDNS name. `REMOTE_DIR` must be an existing folder (the sync pulls
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

# Watch it work
tail -f data/filter.log

# Stop it (clean shutdown, no restart)
sudo systemctl stop jobfilter
```

The unit pins the process to 3 CPU cores (`CPUQuota=300%`) so the Pi's watchdog
is less likely to kill it, and restarts on any failure but not on a clean stop.

## The watchlist (company career pages)

The watchlist scrapes specific companies' job boards directly via their ATS APIs
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee).

**You don't run `detect_platforms.py` manually anymore** — just add company names
to `companies.txt`. On the next cycle the `detect` phase probes each new name,
finds its ATS, and adds it to `scraper_config.json` automatically. Names it can't
resolve are logged to `watchlist_misses.txt`; for those, open the company's
careers page, read the slug from a job URL, and add it to the watchlist by hand.

`detect_platforms.py` (full rebuild from a list) and `verify_watchlist.py`
(spot-check detected entries) remain available for manual use:

```bash
python3 scripts/detect_platforms.py config/companies.txt   # writes data/watchlist_found.json
python3 scripts/verify_watchlist.py                         # sanity-check the detected boards
```

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
- `--dry-run` — skip the LLM entirely; tests file handling

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
