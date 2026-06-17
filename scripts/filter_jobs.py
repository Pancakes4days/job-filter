#!/usr/bin/env python3
"""
Job filter pipeline for Raspberry Pi 5 + gemma3:4b (via Ollama).

Reads job listings from a JSON file (produced by your scraper), asks the
local LLM to score each one against your profile in config.json, and
appends results to a CSV you can open in Excel.

Zero external dependencies — Python 3.9+ stdlib only.

Usage:
    python3 filter_jobs.py jobs.json
    python3 filter_jobs.py jobs.json --csv results.csv
    python3 filter_jobs.py jobs.json --dry-run      # test without calling the model
    python3 filter_jobs.py jobs.json --rescore      # ignore the seen-list, redo everything
"""

import argparse
import csv
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl as _fcntl  # Linux/Mac only; silently unavailable on Windows
except ImportError:
    _fcntl = None

from paths import CONFIG_DIR, DATA_DIR  # noqa: E402

CONFIG_PATH = CONFIG_DIR / "config.json"
SEEN_PATH = DATA_DIR / "seen_jobs.txt"

# JSON schema the model is forced to follow (Ollama structured outputs).
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "suitable": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["suitable", "score", "matched_skills", "concerns", "reason"],
}

CSV_COLUMNS = [
    "date_processed", "title", "company", "location", "salary", "url", "source",
    "score", "suitable", "matched_skills", "concerns", "reason",
]


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"Config not found: {CONFIG_PATH}\nEdit config.json with your profile first.")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_jobs(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "jobs" in data:
        data = data["jobs"]
    if not isinstance(data, list):
        sys.exit("Jobs file must be a JSON array of job objects, or {\"jobs\": [...]}")
    return data


def job_fingerprint(job):
    """Stable ID for duplicate detection: prefer URL, fall back to title+company."""
    key = job.get("url") or f"{job.get('title','')}|{job.get('company','')}"
    return hashlib.sha256(key.strip().lower().encode("utf-8")).hexdigest()[:16]


def load_seen():
    if SEEN_PATH.exists():
        return set(SEEN_PATH.read_text(encoding="utf-8").split())
    return set()


def mark_seen(fp):
    with open(SEEN_PATH, "a", encoding="utf-8") as f:
        f.write(fp + "\n")


def build_system_prompt(profile):
    skills = ", ".join(profile.get("skills", []))
    prefs = "\n".join(f"- {p}" for p in profile.get("preferences", []))
    dealbreakers = "\n".join(f"- {d}" for d in profile.get("dealbreakers", []))
    return f"""You are a strict job-matching assistant. Evaluate whether a job listing
suits this specific candidate. Be honest and conservative: when in doubt, score lower.

CANDIDATE SKILLS:
{skills}

CANDIDATE PREFERENCES (each match raises the score):
{prefs}

DEALBREAKERS (any one of these means suitable=false and score <= 3):
{dealbreakers}

Scoring rubric:
- 9-10: strong skill match, no concerns, hits multiple preferences
- 7-8: good skill match, minor concerns
- 5-6: partial match, worth a human look
- 0-4: poor match or hits a dealbreaker

Set "suitable" to true only for score >= {profile.get('threshold', 6)}.
List matched_skills only for skills the candidate actually has.
Keep "reason" to one or two sentences."""


def build_user_prompt(job):
    parts = []
    for field in ("title", "company", "location", "salary", "description"):
        val = job.get(field)
        if val:
            parts.append(f"{field.upper()}: {val}")
    return "Evaluate this job listing:\n\n" + "\n".join(parts)


def call_ollama(config, system_prompt, user_prompt):
    """Call local Ollama /api/chat with an enforced JSON schema. Returns parsed dict."""
    payload = {
        "model": config.get("model", "gemma3:4b"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": RESULT_SCHEMA,
        "options": {
            "temperature": config.get("temperature", 0.1),
            "num_ctx": config.get("num_ctx", 4096),
        },
    }
    req = urllib.request.Request(
        config.get("ollama_url", "http://localhost:11434") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.get("timeout_seconds", 300)) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["message"]["content"]
    return json.loads(content)


def validate_result(result, threshold=6):
    """Belt-and-braces validation even though the schema is enforced.
    NOTE: small models set `suitable` inconsistently with their own `score`
    (e.g. score 3 but suitable=true). The score is the more considered value,
    so we DERIVE suitable from it rather than trusting the model's boolean."""
    score = max(0, min(10, int(result.get("score", 0))))
    out = {
        "suitable": score >= threshold,
        "score": score,
        "matched_skills": "; ".join(str(s) for s in result.get("matched_skills", [])),
        "concerns": "; ".join(str(c) for c in result.get("concerns", [])),
        "reason": str(result.get("reason", "")).strip(),
    }
    return out


def append_csv(csv_path, row):
    csv_path = Path(csv_path)
    is_new = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        if _fcntl is not None:
            _fcntl.flock(f, _fcntl.LOCK_EX)
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
        # lock released automatically on file close


def main():
    parser = argparse.ArgumentParser(description="Filter job listings with a local LLM.")
    parser.add_argument("jobs_file", help="JSON file of job listings from your scraper")
    parser.add_argument("--csv", default=str(DATA_DIR / "matched_jobs.csv"),
                        help="Output CSV path (default: matched_jobs.csv)")
    parser.add_argument("--all", action="store_true",
                        help="Write every job to the CSV, not just suitable ones")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-evaluate jobs even if already seen")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip the LLM call; emit fake scores to test the pipeline")
    args = parser.parse_args()

    config = load_config()
    profile = config.get("profile", {})
    system_prompt = build_system_prompt(profile)
    jobs = load_jobs(args.jobs_file)
    seen = set() if args.rescore else load_seen()

    total, kept, skipped, errors = 0, 0, 0, 0
    started = time.time()

    for job in jobs:
        fp = job_fingerprint(job)
        if fp in seen:
            skipped += 1
            continue
        total += 1
        title = job.get("title", "(no title)")
        print(f"[{total}] {title} @ {job.get('company','?')} ... ", end="", flush=True)

        try:
            if args.dry_run:
                result = {"suitable": True, "score": 7,
                          "matched_skills": ["dry-run"], "concerns": [],
                          "reason": "Dry run — no model called."}
            else:
                result = call_ollama(config, system_prompt, build_user_prompt(job))
            r = validate_result(result, profile.get("threshold", 6))
        except urllib.error.URLError as e:
            errors += 1
            reason = str(getattr(e, "reason", e))
            if "refused" in reason.lower():
                print(f"OLLAMA OFFLINE — start Ollama and retry ({reason})")
            else:
                print(f"OLLAMA NETWORK ERROR ({reason})")
            continue
        except TimeoutError:
            errors += 1
            print(f"OLLAMA TIMEOUT — model too slow or num_ctx={config.get('num_ctx', 4096)} too large for this job")
            continue
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            errors += 1
            print(f"BAD RESPONSE ({e}) — skipping")
            continue

        verdict = "MATCH" if r["suitable"] else "no"
        print(f"score {r['score']}/10 -> {verdict}")

        if r["suitable"] or args.all:
            append_csv(args.csv, {
                "date_processed": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "salary": job.get("salary", ""),
                "url": job.get("url", ""),
                "source": job.get("source", ""),
                **r,
            })
            kept += 1

        if not args.rescore:
            mark_seen(fp)

    elapsed = time.time() - started
    print(f"\nDone. Evaluated {total}, wrote {kept} to {args.csv}, "
          f"skipped {skipped} already-seen, {errors} errors, {elapsed:.0f}s elapsed.")


if __name__ == "__main__":
    main()
