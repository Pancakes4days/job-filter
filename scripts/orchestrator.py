#!/usr/bin/env python3
"""
Persistent orchestrator for the job-filter pipeline. Run as a systemd service.

Pipeline (fires on SCRAPE_HOURS_LOCAL schedule, twice daily):
  1. detect   — auto-detect ATS platforms for new companies.txt entries
  2. verify   — probe each watchlist company's ATS for live job count
  3. scrape   — scraper.py: public sources + verified watchlist, single pass
  4. filter   — filter_jobs.py scores jobs via local Ollama LLM
  5. sync     — pull the laptop's tracker, append new matches (export_workbook.py),
                push the .xlsx + .csv back over Tailscale (retries if offline)

Phase is written to orchestrator_state.json before every transition, so a
crash or systemd restart resumes from the last checkpoint automatically.

Stop cleanly:   systemctl stop jobfilter   (SIGTERM → exits with code 0, no restart)
Manual run:     python3 orchestrator.py
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import fcntl as _fcntl  # Linux/Mac only; unavailable on Windows
except ImportError:
    _fcntl = None

# Reuse the standalone detector's logic for incremental auto-detection of
# companies newly added to companies.txt.
from detect_platforms import detect as detect_platform, load_names  # noqa: E402
from paths import SCRIPTS_DIR, CONFIG_DIR, DATA_DIR  # noqa: E402
from recruitment_watch import check_recruitment_pulse  # noqa: E402

# ── settings ──────────────────────────────────────────────────────────────────
# Deployment-specific settings live in config/local.json (gitignored).
# Copy config/local.example.json → config/local.json and fill in your values.

_local_path = CONFIG_DIR / "local.json"
if not _local_path.exists():
    sys.exit(
        f"Missing {_local_path}\n"
        f"Copy config/local.example.json → config/local.json and fill in your Tailscale details."
    )
_local = json.loads(_local_path.read_text(encoding="utf-8"))

REMOTE_HOST          = _local["remote_host"]        # Tailscale IP of your laptop
REMOTE_USER          = _local["remote_user"]        # SSH user on your laptop
REMOTE_DIR           = _local["remote_dir"]         # job_data folder on your laptop
# Local-time hours to fire the pipeline. Uses the Pi's system timezone, so set
# the Pi to America/New_York (`sudo timedatectl set-timezone America/New_York`)
# and DST is handled automatically — 6 and 13 always mean 6 AM and 1 PM Eastern.
SCRAPE_HOURS_LOCAL   = _local.get("scrape_hours_local",  [6, 13])
COPY_RETRY_INTERVAL  = _local.get("copy_retry_interval", 60)
DETECT_DELAY         = _local.get("detect_delay",        0.5)

# ── paths ─────────────────────────────────────────────────────────────────────

STATE_PATH     = DATA_DIR / "orchestrator_state.json"
LOCK_PATH      = DATA_DIR / "orchestrator.lock"
SCRAPER_CFG    = CONFIG_DIR / "scraper_config.json"
COMPANIES_TXT  = CONFIG_DIR / "companies.txt"
MISSES_TXT     = DATA_DIR / "watchlist_misses.txt"
SCRAPED_JOBS   = DATA_DIR / "scraped_jobs.json"
CSV_PATH       = DATA_DIR / "matched_jobs.csv"
XLSX_PATH      = DATA_DIR / "matched_jobs.xlsx"
FOUND_JSON     = DATA_DIR / "watchlist_found.json"

# Local backup on the Pi: each sync drops a copy of the latest workbook + CSV
# here before pushing to the laptop, so the Pi always retains its own copy.
# Set to None to disable.
LOCAL_COPY_DIR = DATA_DIR / "job_data"

PYTHON = sys.executable   # same interpreter this script was launched with

# Pipeline phase order — state is saved at the start of each phase so a
# restart can skip phases that already finished.
PHASES = ["detect", "verify", "scrape", "filter", "sync"]
N_PHASES = len(PHASES)

# ── graceful shutdown ──────────────────────────────────────────────────────────

_shutdown = False

def _on_signal(sig, frame):
    global _shutdown
    log(f"Signal {sig} — finishing current step then exiting cleanly.")
    _shutdown = True

signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT,  _on_signal)

# ── logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] {msg}", flush=True)

# ── singleton lock (no overlapping runs) ───────────────────────────────────────

_lock_handle = None

def acquire_singleton_lock():
    """Guarantee only one orchestrator runs at a time. systemd already keeps a
    single service instance, but this also blocks an accidental manual
    `python3 orchestrator.py` while the service is live. The OS releases the
    lock automatically when the process exits, so a watchdog-killed instance
    never leaves a stale lock behind. Returns True if the lock was acquired."""
    global _lock_handle
    if _fcntl is None:
        return True  # Windows dev box — nothing to guard against
    _lock_handle = open(LOCK_PATH, "w")
    try:
        _fcntl.flock(_lock_handle, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()
    return True

# ── state ─────────────────────────────────────────────────────────────────────

def _default_state():
    return {
        "phase": "idle",
        "next_run": None,
        "verified_companies": [],
        "detect_attempted": [],   # company names already probed (resumable detect)
        "copy_pending": False,
        "last_copy_attempt": None,
        "workbook_initialized": False,  # have we ever pushed a workbook to the laptop?
    }

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_state()

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

# ── scheduling ─────────────────────────────────────────────────────────────────

def next_run_time():
    """Next scheduled fire time as a naive local datetime (Pi's timezone)."""
    now = datetime.now()  # local time — DST handled by the OS timezone
    candidates = []
    for h in SCRAPE_HOURS_LOCAL:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        candidates.append(t)
    return min(candidates)

# ── subprocess helper ──────────────────────────────────────────────────────────

def run_step(cmd):
    log(f"$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"Subprocess exited with code {result.returncode}")

# ── network ────────────────────────────────────────────────────────────────────

def laptop_online():
    try:
        with socket.create_connection((REMOTE_HOST, 22), timeout=5):
            return True
    except OSError:
        return False

# ── watchlist verification ─────────────────────────────────────────────────────

def _fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "JobFilterBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _workday_job_count(slug):
    """Open-job count for a Workday watchlist entry, or -1 on error.

    slug is "host/site" (e.g. "bitsight.wd1.myworkdayjobs.com/Bitsight"); tenant
    is the host's first label. Workday needs a POST to its cxs jobs endpoint and
    rejects non-browser UAs. A real tenant returns 200 with a "jobPostings" key
    even at 0 jobs (bogus tenants 404/422), so an empty-but-valid board reports 0
    rather than -1 and simply stays in the watchlist for future postings."""
    try:
        s = slug.strip()
        for pre in ("https://", "http://"):
            if s.startswith(pre):
                s = s[len(pre):]
        s = s.strip("/").split("|")[0]
        host, _, rest = s.partition("/")
        site = rest.split("/")[0]
        tenant = host.split(".")[0]
        if not host or not site:
            return -1
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        payload = json.dumps({"appliedFacets": {}, "limit": 1,
                              "offset": 0, "searchText": ""}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; JobFilterBot/1.0)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="replace"))
        return d.get("total", 0) if isinstance(d, dict) and "jobPostings" in d else -1
    except Exception:
        return -1


def _live_job_count(platform, slug):
    """Return number of open jobs for a watchlist company, or -1 on error."""
    if platform == "workday":
        return _workday_job_count(slug)
    routes = {
        "greenhouse":      (f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                            lambda d: len(d.get("jobs", []))),
        "lever":           (f"https://api.lever.co/v0/postings/{slug}?mode=json",
                            lambda d: len(d) if isinstance(d, list) else 0),
        "ashby":           (f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                            lambda d: len(d.get("jobs", []))),
        "smartrecruiters": (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
                            lambda d: d.get("totalFound", 0)),
        "workable":        (f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
                            lambda d: len(d.get("jobs", []))),
        "recruitee":       (f"https://{slug}.recruitee.com/api/offers/",
                            lambda d: len(d.get("offers", []))),
    }
    if platform not in routes:
        return -1
    url, extract = routes[platform]
    try:
        return extract(_fetch_json(url))
    except Exception:
        return -1


def watchlist_companies():
    """Return the company list from the scraper_config.json watchlist source."""
    cfg = json.loads(SCRAPER_CFG.read_text(encoding="utf-8"))
    wl = next((s for s in cfg.get("sources", []) if s.get("type") == "watchlist"), None)
    return wl.get("companies", []) if wl else []


def add_to_watchlist(company):
    """Append a detected company dict to the watchlist source in
    scraper_config.json (creating the source if absent). Dedupes on
    platform+slug. Persists immediately so detection is crash-resumable."""
    cfg = json.loads(SCRAPER_CFG.read_text(encoding="utf-8"))
    wl = next((s for s in cfg.get("sources", []) if s.get("type") == "watchlist"), None)
    if wl is None:
        wl = {"type": "watchlist", "name": "watchlist", "enabled": True, "companies": []}
        cfg.setdefault("sources", []).append(wl)
    existing = {(c.get("platform"), c.get("slug")) for c in wl.get("companies", [])}
    if (company["platform"], company["slug"]) in existing:
        return False
    wl.setdefault("companies", []).append(company)
    SCRAPER_CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def detect_new_companies(state):
    """Incrementally detect ATS platforms for companies newly added to
    companies.txt. Only names not already in the watchlist and not already
    probed (state['detect_attempted']) are checked, so this is cheap on most
    runs and resumes cleanly after a restart. Detected companies are written
    straight into scraper_config.json; misses go to watchlist_misses.txt."""
    if not COMPANIES_TXT.exists():
        log("No companies.txt — skipping auto-detection.")
        return

    # load_names returns [(name, extra_slugs), ...]; extra_slugs come from
    # pipe-separated hints in companies.txt, e.g. "Acme Corp | acme | acmecorp"
    entries = load_names(str(COMPANIES_TXT), None)
    known = {(c.get("label") or c.get("name") or c.get("slug") or "").strip().lower() for c in watchlist_companies()}
    if FOUND_JSON.exists():
        found_data = json.loads(FOUND_JSON.read_text(encoding="utf-8"))
        known |= {(c.get("label") or c.get("name") or c.get("slug") or "").strip().lower() for c in found_data}
    attempted = {n.strip().lower() for n in state.get("detect_attempted", [])}
    # Companies already recorded as misses are skipped — unless they now have
    # explicit slug hints (pipe syntax), which means the user wants a retry.
    recorded_misses = set()
    if MISSES_TXT.exists():
        recorded_misses = {ln.strip().lower()
                           for ln in MISSES_TXT.read_text(encoding="utf-8").splitlines()
                           if ln.strip()}
    new_entries = [(n, slugs) for n, slugs in entries
                   if n.strip().lower() not in known
                   and n.strip().lower() not in attempted
                   and (slugs or n.strip().lower() not in recorded_misses)]

    if not new_entries:
        log("No new companies in companies.txt.")
        return

    log(f"Detecting platforms for {len(new_entries)} new compan(y/ies)...")
    for name, extra_slugs in new_entries:
        if _shutdown:
            log("Shutdown requested mid-detection — progress saved, will resume.")
            return
        hit = detect_platform(name, DETECT_DELAY, extra_slugs)
        if hit:
            added = add_to_watchlist(hit)
            log(f"  + {name}: {hit['platform']} ({hit['slug']})"
                + ("" if added else " [already present]"))
        else:
            log(f"  - {name}: no ATS found — add slug hints after a pipe, e.g.:"
                f" '{name} | {name.lower().replace(' ', '')}' (see watchlist_misses.txt)")
            if name.strip().lower() not in recorded_misses:
                with open(MISSES_TXT, "a", encoding="utf-8") as f:
                    f.write(name + "\n")
                recorded_misses.add(name.strip().lower())
        # Mark attempted + persist after every company so a crash resumes here.
        state.setdefault("detect_attempted", []).append(name)
        save_state(state)


def verify_watchlist():
    """
    Check every company in the scraper_config.json watchlist source.
    Returns a list of company dicts that currently have at least one open job.
    """
    companies = watchlist_companies()
    if not companies:
        log("No watchlist companies configured — skipping verification.")
        return []

    log(f"Verifying {len(companies)} watchlist companies...")
    verified = []
    for c in companies:
        platform = c.get("platform", "")
        slug     = c.get("slug", "")
        label    = c.get("label", slug)
        count    = _live_job_count(platform, slug)
        if count > 0:
            log(f"  {label}: {count} jobs  ({platform}:{slug})")
            verified.append(c)
        elif count == 0:
            log(f"  {label}: 0 openings")
        else:
            log(f"  {label}: unreachable")
        time.sleep(0.5)

    log(f"Verified: {len(verified)} / {len(companies)} companies have open jobs.")
    return verified

# ── temp config builders ───────────────────────────────────────────────────────

def _write_temp_config(cfg_dict):
    """Write cfg_dict to a temp file in DATA_DIR and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json", dir=DATA_DIR)
    os.close(fd)
    Path(path).write_text(json.dumps(cfg_dict, indent=2), encoding="utf-8")
    return path


def make_scrape_config(verified_companies):
    """Temp config for a single scrape: public sources keep their configured
    enabled state; the watchlist source is restricted to the verified companies
    (or disabled if none currently have openings). scraper.py dedupes across all
    sources in one pass, so no separate merge step is needed."""
    cfg = json.loads(SCRAPER_CFG.read_text(encoding="utf-8"))
    for s in cfg.get("sources", []):
        if s.get("type") == "watchlist":
            if verified_companies:
                s["enabled"] = True
                s["companies"] = verified_companies
            else:
                s["enabled"] = False
    return _write_temp_config(cfg)

# ── sync results to the laptop (pull → append → push) ───────────────────────────

def _scp(args):
    """Run scp with the standard non-interactive options."""
    return subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", *args])


def _mark_copy_pending(state):
    state["copy_pending"] = True
    state["last_copy_attempt"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def _save_local_copy():
    """Keep a local backup of the latest workbook + CSV on the Pi."""
    if LOCAL_COPY_DIR is None:
        return
    LOCAL_COPY_DIR.mkdir(parents=True, exist_ok=True)
    for src in (XLSX_PATH, CSV_PATH):
        if src.exists():
            shutil.copy2(src, LOCAL_COPY_DIR / src.name)
    log(f"Saved a local copy of the workbook + CSV to {LOCAL_COPY_DIR}")


def sync_to_laptop(state):
    """Pull the laptop's workbook, append new matches, push it (and the CSV) back.

    The laptop's job_data/matched_jobs.xlsx is the copy you hand-edit, so we pull
    it FIRST and only ever add rows — your Status/Notes survive every cycle.
    matched_jobs.csv is cumulative and the append dedupes by URL, so a cycle
    skipped while the laptop is offline is caught up on the next successful sync.
    Returns True on success, False if it should be retried later."""
    if not CSV_PATH.exists():
        log("No matches yet — nothing to sync.")
        return True

    if not laptop_online():
        log(f"Laptop ({REMOTE_HOST}) not reachable on Tailscale — "
            f"will retry in {COPY_RETRY_INTERVAL // 60} min.")
        _mark_copy_pending(state)
        return False

    remote_dir  = f"{REMOTE_USER}@{REMOTE_HOST}:{REMOTE_DIR}/"
    remote_xlsx = f"{REMOTE_USER}@{REMOTE_HOST}:{REMOTE_DIR}/{XLSX_PATH.name}"

    # 1. Pull the laptop's current workbook so its manual edits are preserved.
    #    Start clean so we never append onto a stale local copy.
    XLSX_PATH.unlink(missing_ok=True)
    log(f"Pulling {XLSX_PATH.name} from laptop to preserve manual edits…")
    pulled = _scp([remote_xlsx, str(XLSX_PATH)]).returncode == 0
    if not pulled:
        if state.get("workbook_initialized"):
            # The workbook should exist on the laptop, but we couldn't read it —
            # most likely it's open in Excel. Do NOT overwrite it with a fresh
            # one; defer and retry so hand-typed columns are never lost.
            log("  couldn't pull the workbook (likely open in Excel) — deferring "
                "so your edits aren't overwritten; will retry.")
            _mark_copy_pending(state)
            return False
        log("  no workbook on the laptop yet — creating the first one.")

    # 2. Append new matches (export_workbook.py is append-only + idempotent).
    run_step([PYTHON, SCRIPTS_DIR / "export_workbook.py",
              "--csv", str(CSV_PATH), "--out", str(XLSX_PATH)])

    # 2b. Keep a local backup on the Pi *before* pushing to the laptop.
    _save_local_copy()

    # 3. Push the workbook + the (rewritten) CSV back into job_data.
    log(f"Pushing {XLSX_PATH.name} + {CSV_PATH.name} → {remote_dir}")
    if _scp([str(XLSX_PATH), str(CSV_PATH), remote_dir]).returncode == 0:
        log("Synced workbook + CSV to the laptop.")
        state["workbook_initialized"] = True
        state["copy_pending"] = False
        state["last_copy_attempt"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return True

    log("Push failed (laptop may have the workbook open) — will retry later.")
    _mark_copy_pending(state)
    return False

# ── pipeline ───────────────────────────────────────────────────────────────────

def run_pipeline(state):
    """
    Execute one full pipeline cycle. Respects state["phase"] so that a restart
    after a crash resumes from the last completed checkpoint.
    """
    start_phase = state.get("phase", PHASES[0])
    if start_phase not in PHASES:
        start_phase = PHASES[0]
    start_idx = PHASES.index(start_phase)

    verified = state.get("verified_companies", [])

    for idx, phase in enumerate(PHASES):
        if idx < start_idx:
            log(f"Skipping phase '{phase}' (already completed before restart).")
            continue

        if _shutdown:
            log("Shutdown requested — stopping pipeline.")
            return

        state["phase"] = phase
        save_state(state)
        tag = f"Phase {idx + 1}/{N_PHASES}"

        # ── detect new companies (incremental) ────────────────────────────────
        if phase == "detect":
            log(f"=== {tag}: Auto-detect new companies.txt entries ===")
            detect_new_companies(state)

        # ── verify watchlist ──────────────────────────────────────────────────
        elif phase == "verify":
            log(f"=== {tag}: Verify watchlist companies ===")
            verified = verify_watchlist()
            state["verified_companies"] = verified
            save_state(state)

        # ── scrape (public sources + verified watchlist, single pass) ─────────
        elif phase == "scrape":
            log(f"=== {tag}: Scrape public sources + {len(verified)} verified companies ===")
            tmp = make_scrape_config(verified)
            try:
                run_step([PYTHON, SCRIPTS_DIR / "scraper.py",
                          "--config", tmp, "--out", str(SCRAPED_JOBS)])
            finally:
                Path(tmp).unlink(missing_ok=True)
            # Detect watchlist companies newly posting new-grad / internship roles
            new_alerts = check_recruitment_pulse(SCRAPED_JOBS)
            for a in new_alerts:
                roles = "; ".join(a["sample_roles"][:2])
                log(f"  [RECRUITMENT ALERT] {a['company']} is posting "
                    f"new-grad/intern roles ({a['count']} found, "
                    f"alert active until {a['expires']}): {roles}")

        # ── filter ────────────────────────────────────────────────────────────
        elif phase == "filter":
            log(f"=== {tag}: Filter with LLM ===")
            run_step([PYTHON, SCRIPTS_DIR / "filter_jobs.py", str(SCRAPED_JOBS),
                      "--csv", str(CSV_PATH)])

        # ── sync (pull tracker → append new matches → push xlsx + csv) ────────
        elif phase == "sync":
            log(f"=== {tag}: Sync tracker to laptop ===")
            sync_to_laptop(state)

    log("=== Pipeline complete ===")
    state["verified_companies"] = []   # clear stale data
    state["detect_attempted"] = []     # re-probe is unnecessary; reset for next cycle

# ── idle loop ──────────────────────────────────────────────────────────────────

def idle_until(state, next_run_dt):
    log(f"Idle — next pipeline run: {next_run_dt.strftime('%Y-%m-%d %H:%M')} local")
    while not _shutdown:
        now_local = datetime.now()            # naive local — for the schedule
        if now_local >= next_run_dt:
            return

        # retry pending copy while waiting for next scheduled run
        if state.get("copy_pending"):
            last = state.get("last_copy_attempt")
            now_utc = datetime.now(timezone.utc)   # aware UTC — matches stored timestamp
            since = (
                (now_utc - datetime.fromisoformat(last)).total_seconds()
                if last else COPY_RETRY_INTERVAL
            )
            if since >= COPY_RETRY_INTERVAL:
                sync_to_laptop(state)

        # sleep in 60-second ticks so SIGTERM is handled promptly
        sleep_for = min(60, max(1, (next_run_dt - now_local).total_seconds()))
        time.sleep(sleep_for)

# ── entry point ────────────────────────────────────────────────────────────────

def main():
    log("Job filter orchestrator starting.")

    # No-overlap guard: refuse to start if another instance holds the lock.
    if not acquire_singleton_lock():
        log("Another orchestrator instance is already running — exiting (no overlap).")
        sys.exit(0)

    # The watchlist is auto-populated from companies.txt by the 'detect' phase of
    # the first cycle. Just flag the state of things at startup.
    if not watchlist_companies():
        if COMPANIES_TXT.exists():
            log("Watchlist empty — will auto-detect from companies.txt on first cycle.")
        else:
            log("WARNING: no watchlist and no companies.txt. Public sources only. "
                "Add company names to companies.txt to build a watchlist automatically.")

    first_launch = not STATE_PATH.exists()
    state = load_state()

    # Resume an interrupted pipeline rather than waiting for the next schedule
    if state.get("phase") in PHASES:
        log(f"Resuming interrupted pipeline from phase: {state['phase']}")
        run_pipeline(state)
        if _shutdown:
            log("Orchestrator stopped.")
            sys.exit(0)
    elif first_launch:
        log("First launch — running an initial cycle now, then settling into the schedule.")
        run_pipeline(state)
        if _shutdown:
            log("Orchestrator stopped.")
            sys.exit(0)

    while not _shutdown:
        nrt = next_run_time()
        state["phase"] = "idle"
        state["next_run"] = nrt.isoformat()
        save_state(state)

        idle_until(state, nrt)

        if _shutdown:
            break

        run_pipeline(state)

    log("Orchestrator stopped.")
    sys.exit(0)   # clean exit → systemd will not restart (Restart=on-failure)


if __name__ == "__main__":
    main()
