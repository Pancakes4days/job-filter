#!/usr/bin/env python3
"""
Detect when watchlist companies open new-grad / internship hiring cycles.

Called by the orchestrator after each scrape. Writes data/recruitment_alerts.json
with alerts that persist for TTL_DAYS so pipeline_stats.py can surface them
prominently even if you don't check the Pi for a day or two.

Only watchlist companies are monitored — public job boards (RemoteOK, WWR, etc.)
always have some internship posts and would generate constant noise.
"""

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from paths import CONFIG_DIR, DATA_DIR

ALERTS_PATH = DATA_DIR / "recruitment_alerts.json"
SCRAPER_CFG = CONFIG_DIR / "scraper_config.json"

TTL_DAYS = 4   # how long an alert stays visible in pipeline_stats.py

# Title patterns that signal new-grad / internship hiring has opened.
# Matched against the raw job title — fast and reliable enough for this purpose.
NEWGRAD_RE = re.compile(
    r"intern(ship)?\b"
    r"|new[\s\-]?grad(uate)?\b"
    r"|entry[\s\-]level\b"
    r"|\b2027\b"
    r"|summer\s+2027"
    r"|university\s+(recruit|hiring|grad)"
    r"|graduate\s+(program|engineer|rotational)\b"
    r"|co[\s\-]?op\b"
    r"|campus\s+(recruit|hire|hiring)"
    r"|early[\s\-]career",
    re.I,
)


def _watchlist_labels():
    """Lowercase company labels from the watchlist source in scraper_config.json."""
    if not SCRAPER_CFG.exists():
        return set()
    cfg = json.loads(SCRAPER_CFG.read_text(encoding="utf-8"))
    wl = next((s for s in cfg.get("sources", []) if s.get("type") == "watchlist"), None)
    return {c.get("label", "").strip().lower() for c in (wl or {}).get("companies", [])}


def _load_alerts():
    if not ALERTS_PATH.exists():
        return []
    try:
        return json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def check_recruitment_pulse(scraped_jobs_path):
    """Scan the latest scrape for watchlist companies newly posting new-grad/intern roles.

    Returns a list of newly-created alert dicts (empty if nothing changed).
    Writes/updates data/recruitment_alerts.json as a side effect.
    """
    if not scraped_jobs_path.exists():
        return []

    scraped = json.loads(scraped_jobs_path.read_text(encoding="utf-8")).get("jobs", [])
    watchlist = _watchlist_labels()

    # Collect matching titles by company, watchlist sources only.
    # Watchlist jobs have source like "greenhouse:stripe"; public sources don't contain ":".
    by_company = defaultdict(list)
    for job in scraped:
        src     = job.get("source", "")
        company = job.get("company", "")
        title   = job.get("title", "")
        is_watchlist = ":" in src or company.lower() in watchlist
        if is_watchlist and NEWGRAD_RE.search(title):
            by_company[company].append(title)

    today  = date.today()
    alerts = _load_alerts()

    # Prune expired alerts
    alerts = [a for a in alerts if date.fromisoformat(a["expires"]) >= today]
    active  = {a["company"].lower() for a in alerts}

    new_alerts = []
    for company, titles in sorted(by_company.items()):
        if company.lower() in active:
            continue
        alert = {
            "company":      company,
            "first_seen":   today.isoformat(),
            "expires":      (today + timedelta(days=TTL_DAYS)).isoformat(),
            "count":        len(titles),
            "sample_roles": titles[:4],
        }
        alerts.append(alert)
        new_alerts.append(alert)
        active.add(company.lower())

    ALERTS_PATH.write_text(json.dumps(alerts, indent=2, ensure_ascii=False), encoding="utf-8")
    return new_alerts


def load_active_alerts():
    """Return non-expired alerts sorted newest-first. Used by pipeline_stats.py."""
    today  = date.today()
    alerts = _load_alerts()
    active = [a for a in alerts if date.fromisoformat(a["expires"]) >= today]
    return sorted(active, key=lambda a: a["first_seen"], reverse=True)
