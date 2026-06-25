import json, hashlib
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"

def fingerprint(job):
    key = job.get("url") or f"{job.get('title','')}|{job.get('company','')}"
    return hashlib.sha256(key.strip().lower().encode("utf-8")).hexdigest()[:16]

seen_all  = {l.strip() for l in (DATA / "seen_jobs.txt").read_text().splitlines() if l.strip()}
jobs      = json.loads((DATA / "scraped_jobs.json").read_text())["jobs"]
unseen    = [j for j in jobs if fingerprint(j) not in seen_all]

csv_path  = DATA / "matched_jobs.csv"
matches   = sum(1 for _ in open(csv_path)) - 1 if csv_path.exists() else 0

scored_current = len(jobs) - len(unseen)
no_match       = scored_current - matches

print(f"All time scored:           {len(seen_all)}")
print(f"Scored (this scrape):      {scored_current}")
print(f"  -> Matches:              {matches}")
print(f"  -> No match:             {no_match}")
print(f"Remaining (this scrape):   {len(unseen)}")
print(f"Total jobs (this scrape):  {len(jobs)}")
