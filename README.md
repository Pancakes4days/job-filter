# Job Filter Pipeline — self-hosted, local LLM

Scrapes job listings from multiple sources, scores them against your
skills/preferences with a local LLM, and tracks the matches in a SQLite database
you browse and edit from a small web app served over Tailscale. Runs unattended
as a systemd service so you just open the site to a fresh list. No cloud and no
API keys — the only dependencies are Ollama (the LLM), Flask + Gunicorn (the web
app), and openpyxl (the on-demand `.xlsx` export); everything else is Python's
standard library.

Any always-on Linux box with systemd works. It was built on a Raspberry Pi 5
with `gemma3:4b` — the tightest target — and notes throughout call out where a
choice was a Pi constraint rather than a design decision. Moving to a larger
host: **`docs/MIGRATION_archlab.md`**.

## Tech stack

Everything runs on the host; your other devices are just browsers pointed at it.

| Layer | What | Notes |
|---|---|---|
| **Data** | SQLite (`data/tracker.db`, WAL mode) | The single system of record. File-based, no DB server. `db.py` is the only code that touches it — hand-written SQL, no ORM. |
| **Scoring** | Ollama + `gemma3:4b` | Local LLM that scores each job. The heavy compute, and the floor on what hardware you need. Model tag is `config.json`'s `model` — see [Choosing a model](#choosing-a-model). |
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
if the host was off (or a long cycle overran) when a slot fired, the next start
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
├── docs/      PLAN_web_tracker.md, MIGRATION_archlab.md, working notes
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
- `templates/` — Jinja pages: `dashboard`, `jobs`, `job`, `network`, `contact`,
  `watchlist`, `logs`, plus the `no_db` / `stale_schema` / `not_found` notices
- `static/style.css` — the whole stylesheet; no build step, no CDN

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
  hand-edited columns, with tombstones for deletions, and the `contacts` table
  behind the Network tab. The web app reads and writes this; `.db-wal` /
  `.db-shm` are its WAL sidecar files
- `backups/` — nightly `tracker-YYYYMMDD.db` snapshots (kept 14 by default)
- `matched_jobs.xlsx` — only when you run/download the export; a styled snapshot
  of the DB's live jobs, not a source of truth
- `seen_jobs.txt` — fingerprints of already-scored jobs (dedup across runs)
- `orchestrator_state.json` — pipeline checkpoint for crash recovery
- `orchestrator.lock` — single-instance guard
- `watchlist_misses.txt` — companies whose ATS couldn't be auto-detected
- `filter.log` — combined service log

## One-time host setup

```bash
# 1. Install Ollama and pull the model. See "Choosing a model" below before
#    picking a tag — a tag's parameter count is not its footprint, and RAM
#    decides whether it loads while memory bandwidth decides whether it's fast.
#    Debian/Raspberry Pi OS:
curl -fsSL https://ollama.com/install.sh | sh
#    Arch (packaged; use ollama-cuda / ollama-rocm if you have a GPU):
sudo pacman -S ollama
sudo systemctl enable --now ollama

ollama pull gemma3:4b
ollama run gemma3:4b "Say hello in five words."   # sanity check

# 2. Dependencies. openpyxl (the .xlsx export) + the web app (Flask/gunicorn).
#    Debian:
sudo apt install python3-openpyxl python3-flask gunicorn
#    Arch — `base` has no Python, and system Python is externally-managed
#    (PEP 668), so pip refuses; pacman keeps /usr/bin/{python3,gunicorn}
#    populated, which is exactly what the units invoke:
pacman -Ss gunicorn      # confirm: 'gunicorn' vs 'python-gunicorn'
sudo pacman -S python python-flask python-openpyxl gunicorn sqlite rsync logrotate

# 3. Set the timezone so the schedule means local time (DST-safe).
sudo timedatectl set-timezone America/New_York

# 4. RASPBERRY PI ONLY — make boots WAIT for a synced clock. The Pi has no RTC
#    battery, so a post-outage boot can score jobs with a stale clock and give
#    them wrong Date Found timestamps. On any machine with an RTC this only
#    delays boot for every other service; skip it.
sudo systemctl enable systemd-time-wait-sync.service

# 5. (Optional) Passwordless SSH to another machine, only if you want
#    `backup_db.py --push` to copy nightly snapshots off-device.
ssh-copy-id youruser@<other-tailscale-ip>
```

> The systemd units run with `/usr/bin/python3` and `/usr/bin/gunicorn`, so
> install these for the system interpreter (distro packages, or a venv the units
> point at — just keep them consistent). See `docs/PLAN_web_tracker.md` for
> installing `jobfilter-web.service`, `jobfilter-backup.timer`, and exposing the
> site with `tailscale serve`.

### Choosing a model

`config.json`'s `model` must match an `ollama list` tag exactly. Three things
constrain the choice beyond raw size:

- **`filter_jobs.py` uses Ollama structured outputs** (`"format": RESULT_SCHEMA`).
  Thinking-capable models (Gemma 4, `qwen3`) must be sent `"think": False` in the
  payload, or the `<think>` blocks fight grammar-constrained decoding and
  multiply latency. Always-reasoning models like `deepseek-r1` can't be used.
- **The rubric is Python, not prompt.** `score_from_classification` computes the
  score from the model's *labels*; the model only classifies. Staying in one
  family keeps those labels calibrated against a rubric you tuned elsewhere.
- **RAM decides whether a model loads; memory bandwidth decides whether it's
  fast.** The engine streams the weights it needs once per generated token.

Check `ollama list` SIZE against free RAM first — `gemma4:e4b` is 9.6 GB despite
the "4b", doesn't fit an 8 GB Pi, and gets OOM-killed mid-load.

For **dense** models, per-job cost then scales roughly with file size: at
~45 GB/s a `gemma4:12b` (~8 GB) costs about 2.5× a `gemma3:4b` (3.3 GB).

**Mixture-of-experts models break that rule, in your favor.** `gemma4:26b` keeps
all 26B parameters resident (~18 GB) but activates only ~4B per token, so it
streams like a 4B model while scoring like a 26B one — the best quality-per-
second available to a CPU-only host with RAM to spare. A dense `gemma4:31b` or
`gemma3:27b` of similar footprint is 3-4× slower for no gain unless you have a
GPU. See `docs/MIGRATION_archlab.md` for the full comparison.

Two settings move with the model. `num_ctx` defaults to 4096, an 8 GB-Pi
compromise — `build_user_prompt` passes the **full** job description with no
truncation, so long postings overflow and Ollama silently drops tokens,
sometimes including the system prompt carrying the rubric. Raise it to 8192 if
you have the RAM; on a constrained host that may matter more than the model. And
raise `timeout_seconds` with it: a slow outlier that exceeds it counts as a
failure, and three consecutive failures abort the whole filter phase.

Jobs already in `data/seen_jobs.txt` are never re-evaluated, so a new model only
sees *new* postings. To apply it to the backlog, `filter_jobs.py --rescore` then
`store_matches.py` — safe, because `ON CONFLICT DO NOTHING` plus tombstones mean
a rescore can only *add* jobs the old model wrongly rejected. Update
`SECONDS_PER_JOB` in `scripts/pipeline_stats.py` once you've measured the new
rate, or the dashboard ETA will lie.

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

Five systemd units, all with `User=` / `WorkingDirectory=` you adjust to match
your setup: the **pipeline**, the **web app**, the **nightly DB backup**, the
**new-grad fast lane**, and the **newgrad-jobs.com lane**.

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

# 5. newgrad-jobs.com lane (every 2h; see "newgrad-jobs.com" below for why)
sudo cp jobfilter-ngjobs.service jobfilter-ngjobs.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobfilter-ngjobs.timer

# Log rotation (filter.log grows forever otherwise; adjust the path inside first)
sudo cp jobfilter.logrotate /etc/logrotate.d/jobfilter

# Watch it work
tail -f data/filter.log
sudo systemctl stop jobfilter        # clean shutdown, no restart
```

Both restart on failure but not on a clean stop.

### Memory budget

Relevant only when the box shares RAM with something else - on archlab it shares
with a hand-started Spigot server, and that combination produced real OOM kills.
Everything below is measured on that host (2026-09-11), not estimated.

#### Read the kill log before theorising

```bash
journalctl -k -b --grep='killed process|out of memory'
```

Note the quoting: `--grep -i 'pattern'` makes `--grep` swallow `-i` as its value
and the pattern is then parsed as a field match, failing with a confusing
"Failed to add match ... Invalid argument". Use `--grep='...'`.

On archlab that produced three kills, and every line contradicted a guess made
beforehand:

```
Sep 05 06:22:59  Killed process 52703  (llama-server)  anon-rss:17999112kB  UID:965
Sep 09 13:26:53  Killed process 314338 (llama-server)  anon-rss:22351996kB  UID:965
Sep 10 06:24:06  Killed process 376953 (llama-server)  anon-rss:21696328kB  UID:965
```

**The victim was always Ollama, never the JVM.** `UID:965` is the ollama user.
The Minecraft server was never killed - what presented as "Minecraft is being
disrupted" was memory pressure degrading it, plus scoring runs dying. An
`OOMScoreAdjust=500` already on `ollama.service` had been doing its job.

**The kills track the main pipeline, not the hourly lane.** 06:22, 13:26 and
06:24 are 22-26 minutes into the 06:00 and 13:00 slots. `jobfilter.service`
scores hundreds of jobs per cycle; `jobfilter-ngjobs` scores single digits. The
lane that *fires* most often is not the one that costs most - reach for the kill
timestamps before reasoning about schedules.

**`anon-rss` is 18-22GB while `file-rss` is kilobytes.** Ollama's footprint is
almost entirely anonymous memory, not file-backed mmap. That decides what a cap
can do: anonymous pages cannot be dropped and re-read, only swapped. A
`MemoryHigh` below the real need buys swapping, not graceful degradation. Do not
set one expecting "it just runs slower".

#### What the lanes actually cost

Inference memory is **not** proportional to how many jobs a run finds. Any run
that gets one ungated job past `gate_job()` pulls the whole model resident, the
same for one posting as for fifty.

| Lane | Fires | Calls Ollama? | Memory |
|---|---|---|---|
| `jobfilter-newgrad` | every 15 min | **no** | tens of MB, transient |
| `jobfilter-ngjobs` | every 2h | yes | ~19GiB, small batches |
| `jobfilter` | 06:00, 13:00 | yes | **~22GiB** - the expensive one |

`jobfilter-newgrad` looks like the aggressive one and is nearly free.

#### Do not cap Ollama. Prioritise it instead.

This was got wrong twice before the kill log made it obvious, so the reasoning
is worth keeping.

A `MemoryHigh` on `ollama.service` looks like the right tool and is not. Ollama
logs, on every CPU load:

```
msg="disabling mmap for llama-server load by default" reason=cpu
```

With mmap off the weights are **anonymous** memory, not file-backed. Anonymous
pages cannot be dropped and re-read; they can only go to swap. So at
`memory.high` the kernel cannot reclaim, the cgroup stalls, and the OOM killer
resolves it. A cap below true demand does not degrade gracefully — it *causes*
the kill:

```
Sep 12 06:23:39  Killed process 2110534 (llama-server)  anon-rss:17809520kB  oom_score_adj:500
```

`MemoryPeak` at that moment was 23632752640 (22.008GiB) against a `MemoryHigh`
of 23622320128 (22.000GiB) — dead on the limit, while the machine still had
~8.5GiB free. The box was not short of memory. The cgroup was.

**What works is telling the kernel who to sacrifice, not how much to allow:**

```ini
[Service]
CPUWeight=50
# Ollama is the right thing to lose. filter_jobs aborts after three consecutive
# failures; the orchestrator keeps its checkpoint and retries the filter phase
# 15 min later, skipping phases already done. A killed scoring run costs ~17
# minutes. A killed Minecraft server costs whatever was unsaved.
OOMScoreAdjust=500
MemoryHigh=infinity
MemoryMax=infinity
```

```bash
sudo systemctl set-property user.slice MemoryMin=6G
sudo systemctl set-property user-1000.slice MemoryMin=6G
```

`MemoryMin` matched to the JVM `-Xmx` is the protection that matters: it makes
the heap the thing that is never reclaimed. Sized to `-Xmx`, not above it — the
JVM overhead beyond the heap is genuinely reclaimable and should stay that way.

**Both slices are required, not just the leaf.** In cgroup v2 the effective
`memory.min` is limited by every ancestor's, so setting it only on the session
scope yields an effective protection of zero — `systemctl show` cheerfully
reports the value while the kernel honours nothing. Verify against the kernel:

```bash
cat /sys/fs/cgroup/user.slice/memory.min
cat /sys/fs/cgroup/user.slice/user-1000.slice/memory.min
cat /sys/fs/cgroup/system.slice/ollama.service/memory.high    # expect: max
```

On archlab the Minecraft server is a hand-started Spigot JVM in a `screen`
session, **not** a systemd unit (`cat /proc/<pid>/cgroup` ->
`/user.slice/user-1000.slice/session-N.scope`), which is why the protection goes
on the slices above it. The scope itself cannot be persisted; its number changes
every login. A real system unit under `system.slice` would remove that wrinkle.

#### What Ollama actually loads

Its own sizing is not to be trusted. From the launch line and fit log:

```
--model <26b> -c 8192 -np 1 --mmproj <blob> --spec-type draft-mtp --spec-draft-model <blob>

| - Host       | 9499 = 8727 + 560 + 212 |
| - CPU_REPACK | 8167 = 8167 +   0 +   0 |
projected to use 9499 MiB ... will leave 22514 >= 3454 MiB, no changes needed
```

Three models resident, not one: the 26B, a **draft model** for speculative
decoding, and a **1.29GiB multimodal projector** that is pure waste here — job
descriptions are text. And the fit decision ("no changes needed") counted the
9499MiB Host line while ignoring the 8167MiB of `CPU_REPACK` directly above it.
Real total ~17.25GiB for the process, ~22GiB for the cgroup.

`-np 1` confirms a single slot, so concurrent lanes are **not** multiplying KV
cache — Ollama serialises them. If demand ever has to come down, the `mmproj`
and the draft model are the two levers that cost no scoring quality. Cutting
`num_ctx` is **not** one: 4096 reintroduces the prompt overflow described in the
migration notes, where long postings silently push the rubric out of context.

#### The budget

31.26GiB usable. Spigot is `-Xms6G -Xmx6G -XX:+AlwaysPreTouch`, so the heap is
pre-committed and RSS is a constant ~5.8-6.4GiB regardless of player count.

| | Ollama | Spigot | Left over |
|---|---|---|---|
| Original (`-Xmx8G`) | ~21.3 | 8.85 | **1.09GiB** -> kills |
| **Now (`-Xmx6G`)** | ~21.3 | 5.76 | **4.2GiB** |

Cutting the heap was the fix. It is the only change that created headroom; the
caps layered on top of it were what kept killing llama-server afterwards.

#### It recovers on its own

Worth knowing before treating a kill as an emergency. `filter.log`:

```
06:23:39  Phase 'filter' failed: Subprocess exited with code 1
06:23:39  Checkpoint kept — this phase will be retried without redoing earlier phases.
06:38:39  Resuming pipeline from phase: filter
06:40:36  === Pipeline complete ===
```

`RemoteDisconnected` in that traceback is `filter_jobs.py` watching llama-server
vanish mid-request. Across Sep 06-12, three of ~14 cycles hit this and all three
completed ~17 minutes late. The seen-list means nothing is scored twice and
nothing is skipped.


#### Levers in this repo

- **`keep_alive`** (`config/config.json`, sent per-request by `call_ollama`).
  Ollama's default is 5 minutes of idle residency after a run; `60s` returns the
  memory promptly. Per-request rather than an `OLLAMA_KEEP_ALIVE` drop-in so it
  is version-controlled beside the code that depends on it.
- **The 2h `ngjobs` schedule.** Worth keeping for tidiness, but note above that
  this lane was never the expensive one.

A `MemoryMax` on the jobfilter units would do nothing: they run `orchestrator.py`
and `filter_jobs.py`, which are HTTP requests and JSON parsing. All inference
lives in `ollama.service`. This is the same trap the Pi's `CPUQuota=300%` fell
into - see the migration notes.

`jobfilter.service` pins the pipeline to 3 CPU cores (`CPUQuota=300%`) so the
Pi's watchdog is less likely to kill it; `jobfilter-web.service` is capped at
`CPUQuota=50%`. **Both are Pi-specific and neither bounds inference.**
`orchestrator.py` and `filter_jobs.py` only make HTTP requests and parse JSON —
near-zero CPU. All the scoring load lives in `ollama.service`, a separate unit
outside those quotas, so raising them does nothing. On a host without a hardware
watchdog, delete both lines; to bound inference against other services on the
box, put the quota where the work actually happens:

```bash
sudo systemctl edit ollama      # [Service] / CPUQuota=500%
```

### Exposing the site over Tailscale

The web app binds to `127.0.0.1:8000` — nothing is on the LAN or the public
internet. `tailscale serve` publishes it to your tailnet over HTTPS:

```bash
sudo tailscale serve --bg --https=443 localhost:8000   # at the root
sudo tailscale serve status                            # prints the URL
```

Then open that URL (e.g. `https://raspberrypi.<tailnet>.ts.net/`) from any
device signed into your tailnet — laptop, phone, whatever. Off the tailnet the
URL doesn't resolve; tailnet membership is the only "login."

A node has one hostname and one port 443, so if you host other services on the
same box, give this one a sub-path instead of the root:

```bash
sudo tailscale serve --bg --https=443 --set-path=/jobfilter localhost:8000
```

Tailscale strips `/jobfilter` before forwarding, which would leave Flask
generating links rooted at `/` and every one of them broken. That's what
`ProxyFix(app.wsgi_app, x_prefix=1)` at `web/app.py:55` is for — it reads
`X-Forwarded-Prefix` and puts the prefix back on every `url_for()`, including
static assets. Nothing else in the app knows or records the path, so moving
between root, a sub-path, or a different host is only ever this one command.

## The Network tab (outreach contacts)

A job row says a company is hiring. It doesn't say whether you know anyone
there, and referrals are how most of these roles actually get filled. The
**Network** tab is the other half: people worth messaging at the companies the
pipeline is already surfacing, tracked in the same database.

Contacts live in `tracker.db`'s `contacts` table (schema v3) and are **typed by
hand**. That is deliberate. Scraping a public ATS API is fine; scraping people
is against LinkedIn's terms and gets accounts restricted, which costs you the
network you were trying to build. The useful volume here is a dozen good
messages a week, not a thousand rows — the lookup is a few minutes, and the
personalisation is what makes the message work at all.

The pipeline never writes this table. Unlike `jobs`, which needs the
PIPELINE/USER column split because two writers touch it, `contacts` has exactly
one writer — you, through the web UI — so there is nothing to keep disjoint.

### What the page gives you

- **No contact yet** — companies with live jobs at or above your scoring
  threshold where you've logged nobody, ranked by best score. This is the whole
  reason contacts live in the tracker DB instead of a spreadsheet: only this
  database knows both which companies are hiring you *and* which ones you have
  no one at. It's the weekly "go look these up" queue.
- **Follow-ups due** — open threads past their follow-up date, also surfaced on
  the dashboard. Contacts marked Referred / No response / Closed drop off on
  their own, so a dead thread stops nagging without you clearing the date.
- **Reply rate** — replies per message *sent*, not per name collected. That's
  the number that tells you whether the message itself is working.
- **Re: job** — a contact can be linked to a specific live req at that company,
  and the job's own page lists everyone you know there. Naming the posting is
  most of the difference between a message that gets answered and one that
  doesn't.

Add a contact from the tab itself, from a gap row, or straight from a job page
(the company and the req are prefilled). Removal is a soft delete, like a job —
but for a different reason: nothing would re-add a contact, it's just that a
row you typed yourself has no other copy.

### A note on company names

`No contact yet` joins contacts to jobs on the company string, so a contact
filed under `JaneStreet` leaves `Jane Street` showing as a gap forever. The
company field offers the pipeline's own spellings as you type — take them when
they're offered. The lookups are case-insensitive, so only real spelling
differences bite.

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

## newgrad-jobs.com

The `newgrad_jobs` source type pulls [newgrad-jobs.com][ngj]'s listings — an
hourly-updated aggregate of entry-level US/Canada postings, filtered to 0–2
years of experience.

[ngj]: https://www.newgrad-jobs.com/

```json
{
  "name": "newgrad-jobs.com",
  "type": "newgrad_jobs",
  "enabled": true,
  "categories": ["us/swe", "us/data_engineer", "us/ml_ai", "us/cyber_security"]
}
```

| Key | Default | What it does |
|---|---|---|
| `categories` | `["us/swe"]` | Which of the site's tabs to fetch, as `<country>/<name>` |
| `request_delay` | `1` | Seconds to wait between categories |

### Where the listings actually come from

newgrad-jobs.com has no listings of its own. It's a Webflow page whose job tabs
are iframes onto **jobright.ai** "minisite" pages, one per category, and those
are Next.js pages that ship their rows as JSON in `__NEXT_DATA__`. The connector
reads that JSON — no HTML scraping, and nothing that breaks when the Webflow
shell around it is restyled.

The XHR those pages use for infinite scroll (`POST /swan/mini-sites/list`) would
hand back a whole category in one call — ~2,000 rows for `us/swe` — but
jobright's `robots.txt` disallows `/swan/` and `/api/`, so the connector stays on
the server-rendered page, which is allowed and carries **the newest 50**.

That's a ceiling per *fetch*, not per day — but how much time it buys you varies
enormously by category and by hour of day:

| Category | 50 rows spans | Postings/hr |
|---|---|---|
| `us/swe` | **2–12h** | 23 in US business hours, 4 overnight |
| `us/data_analysis` | 22h | 2.3 |
| `us/ml_ai` | 52h | 1.0 |
| `us/cyber_security` | 65h | 0.8 |
| `us/data_engineer` | 168h | 0.3 |

**`us/swe` is bursty and needs a short scrape interval.** Measured mid-morning it
filled all 50 slots in 2.2 hours, with 34 postings in a single hour; scraping it
twice a day misses most of that category. The other four are comfortable at 12h+.

Scraping more often is the fix, and it is close to free: one page per category
(~110KB), no rate limiting observed, so three runs a day is 15 requests and
1.6 MB total. If a category goes quiet in the tracker, the answer is always a
shorter interval, never a longer one.

### It barely overlaps the GitHub lists

Worth knowing before deciding how much to invest in this source: on a measured
run, **1 of 60** rows duplicated a `github_list` row by company+title, and only
**3 of 60** were even at a company those lists mention anywhere.

The lists are curated FAANG+/startup SWE; this source aggregates LinkedIn,
Indeed and Handshake, so it surfaces a different population — defense primes,
health systems, consultancies, state agencies, regional employers (Booz Allen,
CrowdStrike, Oscar Health, Nuro, NC DIT). The two are complements, not
substitutes, which is why the coverage gap below was worth a dedicated lane
rather than shrugging at.

### The dedicated lane

Because `us/swe` refills its 50 slots in ~2 hours, the twice-daily pipeline sees
only a fraction of it. `jobfilter-ngjobs.timer` closes that gap by running a
normal scrape/score/store pass against this source alone, every 2 hours:

```bash
python3 scripts/scraper.py --only newgrad_jobs --out data/ngjobs_scraped.json
python3 scripts/filter_jobs.py data/ngjobs_scraped.json --csv data/ngjobs_matches.csv
python3 scripts/store_matches.py --csv data/ngjobs_matches.csv
```

`--only MATCH` keeps only the sources whose `type` or `name` contains `MATCH`.
It exists so the lane can share `scraper_config.json` with the main run instead
of needing a second config — duplicating the `include_keywords` and
`location_*` lists is the kind of thing that silently drifts, and then the two
runs disagree about what counts as a match. A `--only` that matches nothing is
an error rather than an empty run, so a typo can't look like "no new jobs".

The lane writes its own `--out` and `--csv` so it never trips over the main
pipeline's files, but **shares `seen_jobs.txt` and the tracker DB on purpose**:
nothing gets scored by the LLM twice, and `store_matches.py` is idempotent
(`ON CONFLICT DO NOTHING`), so rows and tombstones survive.

It does call Ollama, unlike the 15-minute fast lane. If it overlaps a main
pipeline run they queue rather than collide — Ollama serialises requests — so
the cost is a slower run, not a wrong one. Batches here are small (an hour of
new postings, after filtering, is typically single digits).

**This is not the new-grad fast lane.** `jobfilter-newgrad.service` polls tier-1
ATS boards every 15 minutes, emails you and writes no database; it exists to get
you in early. This lane is about *coverage*, and given the source's ~8h feed lag
it can never be the thing that gets you in first.

Note also that **the feed runs ~8 hours behind real time** — the newest row is
consistently that old. This is a bulk source for coverage, not a low-latency one;
`newgrad_watch.py` remains the lane that gets you in early.

### Categories

`<country>/<name>`, exactly as the site's own tab bar spells them:

`swe` · `data_analysis` · `data_engineer` · `ml_ai` · `cyber_security` ·
`business_analyst` · `product_management` · `project_manager` ·
`engineering_development` · `accounting_finance` · `consulting` · `marketing_gen` ·
`creatives_design` · `human_resources` · `sales` · `customer_service` ·
`legal_compliance` · `healthcare` · `supply_chain` · `arts_entertainment` ·
`education_training` · `management_executive` · `public_sector`

All 23 exist under `us/`; `ca/` carries a 13-category subset. The shipped config
fetches `us/*` only — every Canadian city is in `location_exclude`, so `ca/*`
rows would be scraped just to be thrown away.

**A category name that doesn't exist is not an error** — the page renders empty
rather than 404ing — so a typo shows up as a source that quietly returns nothing.
The connector prints a warning naming the category when one comes back empty;
that message is the thing to watch for after editing this list.

### What it gives the LLM

Rows carry title, company, location, salary, work model, company size, industry,
H1B-sponsorship status and jobright's extracted **qualifications** — but no full
job body. The connector synthesises a description from those, the same list-only
shape the Workday and Oracle connectors produce with descriptions off. The
qualifications text is real prose, so unlike `github_list` these rows carry
enough keyword surface to clear `keyword_prefilter` on their own and don't need
a `tags` crutch.

`h1bSponsored` is only passed through when it's a definite `Yes`/`No` — the
site's default is `Not Sure`, which says nothing about the employer.

Apply links point at jobright's own posting page (`jobright.ai/jobs/info/<id>`),
not the employer's ATS; you get the employer link one click on from there.
Tracking parameters are stripped, which matters more here than elsewhere:
jobright stamps the *category* into `utm_campaign`, and a posting that appears
under several tabs (they carry a `tabCategory` list) would otherwise arrive as
several distinct URLs and survive dedupe as duplicates.

## Built In

The `builtin` source type reads a [Built In][bi] job board — any city, category
and level you can express in the URL path.

[bi]: https://www.builtinnyc.com/jobs/dev-engineering/entry-level

```json
{
  "name": "Built In NYC - entry-level dev/engineering",
  "type": "builtin",
  "enabled": true,
  "url": "https://www.builtinnyc.com/jobs/dev-engineering/entry-level"
}
```

| Key | Default | What it does |
|---|---|---|
| `url` | the NYC entry-level dev/engineering board | Board to read; `?page=N` is added for you |
| `max_pages` | `10` | Upper bound on pages (25 cards each) |
| `request_delay` | `1` | Seconds to wait between pages |

Built In has no JSON feed, so this is the one connector that reads HTML. It keys
on the cards' `data-id` attributes and Font Awesome icon names (`fa-location-dot`,
`fa-sack-dollar`, …) rather than layout classes. If Built In changes the card
markup, the connector prints `had N cards but none parsed`, so a broken parser
doesn't just look like an empty board.

The board is sorted by relevance, not date, so every page is read on each run
(~5 pages, ~100 postings for NYC entry-level). `seen_jobs.txt` keeps repeats away
from the LLM. Filter with path segments only: `robots.txt` disallows `/jobs` URLs
that carry `?search=`.

Rows give the LLM the card's title, company, location, salary, work model,
level, industries, posting age and Built In's one-paragraph summary. They don't
include the full job body. Apply links point at Built In's own posting page, not
the employer's ATS. A job that's also on your watchlist still dedupes on
title+company+location.

## Running pieces by hand

Each script still works standalone — handy for testing:

```bash
# Test the filter without the model (instant)
python3 scripts/filter_jobs.py data/scraped_jobs.json --dry-run --all

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
