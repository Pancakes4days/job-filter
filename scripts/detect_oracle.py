#!/usr/bin/env python3
"""
detect_oracle.py — resolve a company's Oracle Recruiting Cloud (Candidate
Experience) careers site into the canonical "host/site" slug used by the
scraper's `oracle` connector, and verify it against the live API.

Oracle-hosted careers sites live at a host like {tenant}.fa.oraclecloud.com or a
shared pod fa-ext...saasfaprod1.fa.ocs.oraclecloud.com, with a career-site view
identified by a "CX_####" site number. The scraper needs that "host/site" string
(e.g. "jpmc.fa.oraclecloud.com/CX_1001"). This tool finds it from either:
  * a direct Oracle URL   (…/hcmUI/CandidateExperience/en/sites/CX_1001/…)
  * a company careers URL  (it follows redirects and scans the HTML for one)

It then calls the list endpoint and prints the job count + a couple sample
titles/locations so you can confirm it's the right company — important because
on a SHARED pod many tenants share the host and the CX_#### site number is what
actually identifies the employer, so eyeball the samples.

Usage:
    python3 detect_oracle.py https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions
    python3 detect_oracle.py https://www.jpmorganchase.com/careers
    python3 detect_oracle.py --batch companies_with_urls.txt   # "Label | url" per line

Writes verified entries to data/watchlist_oracle.json.
"""

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from paths import DATA_DIR  # noqa: E402

BROWSER_UA = "Mozilla/5.0 (compatible; JobFilterBot/1.0; personal job search)"
EXPAND = "requisitionList.secondaryLocations,flexFieldsFacet.values"
# Host + site captured together from a CandidateExperience URL (most reliable). The
# site is whatever follows /sites/ — "CX", "CX_1001", "jobsearch", etc.
CAREER_RE = re.compile(
    r"([a-z0-9][a-z0-9.\-]*\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#\"']+)",
    re.I)
HOST_RE = re.compile(r"([a-z0-9][a-z0-9.\-]*\.oraclecloud\.com)", re.I)
SITE_RE = re.compile(r"/sites/([^/?#\"']+)")


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


def _scan(text):
    """Pull (host, site) out of a blob — prefer the combined CandidateExperience URL,
    fall back to a standalone host + a /sites/<site> reference."""
    m = CAREER_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    hm, sm = HOST_RE.search(text), SITE_RE.search(text)
    if hm and sm:
        return hm.group(1), sm.group(1)
    return None


def find_oracle_target(url):
    """Return (host, site) for an Oracle careers site reachable from `url`, or None."""
    hit = _scan(url)
    if hit:
        return hit
    # careers page: fetch and scan the final URL + body. Catch broadly — any failure
    # just means the guess didn't pan out; it must never abort a batch.
    try:
        final_url, body = _get(url)
    except Exception as e:
        print(f"    (fetch failed: {type(e).__name__})")
        return None
    return _scan(final_url) or _scan(body)


def verify(host, site):
    """List the site's jobs. Return (total, sample_titles) or None if not a real board."""
    url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand={EXPAND}"
           f"&finder=findReqs;siteNumber={site},limit=3,offset=0,sortBy=POSTING_DATES_DESC")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    items = data.get("items", [])
    if not items or "TotalJobsCount" not in items[0]:
        return None
    reqs = items[0].get("requisitionList", [])
    samples = [f"{r.get('Title', '?')} [{r.get('PrimaryLocation', '?')}]" for r in reqs[:3]]
    return items[0].get("TotalJobsCount", 0), samples


def resolve_one(label, url):
    print(f"{label} ... ", end="", flush=True)
    target = find_oracle_target(url)
    if not target:
        print("no Oracle site found")
        return None
    host, site = target
    result = verify(host, site)
    if result is None:
        print(f"found {host}/{site} but CE API rejected it")
        return None
    total, samples = result
    print(f"OK  {host}/{site}  ({total} jobs)")
    for s in samples:
        print(f"      - {s}")
    return {"platform": "oracle", "slug": f"{host}/{site}", "label": label}


def load_batch(path):
    entries = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        label, _, url = ln.partition("|")
        label, url = label.strip(), url.strip()
        if not url:
            url, label = label, label
        entries.append((label, url))
    return entries


def main():
    ap = argparse.ArgumentParser(description="Resolve Oracle Cloud careers sites to slugs.")
    ap.add_argument("target", nargs="?", help="An Oracle URL or company careers URL")
    ap.add_argument("--batch", help="File of 'Label | url' lines (one company each)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Seconds between companies in batch mode (default 1.0)")
    args = ap.parse_args()

    if not args.target and not args.batch:
        ap.error("give a URL or --batch file")

    entries = load_batch(args.batch) if args.batch else [(args.target, args.target)]
    found, missed = [], []
    for i, (label, url) in enumerate(entries, 1):
        if args.batch:
            print(f"[{i}/{len(entries)}] ", end="")
        try:
            entry = resolve_one(label, url)
        except Exception as e:
            print(f"{label} ... error ({type(e).__name__})")
            entry = None
        (found if entry else missed).append(entry or label)
        if args.batch:
            time.sleep(args.delay)

    out_path = DATA_DIR / "watchlist_oracle.json"
    out_path.write_text(json.dumps([e for e in found], indent=2), encoding="utf-8")
    print(f"\n{len(found)} verified -> {out_path}")
    if missed:
        print(f"{len(missed)} unresolved: " + ", ".join(missed))
    print("\nPaste the verified entries into the watchlist \"companies\" array.")


if __name__ == "__main__":
    main()
