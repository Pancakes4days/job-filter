#!/usr/bin/env python3
"""
pipeline_stats.py — Rich analytics snapshot of the job filter pipeline.
Run any time from ~/job_filter:
    python3 scripts/pipeline_stats.py
    python3 scripts/pipeline_stats.py --top 20      # show more rows
    python3 scripts/pipeline_stats.py --company Palantir
"""

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

BASE    = Path(__file__).resolve().parent.parent
DATA    = BASE / "data"
SCRIPTS = BASE / "scripts"

SCRAPED   = DATA / "scraped_jobs.json"
SEEN      = DATA / "seen_jobs.txt"
MATCHES   = DATA / "matched_jobs.csv"
STATE     = DATA / "orchestrator_state.json"
CFG       = BASE / "config" / "config.json"

CSV_FIELDS = [
    "date_processed", "title", "company", "location", "salary", "url", "source",
    "score", "suitable", "matched_skills", "concerns", "reason",
]

# ── helpers ───────────────────────────────────────────────────────────────────

def fingerprint(job):
    key = job.get("url") or f"{job.get('title','')}|{job.get('company','')}"
    return hashlib.sha256(key.strip().lower().encode()).hexdigest()[:16]

def load_seen():
    if not SEEN.exists():
        return set()
    return {l.strip() for l in SEEN.read_text().splitlines() if l.strip()}

def load_jobs():
    if not SCRAPED.exists():
        return []
    return json.loads(SCRAPED.read_text())["jobs"]

def load_matches():
    if not MATCHES.exists():
        return []
    rows = []
    with open(MATCHES, newline="", encoding="utf-8-sig") as f:
        first = f.readline(); f.seek(0)
        has_header = first.strip().startswith("date_processed")
        reader = csv.DictReader(f, fieldnames=None if has_header else CSV_FIELDS)
        for row in reader:
            try:
                score = int(float(row.get("score", "") or 0))
                if 0 <= score <= 10:
                    rows.append(row)
            except (TypeError, ValueError):
                continue
    return rows

def load_state():
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text())

def load_threshold():
    if not CFG.exists():
        return 6
    return json.loads(CFG.read_text()).get("profile", {}).get("threshold", 6)

def bar(n, total, width=20):
    filled = int(width * n / total) if total else 0
    return "█" * filled + "░" * (width - filled)

def pct(n, total):
    return f"{100*n/total:.0f}%" if total else "0%"

def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Pipeline analytics snapshot.")
    ap.add_argument("--top",     type=int, default=15, help="Rows to show in rankings")
    ap.add_argument("--company", type=str, default=None, help="Filter to one company")
    args = ap.parse_args()

    jobs      = load_jobs()
    seen      = load_seen()
    matches   = load_matches()
    state     = load_state()
    threshold = load_threshold()

    if not jobs:
        print("No scraped_jobs.json found — run the scraper first.")
        return

    # Partition jobs
    fps = {fingerprint(j): j for j in jobs}
    scored_fps   = {fp for fp in fps if fp in seen}
    unscored_fps = {fp for fp in fps if fp not in seen}

    scored_jobs   = [fps[fp] for fp in scored_fps]
    unscored_jobs = [fps[fp] for fp in unscored_fps]

    total        = len(jobs)
    scored       = len(scored_jobs)
    remaining    = len(unscored_jobs)
    all_time     = len(seen)
    match_count  = len(matches)
    # Count matches from current scrape only
    job_urls = {j.get("url","").strip().lower() for j in jobs if j.get("url")}
    scrape_matches  = sum(1 for r in matches if (r.get("url") or "").strip().lower() in job_urls)
    scrape_no_match = scored - scrape_matches
    scrape_label    = "last scrape" if remaining == 0 else "this scrape"

    # ── OVERVIEW ──────────────────────────────────────────────────────────────
    section("PIPELINE OVERVIEW")
    print(f"  {'Total jobs (' + scrape_label + ')':<30} {total:>6}")
    print(f"  {'Ever scored (all time)':<30} {all_time:>6}")
    print(f"  {'Scored (' + scrape_label + ')':<30} {scored:>6}")
    print(f"    -> Matches (score >= {threshold})          {scrape_matches:>6}")
    print(f"    -> No match                      {scrape_no_match:>6}")
    print(f"  {'Remaining':<30} {remaining:>6}")
    print(f"  {'Progress':<30} {bar(scored, total)}  {pct(scored, total):>5}")

    # Orchestrator state
    phase    = state.get("phase", "unknown")
    next_run = state.get("next_run", "unknown")
    print(f"\n  Pipeline phase : {phase}")
    print(f"  Next run       : {next_run}")

    # ── SCORE DISTRIBUTION ────────────────────────────────────────────────────
    if matches:
        section("SCORE DISTRIBUTION  (matched jobs)")
        score_dist = Counter(int(float(r.get("score", 0))) for r in matches)
        for s in sorted(score_dist.keys(), reverse=True):
            n = score_dist[s]
            print(f"  {s:>2}/10  {bar(n, match_count, 25)}  {n:>4}  {pct(n, match_count):>5}")

    # ── COMPANY BREAKDOWN (matches) ────────────────────────────────────────────
    if matches:
        section(f"TOP COMPANIES BY MATCHES  (top {args.top})")
        co_matches   = Counter(r.get("company", "?") for r in matches)
        co_scores    = defaultdict(list)
        for r in matches:
            try:
                co_scores[r.get("company", "?")].append(int(float(r.get("score", 0))))
            except (TypeError, ValueError):
                pass

        print(f"  {'Company':<28} {'Matches':>7}  {'Avg Score':>9}  {'Max':>4}")
        print(f"  {'─'*28}  {'─'*7}  {'─'*9}  {'─'*4}")
        for co, cnt in co_matches.most_common(args.top):
            sc = co_scores[co]
            avg = sum(sc)/len(sc) if sc else 0
            mx  = max(sc) if sc else 0
            print(f"  {co:<28} {cnt:>7}  {avg:>9.1f}  {mx:>4}")

    # ── NEW JOBS BY COMPANY (unscored) ─────────────────────────────────────────
    if unscored_jobs:
        section(f"COMPANIES WITH UNSCORED JOBS  (top {args.top})")
        co_unscored = Counter(j.get("company", "?") for j in unscored_jobs)
        print(f"  {'Company':<28} {'Unscored':>8}")
        print(f"  {'─'*28}  {'─'*8}")
        for co, cnt in co_unscored.most_common(args.top):
            print(f"  {co:<28} {cnt:>8}")

    # ── SOURCE BREAKDOWN ──────────────────────────────────────────────────────
    section("JOBS BY SOURCE  (this scrape)")
    source_dist = Counter()
    for j in jobs:
        src = j.get("source", "unknown")
        # group watchlist sources by platform
        if ":" in src:
            platform = src.split(":")[0]
            source_dist[f"watchlist ({platform})"] += 1
        else:
            source_dist[src] += 1
    for src, cnt in source_dist.most_common():
        print(f"  {src:<30} {cnt:>6}  {bar(cnt, total, 15)}  {pct(cnt, total):>5}")

    # ── LOCATION BREAKDOWN (matches) ──────────────────────────────────────────
    if matches:
        section(f"TOP LOCATIONS  (matched jobs, top {args.top})")
        def simplify_loc(loc):
            loc = (loc or "").strip()
            if not loc:             return "Unknown"
            if "Remote" in loc:     return "Remote (US)"
            if "New York" in loc or "NYC" in loc: return "New York, NY"
            if "San Francisco" in loc or "SF" in loc: return "San Francisco, CA"
            if "Boston" in loc:     return "Boston, MA"
            if "Austin" in loc:     return "Austin, TX"
            if "Seattle" in loc:    return "Seattle, WA"
            if "Switzerland" in loc or "Zurich" in loc: return "Switzerland"
            return loc[:35]

        loc_dist = Counter(simplify_loc(r.get("location","")) for r in matches)
        for loc, cnt in loc_dist.most_common(args.top):
            print(f"  {loc:<35} {cnt:>5}  {pct(cnt, match_count):>5}")

    # ── COMPANY FILTER ────────────────────────────────────────────────────────
    if args.company:
        section(f"DETAIL: {args.company}")
        co_jobs = [j for j in jobs
                   if args.company.lower() in j.get("company","").lower()]
        co_matches_list = [r for r in matches
                           if args.company.lower() in r.get("company","").lower()]
        co_seen = {fingerprint(j) for j in co_jobs if fingerprint(j) in seen}

        print(f"  Total postings in scrape : {len(co_jobs)}")
        print(f"  Scored                   : {len(co_seen)}")
        print(f"  Matches                  : {len(co_matches_list)}")
        print()
        if co_matches_list:
            print(f"  {'Score':<6} {'Title':<45} {'Location'}")
            print(f"  {'─'*6} {'─'*45} {'─'*20}")
            for r in sorted(co_matches_list,
                            key=lambda x: int(float(x.get("score",0))),
                            reverse=True):
                sc  = r.get("score","?")
                ttl = (r.get("title","") or "")[:44]
                loc = (r.get("location","") or "")[:30]
                print(f"  {sc:<6} {ttl:<45} {loc}")

    # ── ETA ───────────────────────────────────────────────────────────────────
    if remaining > 0:
        eta_mins = remaining * 75 // 60
        eta_hrs  = eta_mins // 60
        eta_min  = eta_mins % 60
        section("ESTIMATED TIME REMAINING")
        print(f"  {remaining} jobs × ~75s/job  →  ~{eta_hrs}h {eta_min}m")


if __name__ == "__main__":
    main()
