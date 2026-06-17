# Job Filter — Change Handoff

Context document for an agent picking up this project. Summarizes all changes
made in the most recent work session. The project is a Raspberry Pi 5 job-search
pipeline (scrape → LLM-score → CSV) that now runs unattended via systemd.

## TL;DR of what changed

1. Fixed four low-severity code issues in `scraper.py` / `filter_jobs.py`.
2. Added `orchestrator.py` — a persistent daemon that drives the whole pipeline.
3. Added a `--config` flag to `scraper.py`.
4. Rewrote `jobfilter.service` from a one-shot into a persistent service.
5. Rewrote `README.md` to document the orchestrator workflow.

All Python files compile (`py -m py_compile`). Nothing has been run end-to-end on
a real Pi yet, and the Tailscale copy settings are still placeholders.

---

## 1. Low-severity fixes

### `scraper.py`
- **HTML stripping** — replaced the regex-based `strip_html` with a stdlib
  `HTMLParser` subclass (`_HTMLTextExtractor`) that tolerates malformed/nested
  markup, skips `<script>/<style>/<head>`, and inserts newlines at block tags.
  Falls back to the old regex approach only if the parser raises. New import:
  `from html.parser import HTMLParser`.
- **Rate-limiting** — `scrape_hn_hiring` now sleeps between its two Algolia API
  calls (`source_cfg.get("request_delay", 1)`).

### `filter_jobs.py`
- **CSV file locking** — added a conditional `import fcntl as _fcntl` (no-op on
  Windows). `append_csv` now takes an exclusive `flock` before writing, released
  on file close. Prevents interleaved rows under concurrent writes.
- **Ollama error messages** — split the single `except (URLError, TimeoutError)`
  into specific cases: connection-refused → "OLLAMA OFFLINE", other URL errors →
  "OLLAMA NETWORK ERROR", and `TimeoutError` → a message pointing at `num_ctx`.

---

## 2. `orchestrator.py` (NEW — the main addition)

A long-running daemon, intended to run under systemd. Replaces the old "run
scraper then filter via cron" approach.

### Pipeline (5 phases, fires twice daily)
```
detect → verify → scrape → filter → copy
```
- **detect** — incremental ATS auto-detection for new `companies.txt` entries
- **verify** — probes each watchlist company's ATS for live job counts
- **scrape** — single `scraper.py` run (public sources + verified watchlist)
- **filter** — `filter_jobs.py` scores jobs via local Ollama, writes CSV
- **copy** — `scp` the CSV to the laptop over Tailscale

> Note: an earlier draft of this session had 7 phases with two separate
> `scraper.py` calls (public vs. watchlist) plus a `merge` step. That was
> consolidated into one `scrape` phase because `scraper.py` already dedupes
> across all its sources in a single pass. Do not reintroduce the split.

### Key behaviors
- **Crash/restart recovery** — current phase written to `orchestrator_state.json`
  before each step. On restart, completed phases are skipped and the pipeline
  resumes. `seen_jobs.txt` ensures the LLM only scores unprocessed jobs.
- **No-overlap guard** — `acquire_singleton_lock()` uses `fcntl.flock` (LOCK_NB)
  on `orchestrator.lock`. A second instance exits immediately. No-op on Windows.
- **Clean shutdown** — SIGTERM/SIGINT set a `_shutdown` flag; the process finishes
  the current step then exits 0 (so `systemctl stop` does not trigger a restart).
- **Schedule** — `SCRAPE_HOURS_LOCAL = [6, 13]` interpreted in the Pi's *local*
  timezone (set Pi to `America/New_York`) so DST is handled by the OS. Scheduling
  uses naive local datetimes; the copy-retry timer uses aware UTC. These two
  clocks are deliberately kept separate (do not mix them — it raises TypeError).
- **First launch** — if no state file exists, runs one cycle immediately, then
  settles into the schedule.
- **Tailscale copy with hold-off** — `copy_csv()` calls `laptop_online()` (TCP
  connect to port 22, 5s timeout). If offline, sets `copy_pending` and skips
  (does not fail). `idle_until()` retries every `COPY_RETRY_INTERVAL` (900s)
  while waiting for the next scheduled run.

### Incremental company auto-detection
- Imports `detect` and `load_names` from `detect_platforms.py`.
- `detect_new_companies(state)` reads `companies.txt`, finds names not already in
  the watchlist and not already in `state["detect_attempted"]`, probes each, and
  writes hits straight into `scraper_config.json` via `add_to_watchlist()`.
  Misses are appended (deduped) to `watchlist_misses.txt`.
- Each probed company is checkpointed to state immediately (crash-resumable).
- `detect_attempted` is reset at the end of each full cycle; detected companies
  are skipped next cycle because they're now in the watchlist, but persistent
  misses get re-probed (self-healing).

### Settings block (top of file — STILL PLACEHOLDERS, must be edited)
```python
REMOTE_HOST = "100.64.0.1"                     # laptop Tailscale IP
REMOTE_USER = "yourusername"
REMOTE_DIR  = "/Users/yourusername/Downloads/jobs/"
SCRAPE_HOURS_LOCAL  = [6, 13]
COPY_RETRY_INTERVAL = 900
DETECT_DELAY        = 0.5
```

### State file shape (`orchestrator_state.json`)
```json
{
  "phase": "idle|detect|verify|scrape|filter|copy",
  "next_run": "<ISO local>",
  "verified_companies": [ { "platform": "...", "slug": "...", "label": "..." } ],
  "detect_attempted": ["Company A", "..."],
  "copy_pending": false,
  "last_copy_attempt": "<ISO UTC>"
}
```

---

## 3. `scraper.py` — `--config` flag
Added `--config <path>` (defaults to `scraper_config.json`). Lets the orchestrator
pass a temporary config that enables/disables sources and restricts the watchlist
to verified companies without mutating the real config file.

---

## 4. `jobfilter.service` (rewritten)
Was a one-shot that re-ran `filter_jobs.py`. Now a persistent service:
- `ExecStart=/usr/bin/python3 /home/bluke/job-filter/orchestrator.py`
- `Restart=on-failure`, `RestartSec=15` (restarts on crash/kill, not clean stop)
- `CPUQuota=300%` (pins to 3 of 4 cores so the watchdog is less likely to kill it)
- `After=network-online.target ollama.service`
- Logs to `filter.log`
- `User=bluke`, `WorkingDirectory=/home/bluke/job-filter` — **adjust to match the
  actual deployment user/path before installing.**

---

## 5. `README.md` (rewritten)
Now documents the orchestrator daemon, the 5-phase pipeline, unattended-operation
guarantees, Pi setup (Ollama + timezone + passwordless SSH Pi→laptop), the config
settings block, systemd install/stop, the auto-detection watchlist workflow, and
standalone usage of each script. Kept the scraper JSON contract and tuning tips.

---

## Outstanding / TODO for the next agent
- **Fill in the real Tailscale settings** in `orchestrator.py` (`REMOTE_HOST`,
  `REMOTE_USER`, `REMOTE_DIR`) — currently placeholders.
- **Confirm the systemd `User=` and `WorkingDirectory=`** match the real Pi user
  (currently `bluke` / `/home/bluke/job-filter`).
- **Set up passwordless SSH** from the Pi to the laptop or the copy step will hang
  under systemd (it runs non-interactively).
- **Not yet tested end-to-end on hardware.** Verify: first-launch immediate run,
  a mid-pipeline restart resuming correctly, and the offline-laptop copy retry.
- The model default in `filter_jobs.py` `call_ollama` is `"gemma4:e2b"` but docs
  use `gemma3:4b`; the real model comes from `config.json`. Worth confirming
  `config.json` sets `model` explicitly.

## File inventory
| File | Status |
|------|--------|
| `orchestrator.py` | NEW — daemon, all pipeline logic |
| `scraper.py` | MODIFIED — HTML parser, HN delay, `--config` flag |
| `filter_jobs.py` | MODIFIED — CSV flock, granular Ollama errors |
| `jobfilter.service` | REWRITTEN — persistent service |
| `README.md` | REWRITTEN — orchestrator workflow |
| `detect_platforms.py` | UNCHANGED — imported by orchestrator |
| `verify_watchlist.py` | UNCHANGED |
| `config.json`, `scraper_config.json`, `companies.txt` | UNCHANGED (user data) |
