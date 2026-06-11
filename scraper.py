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


SCRAPERS = {
    "remoteok": scrape_remoteok,
    "wwr_rss": scrape_wwr_rss,
}

# ---------------------------------------------------------------- pipeline

def keyword_prefilter(jobs, cfg):
    """Cheap text filter so the LLM only sees plausible listings."""
    include = [k.lower() for k in cfg.get("include_keywords", [])]
    exclude = [k.lower() for k in cfg.get("exclude_keywords", [])]
    kept = []
    for job in jobs:
        text = f"{job['title']} {job['description']}".lower()
        if exclude and any(k in text for k in exclude):
            continue
        if include and not any(k in text for k in include):
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

    before = len(all_jobs)
    all_jobs = dedupe(all_jobs)
    if not args.no_prefilter:
        all_jobs = keyword_prefilter(all_jobs, cfg)

    out = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "jobs": all_jobs,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\n{before} fetched -> {len(all_jobs)} after dedupe/prefilter")
    print(f"Wrote {args.out}")
    print(f"Next: python3 filter_jobs.py {Path(args.out).name}")


if __name__ == "__main__":
    main()
