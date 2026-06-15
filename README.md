# Job Filter Pipeline — Raspberry Pi 5 + gemma3:4b

Scores job listings against your skills/preferences using a local LLM and
writes matches to a CSV you can open in Excel. No cloud, no API keys, no
Python packages to install.

## Files

- `filter_jobs.py` — the pipeline (stdlib only, Python 3.9+)
- `config.json` — **edit this first**: your skills, preferences, dealbreakers
- `sample_jobs.json` — fake listings for testing
- `matched_jobs.csv` — created on first run (open in Excel)
- `seen_jobs.txt` — created automatically; tracks processed jobs so re-runs skip duplicates

## One-time Pi setup

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull the model (~1.5 GB download)
ollama pull gemma3:4b

# Sanity check
ollama run gemma3:4b "Say hello in five words."
```

## Usage

```bash
# 1. Edit your profile
nano config.json

# 2. Test the plumbing without the model (instant)
python3 filter_jobs.py sample_jobs.json --dry-run --all

# 3. Real run on the samples (expect ~20-60s per job on the Pi)
python3 filter_jobs.py sample_jobs.json

# Later, with your scraper's output:
python3 filter_jobs.py scraped_jobs.json
```

Flags:
- `--all` — write every job to the CSV, not just matches (useful while tuning)
- `--rescore` — ignore seen_jobs.txt and re-evaluate everything (after editing your profile, delete seen_jobs.txt or use this)
- `--csv path.csv` — custom output location
- `--dry-run` — skip the LLM entirely; tests file handling

## The contract for your future scraper

Have your scraper write a JSON file shaped like this — that's all the
pipeline needs:

```json
{
  "jobs": [
    {
      "title": "...",        // required in practice
      "company": "...",
      "location": "...",
      "salary": "...",        // optional
      "url": "...",           // used for duplicate detection — include it
      "description": "..."    // the more text here, the better the scoring
    }
  ]
}
```

A bare JSON array `[ {...}, {...} ]` also works.

## Automating with cron

Once your scraper exists, chain them nightly:

```bash
crontab -e
# Run at 2am every night:
0 2 * * * cd /home/pi/job_filter && /usr/bin/python3 scraper.py && /usr/bin/python3 filter_jobs.py scraped_jobs.json >> filter.log 2>&1
```

## Tuning tips

- Start with `--all` and a low threshold so you can see how the model scores
  everything, then tighten `threshold` in config.json once you trust it.
- Borderline scores (5-6) are where a 2B model is least reliable — skim those
  yourself rather than trusting the suitable=true/false flag blindly.
- Long descriptions are good, but if listings exceed ~3,000 words, raise
  `num_ctx` in config.json (costs RAM) or truncate in your scraper.
- Keep dealbreakers concrete ("requires security clearance") rather than vague
  ("bad culture") — small models follow explicit rules far better.
