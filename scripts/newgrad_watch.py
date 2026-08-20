#!/usr/bin/env python3
"""
newgrad_watch.py — low-latency lane for competitive new-grad postings.

The main pipeline runs twice a day and spends most of that time in the LLM
filter (gemma3:4b, one request per job). That is fine for ordinary listings and
useless for a new-grad req where applying first is most of the advantage: a
posting that goes live at 14:00 would not reach you until 06:00 the next day.

So this is the fast path around it. For watchlist companies flagged
"tier1": true it polls the ATS directly, matches raw titles against
recruitment_watch.is_new_grad (internships excluded), applies the location filter, and emails
whatever is new. No LLM, no DB write, no full scrape — a couple of seconds per
company, cheap enough to run every 15 minutes from a systemd timer.

Matching RAW titles also sidesteps a coupling in the slow path:
recruitment_watch.py reads the scraper's OUTPUT, so a posting whose title misses
include_keywords (say "Associate Software Engineer, Technology Development
Program") is filtered away before it can ever raise an alert.

Seen postings are remembered in data/newgrad_seen.json by URL, so each one
alerts exactly once. The file is only updated when the email actually goes out —
a send failure means the same posting is retried on the next tick.

    python3 newgrad_watch.py --dry-run   # print the email, touch no state
    python3 newgrad_watch.py --list      # show which companies are tier-1
    python3 newgrad_watch.py --all       # ignore seen-state, show every match
"""

import argparse
import json
import time
import urllib.error
from datetime import datetime, timezone

import scraper
from paths import CONFIG_DIR, DATA_DIR
from recruitment_watch import is_new_grad
import notify

SCRAPER_CFG = CONFIG_DIR / "scraper_config.json"
SEEN_PATH = DATA_DIR / "newgrad_seen.json"

# Workday sorts newest-first (offset 0 is "Posted Today", offset 480 is nine
# days old), so two pages covers everything posted since the last tick with a
# fraction of the requests a full 25-page crawl would make.
FAST_WORKDAY_PAGES = 2

# Postings stay in the seen-file this long. Long enough that a req which sits
# open for weeks never re-alerts, short enough that the file cannot grow without
# bound.
SEEN_TTL_DAYS = 120


def load_config():
    return json.loads(SCRAPER_CFG.read_text(encoding="utf-8"))


def tier1_companies(cfg):
    """Watchlist entries flagged "tier1": true — the apply-first shortlist."""
    watchlist = next((s for s in cfg.get("sources", [])
                      if s.get("type") == "watchlist"), {})
    return [c for c in watchlist.get("companies", []) if c.get("tier1")]


def _load_seen():
    """{url: iso-date} of postings already alerted on, expired entries dropped."""
    if not SEEN_PATH.exists():
        return {}
    try:
        seen = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(seen, dict):
        return {}
    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    kept = {}
    for url, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp).timestamp() >= cutoff:
                kept[url] = stamp
        except (TypeError, ValueError):
            continue   # unparseable stamp: treat as expired
    return kept


def _save_seen(seen):
    SEEN_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")


def fetch_company(entry):
    """Newest postings for one watchlist entry. [] if its board is unreachable."""
    platform, slug = entry.get("platform"), entry.get("slug")
    fetcher = scraper.PLATFORM_FETCHERS.get(platform)
    if not fetcher or not slug:
        print(f"  ! {entry.get('label', slug)}: unknown platform {platform!r}")
        return []
    try:
        jobs = fetcher(slug)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
            ValueError) as e:
        print(f"  ! {entry.get('label', slug)}: fetch failed ({e})")
        return []
    label = entry.get("label", slug)
    for job in jobs:
        job["company"] = label
    return jobs


def find_new_grad_jobs(cfg, companies):
    """Poll each company and return the new-grad postings that pass location."""
    hits = []
    for entry in companies:
        jobs = fetch_company(entry)
        matched = [j for j in jobs if is_new_grad(j.get("title", ""))]
        print(f"  {entry.get('label', entry.get('slug'))}: "
              f"{len(jobs)} polled, {len(matched)} new-grad titles")
        hits.extend(matched)
        time.sleep(1)   # be polite across companies

    # Workday multi-city postings arrive with no location (see scraper.py); the
    # list is tiny by now, so name their cities before filtering on them.
    scraper.resolve_workday_locations(hits)
    return scraper.location_prefilter(hits, cfg)


def format_email(jobs):
    """(subject, body) for a batch of new postings, grouped by company."""
    by_company = {}
    for job in jobs:
        by_company.setdefault(job.get("company", "?"), []).append(job)

    if len(by_company) == 1:
        company = next(iter(by_company))
        plural = "role" if len(jobs) == 1 else "roles"
        subject = f"[job-filter] {company}: {len(jobs)} new-grad {plural}"
    else:
        subject = (f"[job-filter] {len(jobs)} new-grad roles "
                   f"at {len(by_company)} companies")

    lines = []
    for company, group in sorted(by_company.items()):
        lines.append(company.upper())
        for job in group:
            lines.append(f"  {job.get('title', '?')}")
            lines.append(f"    {job.get('location') or 'location not stated'}")
            lines.append(f"    {job.get('url', '')}")
            lines.append("")
        lines.append("")
    lines.append("Apply first, evaluate later — the slow pipeline will score "
                 "these on its next run.")
    return subject, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Poll tier-1 companies for new-grad postings and email them.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the email and leave the seen-file untouched")
    ap.add_argument("--all", action="store_true",
                    help="report every match, not just ones not yet alerted on")
    ap.add_argument("--list", action="store_true",
                    help="list the tier-1 companies and exit")
    args = ap.parse_args()

    cfg = load_config()
    companies = tier1_companies(cfg)

    if args.list:
        if not companies:
            print("No tier-1 companies. Add \"tier1\": true to a watchlist entry "
                  f"in {SCRAPER_CFG.name}.")
        for c in companies:
            print(f"  {c.get('label', '?'):<24} {c.get('platform')}:{c.get('slug')}")
        return

    if not companies:
        print("No tier-1 companies flagged — nothing to poll.")
        return

    # Two pages instead of 25: newest-first ordering means anything posted since
    # the last tick is near the front, and this runs every 15 minutes.
    scraper.WORKDAY_MAX_PAGES = FAST_WORKDAY_PAGES

    print(f"Polling {len(companies)} tier-1 companies ...")
    jobs = find_new_grad_jobs(cfg, companies)

    seen = _load_seen()
    fresh = jobs if args.all else [j for j in jobs if j.get("url") not in seen]
    print(f"\n{len(jobs)} new-grad postings in scope, {len(fresh)} not yet alerted on.")
    if not fresh:
        return

    subject, body = format_email(fresh)
    sent = notify.send(subject, body, dry_run=args.dry_run)

    # Only postings you were actually told about get recorded. Marking them seen
    # after a dry run — including the implicit one when notify is unconfigured —
    # would silently suppress them the day the mailbox starts working.
    unconfigured = notify.dry_run_reason(notify.load_notify_config())
    if args.dry_run or unconfigured:
        print("\n(nothing delivered — seen-file not updated, this repeats next run)")
        if unconfigured and not args.dry_run:
            print(f"To start delivering: {unconfigured}. See notify.py's docstring.")
        return
    if not sent:
        print("\n(send failed — seen-file not updated, will retry next tick)")
        return
    now = datetime.now(timezone.utc).isoformat()
    for job in fresh:
        if job.get("url"):
            seen[job["url"]] = now
    _save_seen(seen)
    print(f"Recorded {len(fresh)} postings in {SEEN_PATH.name}.")


if __name__ == "__main__":
    main()
