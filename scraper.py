#!/usr/bin/env python3
"""
Job scraper for the job_filter pipeline. Stdlib only, Python 3.9+.

Pulls listings from machine-readable sources (no fragile HTML scraping):
  - RemoteOK public JSON API
  - We Work Remotely RSS feeds

Applies cheap keyword pre-filtering (so the slow LLM step only sees
plausible candidates), then writes scraped_jobs.json in the exact format
filter_jobs.py expects.

Usage:
    python3 scraper.py                      # uses scraper_config.json
    python3 scraper.py --out myjobs.json
    python3 scraper.py --no-prefilter       # keep everything, let the LLM judge
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "scraper_config.json"

# Reuse the filter's fingerprint + seen-list so "seen" means "already
# evaluated by the LLM", not merely "already scraped". A job that gets
# scraped but never filtered keeps reappearing until it's processed.
from filter_jobs import job_fingerprint, load_seen  # noqa: E402

USER_AGENT = "JobFilterBot/1.0 (personal job search; contact: see config)"
MAX_DESC_CHARS = 4000  # keep descriptions within the LLM's context budget

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(text):
    """Crude but dependency-free HTML -> text."""
    text = html.unescape(text or "")
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.I)
    text = TAG_RE.sub(" ", text)
    lines = [WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:MAX_DESC_CHARS]


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- sources

def scrape_remoteok(source_cfg):
    """RemoteOK public API: https://remoteok.com/api (first element is metadata)."""
    raw = fetch(source_cfg.get("url", "https://remoteok.com/api"))
    data = json.loads(raw)
    jobs = []
    for item in data:
        if not isinstance(item, dict) or "position" not in item:
            continue  # skips the legal/metadata header element
        salary = ""
        lo, hi = item.get("salary_min"), item.get("salary_max")
        if lo and hi:
            salary = f"${lo:,} - ${hi:,}"
        tags = ", ".join(item.get("tags", []))
        desc = strip_html(item.get("description", ""))
        if tags:
            desc = f"TAGS: {tags}\n{desc}"
        jobs.append({
            "title": item.get("position", ""),
            "company": item.get("company", ""),
            "location": item.get("location") or "Remote",
            "salary": salary,
            "url": item.get("url", ""),
            "description": desc,
            "source": "remoteok",
        })
    return jobs


def scrape_wwr_rss(source_cfg):
    """We Work Remotely RSS feed (one feed per category)."""
    raw = fetch(source_cfg["url"])
    root = ET.fromstring(raw)
    jobs = []
    for item in root.iter("item"):
        title_raw = (item.findtext("title") or "").strip()
        # WWR titles look like "Company Name: Job Title"
        company, _, title = title_raw.partition(":")
        if not title:
            title, company = title_raw, ""
        region = (item.findtext("region") or "").strip()
        jobs.append({
            "title": title.strip(),
            "company": company.strip(),
            "location": region or "Remote",
            "salary": "",
            "url": (item.findtext("link") or "").strip(),
            "description": strip_html(item.findtext("description") or ""),
            "source": "weworkremotely",
        })
    return jobs


def scrape_remotive(source_cfg):
    """Remotive public API: https://remotive.com/api/remote-jobs
    Their terms ask for low request volume — fine for a nightly cron."""
    raw = fetch(source_cfg.get("url", "https://remotive.com/api/remote-jobs"))
    data = json.loads(raw)
    jobs = []
    for item in data.get("jobs", []):
        tags = ", ".join(item.get("tags", []))
        desc = strip_html(item.get("description", ""))
        if tags:
            desc = f"TAGS: {tags}\n{desc}"
        jobs.append({
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "location": item.get("candidate_required_location") or "Remote",
            "salary": item.get("salary", ""),
            "url": item.get("url", ""),
            "description": desc,
            "source": "remotive",
        })
    return jobs


def scrape_arbeitnow(source_cfg):
    """Arbeitnow public API (paginated). Listings skew Europe/Germany."""
    base = source_cfg.get("url", "https://www.arbeitnow.com/api/job-board-api")
    pages = source_cfg.get("pages", 2)
    jobs = []
    for page in range(1, pages + 1):
        raw = fetch(f"{base}?page={page}")
        data = json.loads(raw)
        for item in data.get("data", []):
            extras = ", ".join(item.get("tags", []) + item.get("job_types", []))
            desc = strip_html(item.get("description", ""))
            if extras:
                desc = f"TAGS: {extras}\n{desc}"
            loc = item.get("location", "")
            if item.get("remote"):
                loc = f"{loc} (Remote)" if loc else "Remote"
            jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "location": loc,
                "salary": "",
                "url": item.get("url", ""),
                "description": desc,
                "source": "arbeitnow",
            })
        if not data.get("links", {}).get("next"):
            break
        time.sleep(1)
    return jobs


def scrape_hn_hiring(source_cfg):
    """Latest monthly 'Ask HN: Who is hiring?' thread via the Algolia API.
    One request finds the thread, one fetches every comment in it."""
    search_url = ("https://hn.algolia.com/api/v1/search_by_date"
                  "?tags=story,author_whoishiring&query=who%20is%20hiring")
    hits = json.loads(fetch(search_url)).get("hits", [])
    thread = next((h for h in hits
                   if "who is hiring" in (h.get("title") or "").lower()), None)
    if thread is None:
        raise ValueError("Could not locate a 'Who is hiring?' thread")
    story_id = thread.get("story_id") or thread.get("objectID")
    item = json.loads(fetch(f"https://hn.algolia.com/api/v1/items/{story_id}"))

    jobs = []
    for c in item.get("children", []):
        text = strip_html(c.get("text") or "")
        if not text or len(text) < 40:
            continue  # deleted/empty/noise comments
        lines = text.splitlines()
        first = lines[0]
        # Convention: "Company | Role | Location | extras..."
        parts = [p.strip() for p in first.split("|")]
        if len(parts) >= 2:
            company, title = parts[0], parts[1]
            location = parts[2] if len(parts) > 2 else ""
        else:
            company, title, location = "", first[:120], ""
        jobs.append({
            "title": title[:150],
            "company": company[:100],
            "location": location[:100],
            "salary": "",
            "url": f"https://news.ycombinator.com/item?id={c.get('id','')}",
            "description": text,
            "source": "hn_hiring",
        })
    return jobs


SCRAPERS = {
    "remoteok": scrape_remoteok,
    "wwr_rss": scrape_wwr_rss,
    "remotive": scrape_remotive,
    "arbeitnow": scrape_arbeitnow,
    "hn_hiring": scrape_hn_hiring,
}

# ---------------------------------------------------------------- pipeline

def _compile_keywords(keywords):
    """Whole-word/phrase regexes, so 'AI' doesn't match 'maintain'
    and 'ML' doesn't match 'html'. Phrases match across whitespace."""
    patterns = []
    for kw in keywords:
        escaped = r"\s+".join(re.escape(part) for part in kw.lower().split())
        patterns.append(re.compile(r"(?<!\w)" + escaped + r"(?!\w)"))
    return patterns


def keyword_prefilter(jobs, cfg):
    """Cheap text filter so the LLM only sees plausible listings."""
    include = _compile_keywords(cfg.get("include_keywords", []))
    exclude = _compile_keywords(cfg.get("exclude_keywords", []))
    kept = []
    for job in jobs:
        text = f"{job['title']} {job['description']}".lower()
        if exclude and any(p.search(text) for p in exclude):
            continue
        if include and not any(p.search(text) for p in include):
            continue
        kept.append(job)
    return kept


def dedupe(jobs):
    seen, out = set(), []
    for job in jobs:
        key = (job.get("url") or f"{job['title']}|{job['company']}").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def main():
    parser = argparse.ArgumentParser(description="Scrape job listings to JSON.")
    parser.add_argument("--out", default=str(SCRIPT_DIR / "scraped_jobs.json"))
    parser.add_argument("--no-prefilter", action="store_true",
                        help="Skip keyword filtering; pass everything to the LLM")
    parser.add_argument("--include-seen", action="store_true",
                        help="Also emit jobs the filter has already evaluated")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH} — create it (see README).")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    all_jobs = []
    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            continue
        kind = source.get("type")
        scraper = SCRAPERS.get(kind)
        if scraper is None:
            print(f"  ! unknown source type '{kind}', skipping")
            continue
        name = source.get("name", kind)
        print(f"Fetching {name} ... ", end="", flush=True)
        try:
            jobs = scraper(source)
            print(f"{len(jobs)} listings")
            all_jobs.extend(jobs)
        except (urllib.error.URLError, TimeoutError, ET.ParseError,
                json.JSONDecodeError) as e:
            print(f"FAILED ({e}) — continuing with other sources")
        time.sleep(cfg.get("delay_between_sources", 2))  # be polite

    fetched = len(all_jobs)
    all_jobs = dedupe(all_jobs)
    deduped = len(all_jobs)
    if not args.no_prefilter:
        all_jobs = keyword_prefilter(all_jobs, cfg)
    prefiltered = len(all_jobs)

    already_seen = 0
    if not args.include_seen:
        seen = load_seen()
        if seen:
            fresh = [j for j in all_jobs if job_fingerprint(j) not in seen]
            already_seen = len(all_jobs) - len(fresh)
            all_jobs = fresh

    out = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "jobs": all_jobs,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\n{fetched} fetched -> {deduped} after dedupe -> "
          f"{prefiltered} after prefilter -> {len(all_jobs)} new "
          f"({already_seen} already evaluated)")
    print(f"Wrote {args.out}")
    print(f"Next: python3 filter_jobs.py {Path(args.out).name}")


if __name__ == "__main__":
    main()
