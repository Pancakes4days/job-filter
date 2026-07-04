"""Quick one-screen progress check: how many scraped jobs are still unscored.
(pipeline_stats.py gives the full picture; this is the 2-second version.)"""

import json

from filter_jobs import count_scrape_matches, job_fingerprint, load_seen
from matches import read_matches
from paths import DATA_DIR


def main():
    seen_all = load_seen()
    jobs     = json.loads((DATA_DIR / "scraped_jobs.json").read_text(encoding="utf-8"))["jobs"]
    unseen   = [j for j in jobs if job_fingerprint(j) not in seen_all]
    rows     = read_matches(DATA_DIR / "matched_jobs.csv")

    # Matches for THIS scrape only, fingerprint-keyed to match how "scored"
    # is counted (the CSV is cumulative — mixing scopes or keying by URL made
    # "No match" go negative).
    scrape_matches = count_scrape_matches(rows, jobs)
    scored_current = len(jobs) - len(unseen)
    no_match       = scored_current - scrape_matches

    print(f"All time scored:           {len(seen_all)}")
    print(f"All time matches:          {len(rows)}")
    print(f"Scored (this scrape):      {scored_current}")
    print(f"  -> Matches:              {scrape_matches}")
    print(f"  -> No match:             {no_match}")
    print(f"Remaining (this scrape):   {len(unseen)}")
    print(f"Total jobs (this scrape):  {len(jobs)}")


if __name__ == "__main__":
    main()
