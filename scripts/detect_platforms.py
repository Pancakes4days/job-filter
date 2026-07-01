#!/usr/bin/env python3
"""
Detect which ATS platform (Greenhouse / Lever / Ashby) each company uses,
by probing their public job-board APIs. Run this ON THE PI (needs internet).

Input:  a text file with one company name per line (# comments ok), OR a CSV
        with a column containing company names (give the column name).
Output: watchlist_found.json  — paste-ready "companies" array for scraper_config.json
        watchlist_misses.txt  — companies with no API found (their careers page
                                may use Workday/SmartRecruiters/custom — check manually)

Usage:
    python3 detect_platforms.py companies.txt
    python3 detect_platforms.py applications.csv --column Company
    python3 detect_platforms.py companies.txt --delay 1.5
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from paths import DATA_DIR  # noqa: E402

UA = {"User-Agent": "JobFilterBot/1.0 (platform detection for personal job search)"}
# SmartRecruiters' public careers page is served like a normal browser page;
# use a browser-style UA when probing it (the JSON API accepts the bot UA fine).
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (compatible; JobFilterBot/1.0)"}


def slug_variants(name):
    """Plausible board slugs for a company name, most likely first."""
    base = name.strip().lower()
    base = re.sub(r"\b(inc|llc|ltd|corp|co|company|technologies|labs)\.?$", "", base).strip()
    nospace = re.sub(r"[^a-z0-9]", "", base)
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    variants = []
    for v in (nospace, hyphen):
        if v and v not in variants:
            variants.append(v)
    return variants


def _get_json(url, timeout=10):
    """GET url and parse JSON; return None on any network/parse error."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, ValueError):
        return None


def _get_text(url, timeout=10):
    """GET url with a browser UA and return the body text; None on any error."""
    try:
        req = urllib.request.Request(url, headers=BROWSER_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


# Detection tests for ATS *presence*, not open-job count: a valid board answers
# even with zero current openings, so a company that simply isn't hiring right now
# is still detected (and re-scraped later when it posts). A wrong slug is rejected.
#
# For greenhouse/lever/ashby/workable/recruitee a bogus slug 404s (caught as None
# by _get_json), so "the expected JSON shape came back" is a sound presence signal.
#
# SmartRecruiters is the exception and the reason this file was rewritten: its
# public postings endpoint returns 200 {"totalFound": 0, "content": []} for ANY
# slug, real or bogus, so the JSON shape proves nothing (the old probe matched
# every slug and produced false positives). We treat SmartRecruiters as present
# only if it currently has postings (totalFound > 0) OR its public careers page
# renders a real company page: a bogus slug renders the generic "SmartRecruiters
# Job Search" landing page with no og:title, while a real board renders
# "Careers at <Company>" and includes an og:title meta tag.
def check_greenhouse(slug):
    d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return isinstance(d, dict) and "jobs" in d


def check_lever(slug):
    d = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return isinstance(d, list)


def check_ashby(slug):
    d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return isinstance(d, dict) and "jobs" in d


def check_smartrecruiters(slug):
    d = _get_json(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
    if isinstance(d, dict) and d.get("totalFound", 0) > 0:
        return True
    html = _get_text(f"https://careers.smartrecruiters.com/{slug}")
    return bool(html) and 'property="og:title"' in html


def check_workable(slug):
    d = _get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    return isinstance(d, dict) and "jobs" in d


def check_recruitee(slug):
    d = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    return isinstance(d, dict) and "offers" in d


PROBES = [
    ("greenhouse", check_greenhouse),
    ("lever", check_lever),
    ("ashby", check_ashby),
    ("smartrecruiters", check_smartrecruiters),
    ("workable", check_workable),
    ("recruitee", check_recruitee),
]


def detect(name, delay, extra_slugs=None):
    # Try any hand-supplied slugs first (from "Company | slug1 | slug2" input),
    # then fall back to auto-generated variants. Dedupe, preserve order.
    slugs = []
    for s in list(extra_slugs or []) + slug_variants(name):
        if s and s not in slugs:
            slugs.append(s)
    for slug in slugs:
        for platform, check in PROBES:
            if check(slug):
                return {"platform": platform, "slug": slug, "label": name.strip()}
            time.sleep(delay)
    return None


def load_names(path, column):
    """Return a list of (display_name, extra_slugs) tuples.

    Text-file format (one entry per line, # comments ok):
        Company Name
        Company Name | slug1 | slug2   <- extra slugs tried before auto-variants

    CSV format: column must be specified; extra-slug syntax not supported in CSV.
    """
    p = Path(path)
    if p.suffix.lower() == ".csv":
        if not column:
            sys.exit("CSV input needs --column <name of the company-name column>")
        with open(p, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if column not in (reader.fieldnames or []):
                sys.exit(f"Column '{column}' not found. Columns: {reader.fieldnames}")
            entries = [(row[column], []) for row in reader if (row.get(column) or "").strip()]
    else:
        entries = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            entries.append((parts[0], [s for s in parts[1:] if s]))
    # dedupe by name, preserve order, case-insensitive
    seen, out = set(), []
    for name, extra_slugs in entries:
        key = name.strip().lower()
        if key not in seen:
            seen.add(key)
            out.append((name.strip(), extra_slugs))
    return out


def main():
    ap = argparse.ArgumentParser(description="Detect ATS platforms for companies.")
    ap.add_argument("input", help="Text file (one company per line) or CSV")
    ap.add_argument("--column", help="CSV column containing company names")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds between probes (default 0.5; be polite)")
    args = ap.parse_args()

    entries = load_names(args.input, args.column)
    print(f"Probing {len(entries)} companies (up to ~6 requests each; "
          f"worst case ~{int(len(entries) * 6 * args.delay / 60) + 1} min)...\n")

    found, missed = [], []
    for i, (name, extra_slugs) in enumerate(entries, 1):
        hint = f" [hints: {', '.join(extra_slugs)}]" if extra_slugs else ""
        print(f"[{i}/{len(entries)}] {name}{hint} ... ", end="", flush=True)
        hit = detect(name, args.delay, extra_slugs)
        if hit:
            print(f"{hit['platform']} ({hit['slug']})")
            found.append(hit)
        else:
            print("not found")
            missed.append(name)

    (DATA_DIR / "watchlist_found.json").write_text(
        json.dumps(found, indent=2), encoding="utf-8")

    # Misses: write both a plain list and a fill-in-the-blank template so you
    # can look up real slugs by hand (open the company's careers page, click a
    # job, read the slug from the URL) and paste straight into the watchlist.
    (DATA_DIR / "watchlist_misses.txt").write_text(
        "\n".join(missed), encoding="utf-8")
    template = [
        {"label": name,
         "platform": "FILL_IN: greenhouse | lever | ashby | smartrecruiters | workable | recruitee",
         "slug": "FILL_IN: from the careers-page URL, e.g. boards.greenhouse.io/THIS"}
        for name in missed
    ]
    (DATA_DIR / "watchlist_manual.json").write_text(
        json.dumps(template, indent=2), encoding="utf-8")

    print(f"\n{len(found)} detected -> watchlist_found.json")
    print(f"{len(missed)} not found -> watchlist_misses.txt (names)")
    print(f"{len(missed)} not found -> watchlist_manual.json (fill-in template)")
    print("\nPaste the contents of watchlist_found.json into the \"companies\" array")
    print("of the watchlist source in scraper_config.json.")
    print("For misses you care about: find the real slug in their careers-page URL,")
    print("fill in watchlist_manual.json, delete the rows you don't want, and add those too.")


if __name__ == "__main__":
    main()
