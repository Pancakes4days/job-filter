# Migrating the pipeline: Raspberry Pi 5 → archlab (Arch Linux)

Moving `job_filter` from the Pi 5 to `archlab`, a 6-core / 32 GB Arch box that
also hosts other services. The motivation is headroom for a scoring model the
Pi's 8 GB could never hold.

Both machines run the same user (`bluke`) and the same path
(`/home/bluke/job_filter`), so **none of the systemd units need path edits** —
`User=` and `WorkingDirectory=` are already correct. Verified, not assumed.

The Pi keeps running during the overlap and is kept as a **recovery image**.
There is no final re-sync back from it — see [Running both](#running-both).

---

## Status

| | Step | State |
|---|---|---|
| 0 | Copy the tree from the Pi | **done** |
| 1 | Align the working copy with git | **done** |
| 2 | Install packages | **done** |
| 3 | Switch the model | **done** — unproven until a cycle runs |
| 4 | Strip the Pi's CPU quotas | **done** |
| 5 | Generalize Pi-specific text | not started |
| 6 | Install and start the units | **done** — reboot survived |
| 7 | Publish over Tailscale | **done** — see the note, it is not `tailscale serve` |
| 8 | Verify, then measure the real scoring rate | partial — `SECONDS_PER_JOB` still unmeasured |
| 9 | Rescore the backlog | not started |

Remaining to call the migration finished: one full pipeline cycle on the new
model, the measured `SECONDS_PER_JOB` that falls out of it, the backlog rescore,
and the cosmetic pass in step 5.

---

## 0. The copy (done)

For the record, and for the re-run if you ever need it. `data/tracker.db` is
WAL-mode and `jobfilter-web` holds it open, so committed rows can be sitting in
`tracker.db-wal` — copying the `.db` alone loses them.

```bash
# On the Pi
sudo systemctl stop jobfilter jobfilter-web jobfilter-newgrad.timer jobfilter-backup.timer
sqlite3 ~/job_filter/data/tracker.db "PRAGMA wal_checkpoint(TRUNCATE);"

rsync -av --exclude '__pycache__' --exclude 'data/backups' \
  ~/job_filter/ bluke@archlab:~/job_filter/

sudo systemctl start jobfilter jobfilter-web jobfilter-newgrad.timer jobfilter-backup.timer
```

If MagicDNS doesn't resolve `archlab` from the Pi, that's the Pi's resolver, not
a key problem — `tailscale ip -4 archlab` and use the address, or
`sudo tailscale set --accept-dns=true` on the Pi.

### What the copy carries, and what it can't

The rsync brought code, `config/`, `data/`, **and `.git`** — so `~/job_filter`
on archlab is already a working clone. It could not bring:

| Not copied | Where it actually lives | Step |
|---|---|---|
| Ollama model blobs | `/var/lib/ollama` (`OLLAMA_MODELS`) | [3](#3-switch-the-model) |
| Flask / Gunicorn / openpyxl | system site-packages | [2](#2-packages) |
| systemd unit registration | `/etc/systemd/system` | [6](#6-install-and-start-the-units) |
| Tailnet identity + `serve` config | `/var/lib/tailscale` | [7](#7-publish-over-tailscale) |

> Always copy from the **Pi**, never from a dev checkout. `data/.dev_seed` exists
> only in dev trees, and its presence is the sole thing that lets
> `seed_dev_db.py` overwrite real state.

## 1. Align the working copy with git

The tree came with the Pi's `.git`, so there is nothing to clone. **Do not
delete and re-clone** — a fresh clone gets only tracked files, and everything
that made this migration worth doing is gitignored: `data/tracker.db` (the
entire tracking history), `config/local.json`, `data/seen_jobs.txt`,
`data/newgrad_seen.json`, `data/orchestrator_state.json`.

```bash
cd ~/job_filter
git status
git fetch origin
git --no-pager log --oneline HEAD..origin/main   # what GitHub has that you don't
git --no-pager diff --stat                       # what the Pi generated that GitHub doesn't
```

The remote is SSH (`git@github.com:Pancakes4days/job-filter.git`) and archlab is
a fresh box, so `git fetch` will fail with a genuine `Permission denied
(publickey)` until you add a key:

```bash
ssh-keygen -t ed25519 -C "bluke@archlab"
cat ~/.ssh/id_ed25519.pub      # paste into github.com/settings/keys
ssh -T git@github.com
```

**Expect modified tracked files, and keep them.** `config/scraper_config.json`
is rewritten by the pipeline — the `detect` phase writes newly-discovered ATS
entries into it, and `verify` rewrites the `data/watchlist_*.json` files. Those
diffs are real accumulated output, not drift. Commit them from archlab. On a
merge conflict in `scraper_config.json`, take the Pi's side; it's the superset.

## 2. Packages

Arch marks system Python externally-managed (PEP 668), so `pip` refuses. Pacman
keeps `/usr/bin/python3` and `/usr/bin/gunicorn` populated, which is exactly what
the units invoke — so no `ExecStart` edits are needed.

```bash
pacman -Ss gunicorn      # confirm the name: 'gunicorn' vs 'python-gunicorn'
sudo pacman -S python python-flask python-openpyxl gunicorn \
               sqlite rsync logrotate less curl
```

`base` ships neither Python nor `less` nor `curl` on a minimal install. Confirm
everything the later steps call:

```bash
for c in less curl sqlite3 ss rsync logrotate gunicorn python3 ollama git; do
  command -v $c >/dev/null && echo "ok   $c" || echo "MISS $c"
done
```

If `/usr/bin/gunicorn` doesn't exist but `which gunicorn` finds it elsewhere,
that path goes into `jobfilter-web.service`.

### Ollama

```bash
lspci | grep -Ei 'vga|3d|display'      # decides CPU vs GPU package, and the model
sudo pacman -S ollama                  # or: ollama-cuda / ollama-rocm
sudo systemctl enable --now ollama
ollama --version                       # must be >= 0.22 for Gemma 4
```

The Arch unit is `ollama.service`, matching the `After=` / `Wants=` already in
`jobfilter.service`. It runs as its own `ollama` user with
`OLLAMA_MODELS=/var/lib/ollama` and no `OLLAMA_HOST` override, so it listens on
`127.0.0.1:11434` — which is what `config/config.json`'s `ollama_url` expects.
Nothing to change.

**Bound it by priority, not by a hard cap:**

```bash
sudo systemctl edit ollama
```
```ini
[Service]
CPUWeight=50
```
```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama -p CPUWeight     # confirm it saved
```

`CPUWeight` only matters under contention: an idle box lets Ollama use all six
cores, and the other services take priority when they want CPU. A `CPUQuota`
ceiling would instead waste capacity during the twice-daily bursts, which is the
one time you want this to run flat out. Add `CPUQuota=500%` alongside it only if
full-tilt inference makes the box unpleasant to use.

## 3. Switch the model

**Target: `gemma4:26b`.** Substitute another tag below if you decide otherwise —
see [Choosing the model](#choosing-the-model) for why this one.

```bash
ollama pull gemma4:26b
ollama run gemma4:26b "Say hello in five words."
```

Three file edits:

**`config/config.json`**
```json
"model": "gemma4:26b",
"num_ctx": 8192,
"timeout_seconds": 600,
```

**`scripts/filter_jobs.py`** — Gemma 4 is thinking-capable, and `filter_jobs.py`
uses Ollama structured outputs (`"format": RESULT_SCHEMA`). Reasoning blocks
fight grammar-constrained decoding and multiply latency, and three slow jobs in
a row abort the whole filter phase. Disable it in the payload at ~line 294:

```python
"stream": False,
"think": False,          # ← add
"format": RESULT_SCHEMA,
```

Also update the fallback default on the next line, which only fires if the
config key goes missing but should still agree:

```python
"model": config.get("model", "gemma4:26b"),
```

**Verify before a full cycle.** Watch the first few jobs; if each takes minutes
or the output carries `<think>` content, thinking is still on:

```bash
python3 scripts/filter_jobs.py data/scraped_jobs.json --all --csv /tmp/probe.csv
```

`SECONDS_PER_JOB` in `scripts/pipeline_stats.py` also needs updating, but it
takes a real measurement — [step 8](#8-verify-and-measure).

## 4. Strip the Pi's CPU quotas

`jobfilter.service` carries `CPUQuota=300%` and `jobfilter-web.service`
`CPUQuota=50%`. Both were sized for the Pi's 4 cores and its hardware watchdog,
and **neither does what its comment claims here.**

`jobfilter.service` runs `orchestrator.py`, which spawns `filter_jobs.py`, which
makes HTTP requests and parses JSON — near-zero CPU. All inference load is in
`ollama.service`, a separate unit outside that quota, already handled in step 2.
The 50% on the web app is worse than useless: it throttles gunicorn to half a
core on a six-core box for no reason.

Delete both lines. The watchdog that motivated them is Pi hardware:

```bash
systemctl show -p RuntimeWatchdogUSec       # "0" = not configured
wdctl 2>/dev/null || echo "no hardware watchdog"
```

## 5. Generalize Pi-specific text

Comments that will mislead on the new host:

- `jobfilter.service:4` — the RTC-battery rationale for `time-sync.target`
- `jobfilter.logrotate:2` — "a disk and SD-wear concern on the Pi"
- `scripts/orchestrator.py:61-62, 158, 178, 583` — "the Pi's timezone", "the Pi was off"
- `scripts/filter_jobs.py:3` — "Raspberry Pi 5 + gemma3:4b"
- `scripts/newgrad_watch.py:6` — "(gemma3:4b, one request per job)"
- `scripts/pipeline_stats.py:74` — "Measured gemma3:4b scoring rate on the Pi 5"
- `scripts/db.py:8`, `scripts/recruitment_watch.py:7`

**One of these is not cosmetic.** `scripts/seed_dev_db.py` warns *"Never run this
on the Pi"* at lines 31, 240, and 252. That guard is about to name a machine
that is no longer the one it protects — reword it to "the production host", or
it reads as inapplicable on archlab, which is exactly backwards.

`scripts/bootstrap_from_workbook.py` also mentions the Pi, but as history ("the
moment the Pi takes over as system of record"). That's accurate; leave it.

## 6. Install and start the units

```bash
cd ~/job_filter
grep -n "bluke\|WorkingDirectory" *.service *.logrotate   # confirm, don't edit

sudo cp jobfilter.service jobfilter-web.service \
        jobfilter-backup.{service,timer} jobfilter-newgrad.{service,timer} \
        /etc/systemd/system/
sudo cp jobfilter.logrotate /etc/logrotate.d/jobfilter
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/jobfilter{,-web,-backup,-newgrad}.service
```

`jobfilter-newgrad.service` uses `WorkingDirectory=.../job_filter/scripts`,
deeper than the other three. Deliberate — don't "fix" it.
`jobfilter-web.service` has no path in its `ExecStart` at all
(`gunicorn ... web.app:app`), but depends on `WorkingDirectory` because that
module path resolves relative to CWD.

**Arch does not run logrotate on its own.** Debian installs a cron job; Arch
ships a timer and, per its policy of never auto-enabling anything, leaves it
off. Without this your `/etc/logrotate.d/jobfilter` is inert and `filter.log`
grows unbounded — a silent failure you'd notice months later:

```bash
sudo systemctl enable --now logrotate.timer
sudo logrotate -d /etc/logrotate.d/jobfilter     # -d = parse only, rotates nothing
```

That dry run is worth doing. logrotate refuses any log whose parent directory is
group- or world-writable unless told which user to become, and `data/` arrives
from the rsync as 775 — so rotation silently never happens. `jobfilter.logrotate`
now carries `su bluke bluke` for this, and tightening the directory is worth
doing regardless:

```bash
chmod g-w data       # 775 -> 755
```

A working run prints `switching euid from 0 to 1000` and no `error:` line.

Smoke-test before enabling anything, since Python 3.10 → Arch's current is a
real jump even for stdlib-only code:

```bash
python3 scripts/newgrad_watch.py --dry-run                        # prints, doesn't mark seen
python3 scripts/filter_jobs.py data/scraped_jobs.json --dry-run --all
```

The second one skips the LLM entirely, writes to `data/dry_run_results.csv`, and
never marks jobs seen — it exercises config loading, the gate regexes, and CSV
writing without touching Ollama or the tracker DB. `data/scraped_jobs.json` is
gitignored runtime output that came across in the rsync; if it's absent, use any
prior scrape or run the scrape phase once to produce one.

Then bring them up one at a time, pipeline last:

```bash
sudo systemctl enable --now jobfilter-web
curl -sI http://127.0.0.1:8000/ | head -1        # expect 200 or 302
sudo systemctl enable --now jobfilter-backup.timer jobfilter-newgrad.timer
sudo systemctl enable --now jobfilter
journalctl -u jobfilter -f
```

That `curl` runs before any Tailscale work on purpose: it separates "gunicorn is
broken" from "the proxy is misconfigured", which look identical in a browser.

## 7. Publish at /jobfilter

Check the local port first, since this box has other tenants:

```bash
ss -ltnp | grep 8000       # empty = free
```

If taken, change `--bind 127.0.0.1:8000` in `jobfilter-web.service`.

**archlab does not use `tailscale serve`.** Unlike the Pi, this box already
fronts Grafana and Prometheus behind a reverse proxy on :443, holding a
`tailscale cert` for `archlab.taileeb244.ts.net`. `jobfilter` is one more path
alongside them, so `tailscale serve status` correctly reports `No serve config`
— that is not a fault. The Pi's equivalent, kept for the fallback case:

```bash
sudo tailscale serve --bg --https=443 --set-path=/jobfilter localhost:8000
```

**The path stays `/jobfilter`** either way, so the hostname is the only thing
that changes between the two installs — one word apart in the URL bar during the
overlap, when you may be switching between them.

`web/app.py:55` sets `ProxyFix(app.wsgi_app, x_prefix=1)`: the proxy strips
`/jobfilter` before forwarding, and ProxyFix reads `X-Forwarded-Prefix` to put it
back on every `url_for()`, static assets included. Nothing in the repo hardcodes
the hostname or the path.

**That header is not automatic behind a general reverse proxy.** `tailscale
serve` sends it; nginx does not unless told to:

```nginx
proxy_set_header X-Forwarded-Prefix /jobfilter;
```

Verify rather than assume — the landing page renders fine while every generated
link is broken, so test the links, not the page:

```bash
curl -s https://archlab.taileeb244.ts.net/jobfilter/ | grep -o 'href="[^"]*"' | head
```

`href="/jobfilter/job/12"` is correct. `href="/job/12"` means the header is not
arriving and saving a job will land somewhere wrong.

**Check what the proxy is bound to.** `tailscale serve` listens only on the
Tailscale interface; a general reverse proxy usually binds `0.0.0.0:443`, which
puts a tracker with no login on the LAN as well as the tailnet:

```bash
sudo ss -ltnp | grep ':443'
```

A `100.x` address is tailnet-only. `0.0.0.0:*` means anyone on the home network
can read and edit the tracker — bind the proxy to the tailnet IP, or firewall
443 to the `tailscale0` interface.
## 8. Verify and measure

Let one real cycle run, then check the scoring rate against the ETA constant:

```bash
grep -c . data/seen_jobs.txt          # before and after a filter phase
journalctl -u jobfilter --since "1 hour ago" | tail -50
```

Divide elapsed filter-phase time by jobs scored and put the result in
`scripts/pipeline_stats.py:75`:

```python
# Measured gemma4:26b scoring rate on archlab, used for the ETA.
SECONDS_PER_JOB = <measured>
```

Until this is right the web dashboard's ETA is quoting Pi-era `gemma3:4b`
numbers and will be wrong in both directions.

## 9. Rescore the backlog

`data/seen_jobs.txt` came across in the rsync, and `filter_jobs.py:447` skips
everything in it. **The new model will only ever see new postings** — your whole
existing backlog stays scored at `gemma3:4b` quality until you force it:

```bash
python3 scripts/filter_jobs.py data/scraped_jobs.json --rescore
python3 scripts/store_matches.py
```

This is safe. `store_matches` upserts `ON CONFLICT(key) DO NOTHING` and archived
rows persist as tombstones, so a rescore can **only add** jobs the smaller model
wrongly rejected — it cannot overwrite hand-edited columns or resurrect anything
deleted. Budget the time: full history at the new sec/job rate. Run it by hand,
not into a scheduled cycle.

---

## Choosing the model

32 GB lets a large model **load**. It does not make it **fast**. CPU inference
is memory-bandwidth-bound — the engine streams the weights it needs once per
generated token. The Pi 5's LPDDR4X does ~17 GB/s; desktop dual-channel
DDR4/DDR5 does 45-50 GB/s.

Scaled from the measured Pi baseline (`SECONDS_PER_JOB = 75` for `gemma3:4b`):

| Tag | Architecture | Q4 size | Est. sec/job, CPU-only |
|---|---|---|---|
| `gemma3:4b` | dense | 3.3 GB | ~25s |
| `gemma4:e4b` | dense, ~4B effective | 9.6 GB | ~45s |
| `gemma4:12b` | dense | ~8 GB | ~60s |
| **`gemma4:26b`** | **MoE, ~4B active/token** | **~18 GB** | **~40-60s** |
| `gemma3:27b` | dense | ~17 GB | ~125s |
| `gemma4:31b` | dense | ~20 GB | ~140s |

**`gemma4:26b` is the pick.** It is mixture-of-experts: all 26B parameters sit in
RAM, but only ~4B activate per token. On a 3090 it benches 64-128 tok/s against
the 31B dense model's 30-34 — and that gap is *wider* on CPU, because per-token
bandwidth cost tracks the active slice, not the resident size. You get 26B-class
scoring for roughly 2× your current per-job cost instead of 5×.

Dense `gemma3:27b` streams ~17 GB per token and lands slower per job than the Pi
is today. It is the wrong shape for this box unless `lspci` found a real GPU.

18 GB resident of 32 GB is comfortable, and Ollama's default 5-minute keep-alive
releases it between the twice-daily cycles rather than holding it permanently.

Two constraints come from the code itself:

**Structured output.** `filter_jobs.py:301` sends `"format": RESULT_SCHEMA`.
Thinking-capable models need `"think": False` (step 3); `deepseek-r1` and
similar always-reasoning models can't be used at all.

**The rubric is Python, not prompt.** `score_from_classification` computes the
score from the model's *labels*; the model only classifies. Staying in the Gemma
family keeps those labels calibrated against a rubric tuned on `gemma3:4b`. A
cross-family jump changes behavior more than the parameter count suggests —
which is the other reason `gemma4:26b` beats an equivalent Qwen or Mistral MoE.

### Context window matters more than parameter count

`build_user_prompt` (`filter_jobs.py:285`) passes the **full** job description
with no truncation, into `num_ctx: 4096`. Long postings overflow and Ollama
silently drops tokens — sometimes including the system prompt carrying the
rubric. That 4096 was an 8 GB-Pi compromise; 8192 costs little here and may
improve scoring more than the model swap does.

Raise `timeout_seconds` alongside the model. A slow outlier that exceeds it
counts as a failure, and three consecutive failures abort the filter phase.

---

## Running both

The Pi stays up until archlab proves reliable, then its units are disabled. The
two installs **fork** from the moment of the rsync — each independently decides
what's eligible and scores it with its own model. They are not competing copies
of one dataset.

**archlab is the system of record from day one.** Do all application tracking in
its web app. Anything typed into the Pi's site after the copy is lost when the
Pi is retired — there is no re-sync back. Retire the Pi bookmark as soon as
archlab is serving; the two sites look nearly identical, and that is the only
real defense against typing notes into the copy that gets discarded.

Known overlap effects, all accepted deliberately:

- **Duplicate new-grad emails**, one per host every 15 minutes. Accepted until
  archlab is trusted; then set `notify.enabled: false` in the Pi's
  `config/local.json`, or stop its timer.
- **2× request volume** against the same Greenhouse / Workday / Oracle endpoints
  at the same two times of day.
- **Snapshots are archlab-only** going forward; the Pi's `data/backups/` is
  frozen. If both ever ran `backup_db.py --push` to the same `remote_dir`, the
  `tracker-YYYYMMDD.db` names would clobber each other daily.

`orchestrator.lock` is a `flock`, so single-instance protection is per-machine.
No cross-host coordination exists or is needed.

### Retiring the Pi

```bash
# On the Pi, once archlab has been reliable for a cycle or two
sudo systemctl disable --now jobfilter jobfilter-web \
     jobfilter-backup.timer jobfilter-newgrad.timer
```

Leave the tree in place as the recovery image. To fall back, reverse step 0's
rsync — accepting that the Pi's DB has diverged and lacks everything archlab
recorded since the split.
