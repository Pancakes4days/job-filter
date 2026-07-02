#!/usr/bin/env python3
"""
detect_workday.py — resolve a company's Workday careers site into the canonical
"host/site" slug used by the scraper's workday connector, and verify it against
the live jobs API before you trust it.

Workday employers run a tenant at {tenant}.{dc}.myworkdayjobs.com/{site}. The
scraper needs that "host/site" string (e.g. "bitsight.wd1.myworkdayjobs.com/Bitsight").
This tool finds it from either:
  * a direct Workday URL   (…myworkdayjobs.com/Site or …/wday/cxs/tenant/Site/…)
  * a company careers URL   (it follows redirects and scans the HTML for a
                             myworkdayjobs link — many careers pages embed one)

Then it POSTs the cxs jobs endpoint to confirm the board is real and prints the
job count + a couple sample titles/locations so you can eyeball that it's the
right company (a wrong site 404s or looks nothing like the employer).

Usage:
    python3 detect_workday.py https://bitsight.wd1.myworkdayjobs.com/Bitsight
    python3 detect_workday.py https://www.bitsight.com/careers
    python3 detect_workday.py --batch companies_with_urls.txt   # "Label | url" per line

Emits watchlist_workday.json (paste-ready entries for the watchlist "companies"
array) for everything it can verify, and prints the ones it can't so you can
grab their careers-page URL by hand.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from paths import DATA_DIR  # noqa: E402

BROWSER_UA = "Mozilla/5.0 (compatible; JobFilterBot/1.0; personal job search)"
# {tenant}.{dc}.myworkdayjobs.com/{site} — site is the first path segment and may
# be prefixed by a locale like /en-US/. Also matches the internal cxs form
# /wday/cxs/{tenant}/{site}/.
HOST_RE = re.compile(r"([a-z0-9][a-z0-9-]*\.wd\d+\.myworkdayjobs\.com)", re.I)
URL_RE = re.compile(
    r"(?:https?://)?([a-z0-9][a-z0-9-]*\.wd\d+\.myworkdayjobs\.com)"
    r"(?:/wday/cxs/[^/]+)?/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


def find_workday_target(url):
    """Return (host, site) for a Workday careers site reachable from `url`, or None.

    Tries the URL itself first, then the page it resolves to (following redirects)
    and any myworkdayjobs link embedded in that page's HTML.
    """
    m = URL_RE.search(url)
    if m and "/wday/cxs/" not in url.lower():
        return m.group(1), m.group(2)
    # cxs form or a careers page: fetch and scan the final URL + body. Catch broadly
    # (SSL errors, bad certs, resets, decode errors) — any failure just means the
    # guessed URL didn't pan out; it must never abort a batch of many companies.
    try:
        final_url, body = _get(url)
    except Exception as e:
        print(f"    (fetch failed: {type(e).__name__})")
        return None
    for hay in (final_url, body):
        m = URL_RE.search(hay)
        if m:
            return m.group(1), m.group(2)
    return None


def verify(host, site, tenant=None):
    """POST the cxs jobs endpoint. Return (total, sample_titles) or None if not a
    real board. tenant defaults to the host's first label."""
    tenant = tenant or host.split(".")[0]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    payload = json.dumps({"appliedFacets": {}, "limit": 3,
                          "offset": 0, "searchText": ""}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict) or "jobPostings" not in data:
        return None
    samples = [f"{p.get('title', '?')} [{p.get('locationsText', '?')}]"
               for p in data.get("jobPostings", [])[:3]]
    return data.get("total", 0), samples


def resolve_one(label, url):
    """Resolve+verify a single company. Returns a watchlist entry dict or None."""
    print(f"{label} ... ", end="", flush=True)
    target = find_workday_target(url)
    if not target:
        print("no Workday site found")
        return None
    host, site = target
    result = verify(host, site)
    if result is None:
        print(f"found {host}/{site} but cxs API rejected it")
        return None
    total, samples = result
    print(f"OK  {host}/{site}  ({total} jobs)")
    for s in samples:
        print(f"      - {s}")
    return {"platform": "workday", "slug": f"{host}/{site}", "label": label}


def load_batch(path):
    entries = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        label, _, url = ln.partition("|")
        label, url = label.strip(), url.strip()
        if not url:                       # a bare URL line: derive a label from it
            url, label = label, label
        entries.append((label, url))
    return entries


def main():
    ap = argparse.ArgumentParser(description="Resolve Workday careers sites to slugs.")
    ap.add_argument("target", nargs="?", help="A Workday URL or company careers URL")
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
        except Exception as e:                 # last-resort guard: never abort a batch
            print(f"{label} ... error ({type(e).__name__})")
            entry = None
        if entry:
            found.append(entry)
        else:
            missed.append(label)
        if args.batch:
            time.sleep(args.delay)

    out_path = DATA_DIR / "watchlist_workday.json"
    out_path.write_text(json.dumps(found, indent=2), encoding="utf-8")
    print(f"\n{len(found)} verified -> {out_path}")
    if missed:
        print(f"{len(missed)} unresolved (find their careers URL by hand): "
              + ", ".join(missed))
    print("\nPaste the verified entries into the watchlist \"companies\" array.")


if __name__ == "__main__":
    main()
