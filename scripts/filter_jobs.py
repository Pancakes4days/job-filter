#!/usr/bin/env python3
"""
Job filter pipeline for Raspberry Pi 5 + gemma3:4b (via Ollama).

The model name in config.json must match an `ollama list` tag exactly. A
mismatch 404s on every job, trips MAX_CONSECUTIVE_ERRORS, and exits nonzero —
which parks the orchestrator on a 15-minute retry of the filter phase, so
`store` never runs and no new jobs reach the tracker. Symptom is silence, not
an alarm: check `data/filter.log` for OLLAMA errors if the DB stops growing.

Reads job listings from a JSON file (produced by your scraper), decides which
ones are worth keeping, and appends the survivors to a CSV you can open in Excel.

Two stages, deliberately split by what each is good at:

    1. gate_job()                 regex over title/location/source, no model
    2. the model + score_from_classification()

The model is asked only to CLASSIFY what a posting says (role family, seniority,
years required, start date, clearance, degree); the rubric that turns those
labels into a 0-10 score lives in Python. The previous design asked a 4B model
for the score directly against a 20-clause prompt, and it could not do it —
229 of 404 kept jobs landed on exactly 8, and the model wrote "score must be 1"
into its own concerns field on jobs it then scored 8. Both stages are configured
in config.json under profile.gate and profile.scoring.

Zero external dependencies — Python 3.9+ stdlib only.

Usage:
    python3 filter_jobs.py jobs.json
    python3 filter_jobs.py jobs.json --csv results.csv
    python3 filter_jobs.py jobs.json --dry-run      # test without calling the model
    python3 filter_jobs.py jobs.json --rescore      # ignore the seen-list, redo everything
    python3 filter_jobs.py jobs.json --no-gate      # send everything to the model
    python3 filter_jobs.py jobs.json --all          # also write rejects (gate reasons included)
"""

import argparse
import csv
import hashlib
import json
import re
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

from matches import CSV_FIELDS, TS_FORMAT  # noqa: E402 — shared writer/reader schema
from paths import CONFIG_DIR, DATA_DIR  # noqa: E402

CONFIG_PATH = CONFIG_DIR / "config.json"
SEEN_PATH = DATA_DIR / "seen_jobs.txt"

# Abort after this many failures in a row. A dead Ollama fails fast, but a
# WEDGED one (accepts connections, never answers) burns the full
# timeout_seconds (default 300s) per job — without this, a big scrape could
# spend hours timing out before anyone notices.
MAX_CONSECUTIVE_ERRORS = 3

# JSON schema the model is forced to follow (Ollama structured outputs).
#
# The model CLASSIFIES; it does not score. Asking a 4B model for a 0-10 integer
# against a 20-clause rubric produced a pile at 7-8 and nothing else: it cannot
# hold that many constraints at once, so it fell back on the middle of the
# range. Every field below is a single closed question, which is the one thing
# models this size do reliably. score_from_classification() turns the answers
# into a number, so the rubric lives in Python where it is exact and testable.
#
# "evidence" is load-bearing, not decoration: requiring a quoted line from the
# posting costs one short string and measurably curbs invention, because the
# model has to point at text that exists.
ROLE_FAMILIES = ["software_engineering", "security", "ml_ai", "data",
                 "infra_devops", "other_technical", "non_technical"]
SENIORITIES   = ["intern", "new_grad", "junior", "mid", "senior_plus", "unclear"]
START_TIMINGS = ["2027_or_later", "2026_or_earlier", "unclear"]

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "role_family":    {"type": "string", "enum": ROLE_FAMILIES},
        "seniority":      {"type": "string", "enum": SENIORITIES},
        "min_years_experience": {"type": "integer", "minimum": 0, "maximum": 15},
        "start_timing":   {"type": "string", "enum": START_TIMINGS},
        "clearance_required":       {"type": "boolean"},
        "advanced_degree_required": {"type": "boolean"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "evidence":       {"type": "string"},
    },
    "required": ["role_family", "seniority", "min_years_experience",
                 "start_timing", "clearance_required",
                 "advanced_degree_required", "matched_skills", "evidence"],
}

# Fallback labels when a job is scored without a model call (--dry-run).
DRY_RUN_RESULT = {
    "role_family": "software_engineering", "seniority": "new_grad",
    "min_years_experience": 0, "start_timing": "unclear",
    "clearance_required": False, "advanced_degree_required": False,
    "matched_skills": ["dry-run"], "evidence": "Dry run — no model called.",
}

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


def count_scrape_matches(rows, jobs):
    """How many of this scrape's jobs have a match row in the CSV.

    Keyed by the same fingerprint as seen-tracking, so it can't drift from
    'scored' counts the way URL-set intersection did: duplicate CSV rows for
    one job collapse, URL-less jobs (title|company fingerprints) still count,
    and blank/whitespace URLs can't cross-match. Used by pipeline_stats.py
    and jobs_left.py. (Lives here, not in matches.py — matches importing
    job_fingerprint back from this module would be a cycle.)"""
    row_fps = {job_fingerprint(r) for r in rows}
    return len({job_fingerprint(j) for j in jobs} & row_fps)


def load_seen():
    if SEEN_PATH.exists():
        return set(SEEN_PATH.read_text(encoding="utf-8").split())
    return set()


# ── deterministic gate ────────────────────────────────────────────────────────
# Runs BEFORE the model. Title, location and source are string properties, and a
# regex decides them correctly every time where the LLM did not — it was scoring
# Production Test Technicians and Quantitative Traders 8/10 while writing "score
# must be 1" into its own concerns field. Gating them costs no inference at all,
# and (the real prize) stops those clauses from competing for the model's
# attention with the judgment we actually want from it.
#
# The rule everywhere here: reject only on POSITIVE evidence. A title that
# matches nothing, or a location string we don't recognise, falls through to the
# model. A gate that guesses is worse than no gate, because its mistakes are
# invisible — the job never appears anywhere to be noticed as missing.

def compile_gate(gate_cfg):
    """Config's regex strings -> compiled patterns, once per run.

    Keys starting with '_' are prose notes for whoever edits config.json and are
    skipped. An empty list compiles to None rather than an empty alternation,
    which would match every string and silently reject everything."""
    def compile_all(patterns):
        return re.compile("|".join(patterns), re.I) if patterns else None

    return {
        "enabled": gate_cfg.get("enabled", True),
        "sources": {s.lower() for s in gate_cfg.get("blocked_sources", [])},
        "titles": {rule: compile_all(pats)
                   for rule, pats in gate_cfg.get("title_reject", {}).items()
                   if not rule.startswith("_")},
        "loc_allow": compile_all(gate_cfg.get("location_allow", [])),
        "loc_deny":  compile_all(gate_cfg.get("location_deny", [])),
    }


def gate_job(job, gate):
    """None if the job should go to the model, else (rule, detail) explaining
    the rejection. Detail names the matched text so filter.log shows why."""
    if not gate["enabled"]:
        return None

    source = (job.get("source") or "").lower()
    for blocked in gate["sources"]:
        if blocked in source:
            return ("source", blocked)

    title = job.get("title") or ""
    for rule, pattern in gate["titles"].items():
        if pattern is None:
            continue
        hit = pattern.search(title)
        if hit:
            return (rule, hit.group(0))

    # Location is the one field where a match is not enough on its own: listings
    # routinely name several sites, and one acceptable site makes the job
    # acceptable. So deny only when nothing allowed appears anywhere in it.
    location = job.get("location") or ""
    if location and gate["loc_deny"] is not None:
        denied = gate["loc_deny"].search(location)
        allowed = gate["loc_allow"].search(location) if gate["loc_allow"] else None
        if denied and not allowed:
            return ("location", denied.group(0))

    return None


def mark_seen(fp):
    with open(SEEN_PATH, "a", encoding="utf-8") as f:
        f.write(fp + "\n")


def build_system_prompt(profile):
    """Ask for observations, never for a verdict.

    The dealbreaker list is deliberately absent. Pasting it in made the model
    retrieve the nearest-matching clause and echo it into `concerns` verbatim —
    "Role is non-technical — title contains 'Software Engineer'" on a software
    engineering job — while its score ignored the clause entirely. Those rules
    now live in the gate and in score_from_classification(), where they are
    applied rather than recited. What is left is short enough for a 4B model to
    hold at once, which is the whole point."""
    skills = ", ".join(profile.get("skills", []))
    prefs  = "\n".join(f"- {p}" for p in profile.get("preferences", []))
    return f"""You read one job posting and report what it says. You do not decide
whether the candidate should apply — something else does that. Report only what the
posting states. If it does not say, use "unclear" or 0 rather than guessing.

CANDIDATE SKILLS (for matched_skills only):
{skills}

WHAT THE CANDIDATE IS LOOKING FOR (context; do not score it):
{prefs}

Fill in each field:

role_family — what the person in this job mainly does:
  software_engineering  writes application, product, or backend code
  security              security engineering, appsec, detection, threat work
  ml_ai                 machine learning, AI, or research engineering
  data                  data engineering, analytics engineering, pipelines
  infra_devops          SRE, DevOps, platform, cloud, or infrastructure
  other_technical       technical but none of the above (hardware, IT, QA, support)
  non_technical         sales, legal, HR, marketing, design, operations, finance

seniority — the level this posting is aimed at:
  intern        internship, co-op, or any temporary/seasonal position
  new_grad      explicitly for graduating students or new graduates
  junior        entry-level, "I", associate, 0-1 years
  mid           2-5 years of experience expected
  senior_plus   senior, staff, principal, lead, or manager
  unclear       the posting does not indicate a level

min_years_experience — smallest number of years of professional experience the
  posting requires. 0 if it asks for none or does not say. Ignore internships
  and coursework; only count required full-time professional experience.

start_timing — when the job starts:
  2027_or_later     starts in 2027 or later, or is for the 2027 graduating class
  2026_or_earlier   starts in 2026 or earlier, or requires graduating by 2026
  unclear           the posting does not say

clearance_required — true only if an ACTIVE US security clearance is required.
  "Must be eligible to obtain" is false.

advanced_degree_required — true only if a Master's or PhD is REQUIRED.
  "Preferred" or "or equivalent experience" is false.

matched_skills — only skills from the list above that the posting actually asks for.

evidence — one short sentence quoted or closely paraphrased from the posting that
  best supports your seniority and experience answers."""


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


def score_from_classification(result, profile, job=None, gate=None):
    """The model's labels -> the CSV row, with the score computed here.

    This is where the rubric actually lives. Keeping it in Python rather than in
    the prompt buys three things the old design could not have: the same labels
    always produce the same score, `concerns` finally describes what moved the
    number instead of being decorative prose, and the weights can be tuned
    against a labelled set without touching the model.

    Hard rejects collapse to scoring.reject_score. They are dealbreakers — no
    amount of skill overlap should lift one back over the threshold, which is
    exactly what an additive-only score would let happen."""
    cfg       = profile.get("scoring", {})
    threshold = profile.get("threshold", 6)
    bases     = cfg.get("role_family_base", {})
    sen_delta = cfg.get("seniority_delta", {})
    start_delta = cfg.get("start_timing_delta", {})

    family    = result.get("role_family", "other_technical")
    seniority = result.get("seniority", "unclear")
    start     = result.get("start_timing", "unclear")
    try:
        years = max(0, int(result.get("min_years_experience", 0)))
    except (TypeError, ValueError):
        years = 0
    skills = [str(s) for s in result.get("matched_skills", []) if str(s).strip()]

    concerns, rejects = [], []

    max_years = cfg.get("max_years_experience", 1)
    if family == "non_technical":
        rejects.append("non-technical role")
    if seniority == "senior_plus":
        rejects.append("senior/staff-level role")
    if seniority == "intern":
        rejects.append("internship or temporary position")
    if years > max_years:
        rejects.append(f"requires {years}+ years experience")
    if start == "2026_or_earlier":
        rejects.append("starts 2026 or earlier")
    if result.get("clearance_required"):
        rejects.append("active security clearance required")
    if result.get("advanced_degree_required"):
        rejects.append("MS/PhD required")

    if rejects:
        score = cfg.get("reject_score", 1)
        concerns = rejects
    else:
        score = bases.get(family, 3)
        score += sen_delta.get(seniority, 0)
        score += start_delta.get(start, 0)

        # Location and focus bonuses are only reachable here — a job that got
        # this far already passed the gate, so a top-tier location is a genuine
        # tiebreak between survivors rather than a way to rescue a bad match.
        location = (job or {}).get("location") or ""
        if gate is not None and gate["loc_allow"] is not None \
                and gate["loc_allow"].search(location):
            score += cfg.get("top_location_bonus", 0)
        if family in ("security", "ml_ai"):
            score += cfg.get("preferred_focus_bonus", 0)
        if len(skills) >= cfg.get("skill_overlap_min", 3):
            score += cfg.get("skill_overlap_bonus", 0)

        if seniority == "unclear":
            concerns.append("level not stated in posting")
        if start == "unclear":
            concerns.append("start date not stated")
        if family == "other_technical":
            concerns.append("technical but outside target role families")

    score = max(0, min(10, score))
    evidence = str(result.get("evidence", "")).strip()
    return {
        "suitable": score >= threshold,
        "score": score,
        "matched_skills": "; ".join(skills),
        "concerns": "; ".join(concerns),
        "reason": f"{family}/{seniority}, {years}y exp, starts {start}."
                  + (f" {evidence}" if evidence else ""),
    }


def append_csv(csv_path, row):
    csv_path = Path(csv_path)
    is_new = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        if _fcntl is not None:
            _fcntl.flock(f, _fcntl.LOCK_EX)
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_ALL)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
        # lock released automatically on file close


def main():
    parser = argparse.ArgumentParser(description="Filter job listings with a local LLM.")
    parser.add_argument("jobs_file", help="JSON file of job listings from your scraper")
    parser.add_argument("--csv", default=None,
                        help="Output CSV path (default: matched_jobs.csv; "
                             "dry runs default to dry_run_results.csv)")
    parser.add_argument("--all", action="store_true",
                        help="Write every job to the CSV, not just suitable ones")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-evaluate jobs even if already seen")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip the LLM call; emit fake scores to a separate CSV "
                             "without touching matched_jobs.csv or seen_jobs.txt")
    parser.add_argument("--no-gate", action="store_true",
                        help="Send every job to the model, bypassing the "
                             "deterministic gate (for comparing the two)")
    args = parser.parse_args()

    # A dry run must never contaminate the real tracker: unless a CSV path was
    # given explicitly, write the fake rows somewhere disposable.
    if args.csv is None:
        args.csv = str(DATA_DIR / ("dry_run_results.csv" if args.dry_run
                                   else "matched_jobs.csv"))

    config = load_config()
    profile = config.get("profile", {})
    system_prompt = build_system_prompt(profile)
    gate = compile_gate(profile.get("gate", {}))
    if args.no_gate:
        gate["enabled"] = False
    jobs = load_jobs(args.jobs_file)
    seen = set() if args.rescore else load_seen()

    total, kept, skipped, errors, gated = 0, 0, 0, 0, 0
    gate_rules = {}
    consecutive_errors = 0
    started = time.time()

    for job in jobs:
        fp = job_fingerprint(job)
        if fp in seen:
            skipped += 1
            continue
        total += 1
        title = job.get("title", "(no title)")
        print(f"[{total}] {title} @ {job.get('company','?')} ... ", end="", flush=True)

        # Gate first: no model call, no timeout risk, no context spent.
        verdict = gate_job(job, gate)
        if verdict:
            rule, detail = verdict
            gated += 1
            gate_rules[rule] = gate_rules.get(rule, 0) + 1
            print(f"gated [{rule}: {detail}]")
            if args.all:
                append_csv(args.csv, {
                    "date_processed": datetime.now(timezone.utc).strftime(TS_FORMAT),
                    "title": job.get("title", ""), "company": job.get("company", ""),
                    "location": job.get("location", ""), "salary": job.get("salary", ""),
                    "url": job.get("url", ""), "source": job.get("source", ""),
                    "suitable": False, "score": 0, "matched_skills": "",
                    "concerns": f"gated: {rule} ({detail})",
                    "reason": "Rejected by the deterministic gate; no model call.",
                })
                kept += 1      # rows written, not rows matched — --all means both
            # Gated jobs are marked seen like scored ones: the decision is
            # reproducible from config, so re-deciding it every cycle would burn
            # the scrape's whole gate pass for an identical answer. Widening the
            # gate later needs --rescore, same as a prompt change does.
            if not args.rescore and not args.dry_run:
                mark_seen(fp)
            continue

        err = None
        try:
            if args.dry_run:
                result = dict(DRY_RUN_RESULT)
            else:
                result = call_ollama(config, system_prompt, build_user_prompt(job))
            r = score_from_classification(result, profile, job, gate)
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            err = (f"OLLAMA OFFLINE — start Ollama and retry ({reason})"
                   if "refused" in reason.lower()
                   else f"OLLAMA NETWORK ERROR ({reason})")
        except TimeoutError:
            err = (f"OLLAMA TIMEOUT — model too slow or "
                   f"num_ctx={config.get('num_ctx', 4096)} too large for this job")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            err = f"BAD RESPONSE ({e}) — skipping"

        if err:
            errors += 1
            consecutive_errors += 1
            print(err)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"\n{consecutive_errors} failures in a row — Ollama looks "
                      f"down or wedged; aborting so the rest can be retried later.")
                break
            continue
        consecutive_errors = 0

        verdict = "MATCH" if r["suitable"] else "no"
        print(f"score {r['score']}/10 -> {verdict}")

        if r["suitable"] or args.all:
            append_csv(args.csv, {
                "date_processed": datetime.now(timezone.utc).strftime(TS_FORMAT),
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "salary": job.get("salary", ""),
                "url": job.get("url", ""),
                "source": job.get("source", ""),
                **r,
            })
            kept += 1

        # Dry runs produce fake scores — recording them as seen would stop the
        # real model from ever evaluating these jobs.
        if not args.rescore and not args.dry_run:
            mark_seen(fp)

    elapsed = time.time() - started
    scored = total - gated
    print(f"\nDone. Saw {total}, gated {gated} without a model call, scored "
          f"{scored}, wrote {kept} to {args.csv}, skipped {skipped} "
          f"already-seen, {errors} errors, {elapsed:.0f}s elapsed.")
    if gate_rules:
        breakdown = ", ".join(f"{rule} {n}" for rule, n
                              in sorted(gate_rules.items(), key=lambda kv: -kv[1]))
        print(f"Gate: {breakdown}")

    # Exit nonzero when the run was cut short by consecutive failures, or when
    # every attempted evaluation failed — either way Ollama is unusable, and
    # the orchestrator should treat the filter phase as failed and retry it
    # later instead of advancing to sync. Jobs already scored stay in
    # seen_jobs.txt, so the retry only evaluates what's left.
    # Compared against `scored`, not `total`: gated jobs never touch Ollama, so
    # counting them here would mask a dead model behind a big gate pass.
    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS or (scored and errors == scored):
        sys.exit(f"Ollama unusable ({errors}/{scored} attempted jobs failed) — "
                 f"exiting nonzero so the pipeline retries this phase.")


if __name__ == "__main__":
    main()
