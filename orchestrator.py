#!/usr/bin/env python3
"""
Persistent orchestrator for the job-filter pipeline. Run as a systemd service.

Pipeline (fires on SCRAPE_HOURS_LOCAL schedule, twice daily):
  1. detect   — auto-detect ATS platforms for new companies.txt entries
  2. verify   — probe each watchlist company's ATS for live job count
  3. scrape   — scraper.py: public sources + verified watchlist, single pass
  4. filter   — filter_jobs.py scores jobs via local Ollama LLM
  5. copy     — scp matched_jobs.csv to laptop over Tailscale (retries if offline)

Phase is written to orchestrator_state.json before every transition, so a
crash or systemd restart resumes from the last checkpoint automatically.

Stop cleanly:   systemctl stop jobfilter   (SIGTERM → exits with code 0, no restart)
Manual run:     python3 orchestrator.py
"""

import json
import os
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

# ── settings — edit before deploying ──────────────────────────────────────────

REMOTE_HOST = "100.107.150.87"                      # Tailscale IP of your laptop
REMOTE_USER = "lbrug"                               # SSH user on your laptop
REMOTE_DIR  = "C:/Users/lbrug/job_data"             # Destination path

# Local-time hours to fire the pipeline. Uses the Pi's system timezone, so set
# the Pi to America/New_York (`sudo timedatectl set-timezone America/New_York`)
# and DST is handled automatically — 6 and 13 always mean 6 AM and 1 PM Eastern.
SCRAPE_HOURS_LOCAL   = [6, 13]   # 6 AM and 1 PM local
COPY_RETRY_INTERVAL  = 900       # seconds between copy retries while laptop is offline
DETECT_DELAY         = 0.5       # seconds between ATS probes when detecting new companies

# ── paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR     = Path(__file__).resolve().parent
STATE_PATH     = SCRIPT_DIR / "orchestrator_state.json"
LOCK_PATH      = SCRIPT_DIR / "orchestrator.lock"
SCRAPER_CFG    = SCRIPT_DIR / "scraper_config.json"
COMPANIES_TXT  = SCRIPT_DIR / "companies.txt"
MISSES_TXT     = SCRIPT_DIR / "watchlist_misses.txt"
SCRAPED_JOBS   = SCRIPT_DIR / "scraped_jobs.json"
CSV_PATH       = SCRIPT_DIR / "matched_jobs.csv"
FOUND_JSON  = SCRIPT_DIR / "watchlist_found.json"

PYTHON = sys.executable   # same interpreter this script was launched with

# Pipeline phase order — state is saved at the start of each phase so a
# restart can skip phases that already finished.
PHASES = ["detect", "verify", "scrape", "filter", "copy"]
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
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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


def _live_job_count(platform, slug):
    """Return number of open jobs for a watchlist company, or -1 on error."""
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

    names = load_names(str(COMPANIES_TXT), None)
    known = {(c.get("label") or c.get("name") or c.get("slug") or "").strip().lower() for c in watchlist_companies()}
    if FOUND_JSON.exists():
        found_data = json.loads(FOUND_JSON.read_text(encoding="utf-8"))
        known |= {(c.get("label") or c.get("name") or c.get("slug") or "").strip().lower() for c in found_data}
    attempted = {n.strip().lower() for n in state.get("detect_attempted", [])}
    new_names = [n for n in names
                 if n.strip().lower() not in known
                 and n.strip().lower() not in attempted]

    if not new_names:
        log("No new companies in companies.txt.")
        return

    recorded_misses = set()
    if MISSES_TXT.exists():
        recorded_misses = {ln.strip().lower()
                           for ln in MISSES_TXT.read_text(encoding="utf-8").splitlines()
                           if ln.strip()}

    log(f"Detecting platforms for {len(new_names)} new compan(y/ies)...")
    for name in new_names:
        if _shutdown:
            log("Shutdown requested mid-detection — progress saved, will resume.")
            return
        hit = detect_platform(name, DETECT_DELAY)
        if hit:
            added = add_to_watchlist(hit)
            log(f"  + {name}: {hit['platform']} ({hit['slug']})"
                + ("" if added else " [already present]"))
        else:
            log(f"  - {name}: no ATS found — add a slug manually (see watchlist_misses.txt)")
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
    """Write cfg_dict to a temp file in SCRIPT_DIR and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json", dir=SCRIPT_DIR)
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

# ── copy CSV ───────────────────────────────────────────────────────────────────

def copy_csv(state):
    """SCP the CSV to the laptop. Returns True on success, False if unreachable."""
    if not CSV_PATH.exists():
        log("No CSV to copy yet.")
        return True

    if not laptop_online():
        log(f"Laptop ({REMOTE_HOST}) not reachable on Tailscale — "
            f"will retry in {COPY_RETRY_INTERVAL // 60} min.")
        state["copy_pending"] = True
        state["last_copy_attempt"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return False

    dest = f"{REMOTE_USER}@{REMOTE_HOST}:{REMOTE_DIR}"
    log(f"Copying {CSV_PATH.name} → {dest}")
    result = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
         str(CSV_PATH), dest]
    )
    if result.returncode == 0:
        log("CSV copied successfully.")
        state["copy_pending"] = False
        state["last_copy_attempt"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return True

    log("SCP failed — will retry later.")
    state["copy_pending"] = True
    state["last_copy_attempt"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
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
                run_step([PYTHON, SCRIPT_DIR / "scraper.py",
                          "--config", tmp, "--out", str(SCRAPED_JOBS)])
            finally:
                Path(tmp).unlink(missing_ok=True)

        # ── filter ────────────────────────────────────────────────────────────
        elif phase == "filter":
            log(f"=== {tag}: Filter with LLM ===")
            run_step([PYTHON, SCRIPT_DIR / "filter_jobs.py", str(SCRAPED_JOBS),
                      "--csv", str(CSV_PATH)])

        # ── copy ──────────────────────────────────────────────────────────────
        elif phase == "copy":
            log(f"=== {tag}: Copy CSV to laptop ===")
            copy_csv(state)

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
                copy_csv(state)

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
