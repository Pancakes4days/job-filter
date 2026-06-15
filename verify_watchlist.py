#!/usr/bin/env python3
"""
Verify watchlist_found.json entries by asking each job board to identify
itself. Run ON THE PI. Greenhouse returns the company's real display name;
for Lever/Ashby we print job counts and sample titles/locations so impostors
(same slug, different company) are obvious.

Usage:
    python3 verify_watchlist.py                  # reads watchlist_found.json
    python3 verify_watchlist.py myfile.json
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = {"User-Agent": "JobFilterBot/1.0 (watchlist verification)"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def sample(jobs, title_key, loc_fn, n=3):
    out = []
    for j in jobs[:n]:
        out.append(f"{j.get(title_key, '?')} [{loc_fn(j) or '?'}]")
    return "; ".join(out) if out else "(no open jobs)"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "watchlist_found.json"
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"Verifying {len(entries)} boards...\n")

    for e in entries:
        platform, slug, label = e["platform"], e["slug"], e.get("label", "")
        line = f"{label!r:32} {platform}:{slug:28} -> "
        try:
            if platform == "greenhouse":
                board = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
                real_name = board.get("name", "?")
                jobs = get_json(
                    f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
                ).get("jobs", [])
                info = sample(jobs, "title",
                              lambda j: (j.get("location") or {}).get("name"))
                print(f"{line}REAL NAME: {real_name!r} | {len(jobs)} jobs | {info}")
            elif platform == "lever":
                jobs = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
                info = sample(jobs, "text",
                              lambda j: (j.get("categories") or {}).get("location"))
                print(f"{line}{len(jobs)} jobs | {info}")
            elif platform == "ashby":
                jobs = get_json(
                    f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                ).get("jobs", [])
                info = sample(jobs, "title",
                              lambda j: j.get("location") or j.get("locationName"))
                print(f"{line}{len(jobs)} jobs | {info}")
            elif platform == "smartrecruiters":
                jobs = get_json(
                    f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                ).get("content", [])
                info = sample(jobs, "name",
                              lambda j: (j.get("location") or {}).get("city"))
                print(f"{line}{len(jobs)} jobs | {info}")
            elif platform == "workable":
                jobs = get_json(
                    f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
                ).get("jobs", [])
                info = sample(jobs, "title",
                              lambda j: (j.get("location") or {}).get("city"))
                print(f"{line}{len(jobs)} jobs | {info}")
            elif platform == "recruitee":
                jobs = get_json(
                    f"https://{slug}.recruitee.com/api/offers/"
                ).get("offers", [])
                info = sample(jobs, "title", lambda j: j.get("location"))
                print(f"{line}{len(jobs)} jobs | {info}")
            else:
                print(f"{line}unknown platform")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, ValueError) as ex:
            print(f"{line}ERROR ({ex})")
        time.sleep(0.5)

    print("\nReview the list: for greenhouse, REAL NAME should match the label.")
    print("For lever/ashby, sample titles/locations should look like the company")
    print("you applied to. Delete bad entries from watchlist_found.json.")


if __name__ == "__main__":
    main()
