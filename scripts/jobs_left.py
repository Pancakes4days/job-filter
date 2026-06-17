import json, hashlib
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

def fingerprint(job):
    key = job.get("url") or f"{job.get('title','')}|{job.get('company','')}"
    return hashlib.sha256(key.strip().lower().encode("utf-8")).hexdigest()[:16]

seen = {l.strip() for l in (DATA / "seen_jobs.txt").read_text().splitlines() if l.strip()}
jobs = json.loads((DATA / "scraped_jobs.json").read_text())["jobs"]
unseen = [j for j in jobs if fingerprint(j) not in seen]

print(f"Total jobs:    {len(jobs)}")
print(f"Scored so far: {len(seen)}")
print(f"Remaining:     {len(unseen)}")
