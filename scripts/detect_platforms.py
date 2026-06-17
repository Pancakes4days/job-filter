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


def probe(url, validate):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return validate(json.loads(resp.read().decode("utf-8", errors="replace")))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, ValueError):
        return False


# Detection tests for ATS *presence*, not open-job count: a valid board returns
# a 200 with the expected JSON shape even when it has zero current openings, while
# a wrong slug 404s or returns junk (caught as False by probe()). This keeps
# companies that simply have no openings right now from being logged as misses.
PROBES = [
    ("greenhouse",
     "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
     lambda d: isinstance(d, dict) and "jobs" in d),
    ("lever",
     "https://api.lever.co/v0/postings/{slug}?mode=json",
     lambda d: isinstance(d, list)),
    ("ashby",
     "https://api.ashbyhq.com/posting-api/job-board/{slug}",
     lambda d: isinstance(d, dict) and "jobs" in d),
    ("smartrecruiters",
     "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
     lambda d: isinstance(d, dict) and "totalFound" in d),
    ("workable",
     "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
     lambda d: isinstance(d, dict) and "jobs" in d),
    ("recruitee",
     "https://{slug}.recruitee.com/api/offers/",
     lambda d: isinstance(d, dict) and "offers" in d),
]


def detect(name, delay):
    for slug in slug_variants(name):
        for platform, url_tpl, validate in PROBES:
            if probe(url_tpl.format(slug=slug), validate):
                return {"platform": platform, "slug": slug, "label": name.strip()}
            time.sleep(delay)
    return None


def load_names(path, column):
    p = Path(path)
    if p.suffix.lower() == ".csv":
        if not column:
            sys.exit("CSV input needs --column <name of the company-name column>")
        with open(p, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if column not in (reader.fieldnames or []):
                sys.exit(f"Column '{column}' not found. Columns: {reader.fieldnames}")
            names = [row[column] for row in reader if (row.get(column) or "").strip()]
    else:
        names = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    # dedupe, preserve order, case-insensitive
    seen, out = set(), []
    for n in names:
        key = n.strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(n.strip())
    return out


def main():
    ap = argparse.ArgumentParser(description="Detect ATS platforms for companies.")
    ap.add_argument("input", help="Text file (one company per line) or CSV")
    ap.add_argument("--column", help="CSV column containing company names")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds between probes (default 0.5; be polite)")
    args = ap.parse_args()

    names = load_names(args.input, args.column)
    print(f"Probing {len(names)} companies (up to ~6 requests each; "
          f"worst case ~{int(len(names) * 6 * args.delay / 60) + 1} min)...\n")

    found, missed = [], []
    for i, name in enumerate(names, 1):
        print(f"[{i}/{len(names)}] {name} ... ", end="", flush=True)
        hit = detect(name, args.delay)
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
