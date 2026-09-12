# Briefing: the three review bugs (ground truth)

Purpose: context for explaining the three defects an AI code review found in
this repo, accurately enough to speak about them under questioning.

Everything below is verified against the repo — `docs/HANDOFF_review_findings.md`,
commit `b6b26f2` ("Deduplicate shared helpers, harden watermark and stats",
2026-07-03), and the current working tree. Where a number in the draft script
was wrong, it is corrected here.

---

## 0. Facts to fill the blanks

| Claim in the script | Verified value |
|---|---|
| "[how many lines?] of Python" | **~6,700** — 6,724 across 26 files; 6,289 in `scripts/`, 435 in `web/`. ~7,500 with HTML/CSS. |
| "387 company career boards" | **322**. That's the resolved watchlist in `config/scraper_config.json`. `config/companies.txt` has 546 requested names; the ones the `detect` phase can't resolve land in `watchlist_misses.txt`. Say **"a few hundred — about 320"**. |
| "seven ATS platforms" | **Correct, exactly 7**: greenhouse (172), ashby (72), workday (31), lever (20), workable (15), smartrecruiters (9), oracle (3). |
| "wakes up twice a day" | Correct — `scrape_hours_local: [6, 13]`. |
| "local LLM with schema-constrained output" | Correct — `gemma4:26b` via Ollama, `temperature 0.1`, `num_ctx 8192`, request carries `"format": RESULT_SCHEMA`. |
| Total sources | The watchlist is 1 of **12** sources; the others are RemoteOK, 4x WeWorkRemotely RSS, Remotive, Arbeitnow, HN "Who's Hiring", and 3 community GitHub new-grad lists. |

**One structural fact worth knowing before you speak:** there were **three review
rounds**, not one. Review #3's job was to review the *fixes* from review #2 — and
it found that two of them were wrong. That matters; see section 4.

---

## 1. The warning that would never stop

**Where:** `export_workbook.py`, in the now-removed laptop-sync path. The code is
gone from HEAD (removed in `8bed350`, when the SQLite DB became the single system
of record) but it lives in `b6b26f2`.

**The setup.** The pipeline appended new match rows onto an Excel tracker that was
pulled from a laptop. If you hand-deleted a row from that tracker, the pipeline
needed to not re-add it. It decided that with a **sync watermark**: a CSV row
older than the watermark that is missing from the pulled workbook must have been
deleted deliberately, so it stays deleted. Each such row incremented a counter
called `held`.

**The bug.** There was a warning: if `held >= 20`, print a loud message telling
the user their workbook may have been restored from an old backup and they should
delete the mark file to recover rows.

`held` is a **standing total, not a per-run event.** The CSV is cumulative — it
never forgets a row. So every hand-deletion you have ever made stays counted in
`held` on every future run, forever. Once your lifetime deletions crossed 20 —
which for normal use is just a few weeks of pruning jobs you don't want — the
warning fired on every single run and never stopped.

**Why that's a real defect, not cosmetic.** Two separate harms:

1. *Alert fatigue.* A warning that is always on carries zero information. You
   train yourself to scroll past it, so the day it means something you miss it.
   This runs unattended twice a day; log noise is the only channel it has.
2. *The advice was actively destructive.* The warning told you to delete
   `data/export_mark.txt`. Doing that discards the watermark, and every row you
   had deliberately hand-deleted gets **resurrected** into your tracker. So the
   false alarm didn't just cry wolf — it recommended a data-destroying remedy for
   a problem that wasn't happening.

**The fix.** Make it delta-based. Persist last run's count in
`data/held_count.txt` (gitignored), and warn only when `held - prev_held >= 20` —
i.e. when 20+ rows are newly held *in one run*. That jump is the actual signature
of a workbook restored from an old backup; a standing total is not.

```python
prev_held = 0
try:
    prev_held = int(HELD_COUNT_PATH.read_text(encoding="utf-8").strip() or 0)
except (OSError, ValueError):
    pass
HELD_COUNT_PATH.write_text(str(held), encoding="utf-8")
if held - prev_held >= 20:
    print(f"WARNING: held rows jumped {prev_held} -> {held} in one run. ...")
```

**Say it like this:** "I had a warning that fired when 20 or more rows were being
held back as deliberately deleted. But that count was a standing lifetime total,
not a per-run delta — the CSV is cumulative, so every deletion I'd ever made kept
counting. Once I crossed twenty it would warn on every run forever. And the
remedy it suggested was to delete the watermark file, which would have resurrected
every row I'd deliberately deleted. The fix was to persist the previous count and
alert on the jump, because a sudden jump is the real signature of the failure I
was actually trying to catch — a workbook restored from an old backup."

**If asked "why not just raise the threshold?"** Because the threshold isn't the
problem — the quantity is. Any fixed threshold on a monotonically increasing
counter fires eventually. You have to change what you're measuring, not where you
measure it.

---

## 2. Negative counts from mismatched keying

**Where:** `scripts/jobs_left.py` and `scripts/pipeline_stats.py`; the fix is
`count_scrape_matches()` in `scripts/filter_jobs.py:132-142`.

`jobs_left.py` is a 2-second progress check: of the jobs in the current scrape,
how many are scored, how many matched, how many remain.

**The original bug — a scope mismatch.** The old code was:

```python
matches        = sum(1 for _ in open(csv_path)) - 1   # every line in the CSV, ever
scored_current = len(jobs) - len(unseen)              # this scrape only
no_match       = scored_current - matches
```

`matches` counted **every row in the cumulative all-time CSV** by counting file
lines. `scored_current` counted **only the current scrape**. Subtracting an
all-time number from a per-scrape number is meaningless, and as soon as the CSV
had more lifetime matches than the current scrape had scored jobs, `No match`
printed a **negative number**.

(The line count was independently sloppy too — the `- 1` assumed a header row
that `filter_jobs` doesn't write, and any embedded newline in a `reason` or
`concerns` field counts as an extra match.)

**The second bug — right scope, wrong key.** The first fix scoped `matches` to
the current scrape by intersecting **URL sets** between the CSV rows and the
scraped jobs. Correct scope, but the next review round found four ways it still
produced wrong or negative counts:

1. **Duplicate CSV rows** for one job each counted separately.
2. **Fingerprint vs. URL keying.** "Scored" is tracked by `job_fingerprint()` —
   which uses the URL *if present* and otherwise falls back to `title|company`.
   Counting matches by raw URL meant the two halves of the subtraction were keyed
   on different identities.
3. **URL-less jobs** — some sources (HN "Who's Hiring", some GitHub lists) yield
   postings with no URL. They get fingerprints, so they can be *scored*, but they
   have no URL, so they could never be counted as *matched*.
4. **Whitespace-only URLs** collapsed to the same empty key and cross-matched
   unrelated jobs.

**Why the negative number is the interesting part.** A count of things cannot be
negative. It isn't a slightly-wrong number; it's a value the domain says is
impossible. That tells you the error is in the **model**, not the arithmetic —
you assumed two quantities described the same set of things and they didn't. And
crucially: the sign is the only reason it was ever visible. If the mismatch had
gone the other way you'd have gotten a plausible, wrong, silently-accepted number
forever.

**The fix.** One helper, keyed by the same fingerprint used everywhere else,
shared by both consumers:

```python
def count_scrape_matches(rows, jobs):
    row_fps = {job_fingerprint(r) for r in rows}
    return len({job_fingerprint(j) for j in jobs} & row_fps)
```

Sets collapse duplicates; the fingerprint fallback keeps URL-less jobs countable;
both sides of the subtraction now use one identity. It lives in `filter_jobs.py`
rather than the shared `matches.py` module **because `matches.py` importing
`job_fingerprint` back out of `filter_jobs` would create an import cycle** — a
small detail, but it's the kind of thing that shows you placed the code
deliberately.

The same review found `pipeline_stats.py` had its own copy of this logic sharing
3 of the 4 flaws — so the fix also deduplicated the fingerprint, the seen-list
reader and the CSV reader into shared helpers, which is what the commit title
refers to.

**Say it like this:** "I was printing a negative count of non-matching jobs. The
cause was that two halves of one subtraction were scoped and keyed differently —
one side counted all-time matches by raw URL, the other counted this scrape's
scored jobs by fingerprint, where a fingerprint falls back to title-plus-company
when a posting has no URL. A count of things can't be negative, so the negative
sign told me the data model was wrong, not the arithmetic. I replaced both with a
single fingerprint-keyed set intersection shared by every consumer."

---

## 3. Clock skew on a Raspberry Pi — the strongest one

**Where:** `jobfilter.service:4-8`, plus `README.md:177`.

**The hardware fact.** A Raspberry Pi has **no battery-backed real-time clock.** A
laptop or server has a coin cell that keeps time while powered off; the Pi
doesn't. At boot it has no idea what time it is — it restores a saved timestamp
from last shutdown, or starts from something arbitrary. It only learns the true
time once the network is up and it syncs over NTP.

**How that corrupted data.** systemd was starting the daemon early in boot,
potentially before that sync landed. So the daemon would score jobs and stamp
`date_processed` with a **pre-sync, stale clock** — a time in the past. Then NTP
corrects the clock and it jumps forward.

Now the specific damage, which is the part worth saying out loud: those rows have
timestamps that land **behind the sync watermark from section 1**. And the
watermark's rule is "a row older than the watermark that isn't in the workbook was
deleted by hand — never re-add it." So legitimately new job matches got
permanently misclassified as rows I had deliberately deleted, and **silently never
reached my tracker at all.** Not misordered — dropped.

**Why it's a nasty class of bug.** It only manifests after a reboot, which on a
home Pi means a power cut. It's intermittent, environment-dependent, invisible in
normal operation, and its symptom is *absence* — a job that never shows up doesn't
announce itself. This is exactly the bug a solo developer ships and then loses a
weekend to six months later.

**The fix — and this is the detail worth being precise about.** The first fix was
to add ordering on `time-sync.target` in the unit file:

```ini
After=network-online.target ollama.service time-sync.target
Wants=network-online.target ollama.service time-sync.target
```

**The follow-up review found that this alone is a no-op.** `time-sync.target` is a
passive synchronization point — with plain `systemd-timesyncd`, nothing actually
*delays* the target until the clock is correct, so ordering after it buys you
nothing. The unit that provides the hard guarantee is
`systemd-time-wait-sync.service`, which **blocks until the clock is actually
synchronized** and pulls the target in behind it. It has to be explicitly enabled:

```bash
sudo systemctl enable systemd-time-wait-sync.service
```

Both halves are needed: the service must order after `time-sync.target`, *and*
`systemd-time-wait-sync.service` must be enabled to make that target mean
anything. That's now in the README's Pi setup.

**Say it like this** (slowly — `systemd-time-wait-sync`): "The Pi has no
battery-backed real-time clock, so on boot its clock is wrong until it syncs over
NTP. My daemon was starting before that sync and stamping records with a stale
time. Because those timestamps landed behind my sync watermark, brand-new job
matches were being misread as rows I'd deliberately deleted and silently dropped.
I ordered the unit after `time-sync.target` — and then a follow-up review caught
that that's a no-op on its own, because nothing delays that target unless you
enable `systemd-time-wait-sync.service`, which is the unit that actually blocks
until the clock is trustworthy."

**Likely follow-up: "how would you have caught this without the review?"** Honest
answer: probably by noticing a job I knew existed never appeared, months later.
The realistic detection is a sanity check in the daemon — refuse to write a
`date_processed` older than the newest one already on disk, and log loudly if the
clock appears to move backwards. That's a good thing to volunteer; it shows you
thought past the patch.

---

## 4. The point that protects the whole answer

The draft's risk is that it reads as "AI did my debugging." The strongest
available defence is a fact already in the record, and it's underused:

**Two of the three fixes were themselves wrong, and the next review round caught
them.**

- The clock-skew fix (`time-sync.target`) was a **no-op** until
  `systemd-time-wait-sync.service` was enabled.
- The counting fix (URL-set intersection) still had **four** paths to a wrong
  count until it was re-keyed on fingerprints.

That's the whole argument in one line: the agent's first answer was confidently
wrong twice, and finding that out took another structured pass plus your judgment
about which findings were real. Use it.

The review also **refuted** some of its own earlier findings on verification —
e.g. a claim that pruned keys could advance the watermark past unconfirmed rows,
which turned out to be safe by the append-then-push-then-commit ordering. Those
went into the handoff explicitly marked "do NOT fix," so a later pass wouldn't
re-raise them.

---

## 5. "Some weren't worth fixing" — pick one of these

All are verbatim from the handoff. The first is the best answer because it's a
tradeoff, not a shrug:

- **Same-minute watermark collision.** Timestamps are minute-resolution
  (`%Y-%m-%d %H:%M`), and the comparison is `ts <= mark`. A row written in the
  same minute as the watermark is treated as already-handled. Changing `<=` to `<`
  fixes that, but then a row deliberately deleted in that same minute gets
  resurrected. **Both options are wrong in one direction; I chose the one whose
  failure mode is a missing row over the one whose failure mode is silently
  undoing a user's deletion.** Recorded as an accepted tradeoff, with a note that
  a per-key exported ledger would eliminate that whole class if it ever bit in
  practice.

- **Blank-timestamp rows bypass the watermark** — only reachable by hand-editing
  the CSV. Documented as a limitation rather than defended against.

- **The double `save_state()` in `run_pipeline`** — flagged as redundant; it's a
  deliberate persistence guarantee. Marked "do not fix" so it wouldn't get
  re-flagged.

- **Folding `jobs_left` into `pipeline_stats --brief`** — a recurring
  recommendation. Declined: once the shared helper removed the duplicated logic,
  the actual drift risk was gone, and the remaining merge was churn. Recorded with
  a trigger for revisiting it ("fold if it gets flagged again").

The meta-point to land: findings got triaged into **fix / accepted tradeoff /
refuted**, and the refuted and deliberate ones were written down *as* refuted, so
the next review round wouldn't resurface them. That's the part that's actually
engineering judgment rather than patch application.

---

## 6. Corrections to make in the script before recording

1. **"387 company career boards" -> "about 320."** 322 is the verified number.
   Don't state a precise figure you can't defend; "a few hundred" is safer.
2. **Fill the line count: ~6,700 lines of Python.**
3. **Bug 3's stated symptom is wrong.** The draft says "records were written out
   of order." The real consequence is worse and more specific: new matches fell
   behind the sync watermark and were **misclassified as deliberate deletions,
   then silently dropped.** Ordering is the mechanism; data loss is the effect.
   Say the effect.
4. **Bug 3's fix is two parts,** not one — the unit ordering *and* enabling
   `systemd-time-wait-sync.service`, because the ordering alone is a no-op.
5. **Consider adding one sentence about the review rounds** (section 4). It's the
   cheapest possible insurance against the "AI did your debugging" read.

---

## Appendix — where to verify each claim

```bash
git show b6b26f2 -- jobfilter.service          # clock-skew fix
git show b6b26f2 -- scripts/jobs_left.py       # counting fix, before/after
git show b6b26f2 -- scripts/export_workbook.py # held-count delta fix
sed -n '1,30p' docs/HANDOFF_review_findings.md # review #3 findings 1-3
sed -n '132,142p' scripts/filter_jobs.py       # count_scrape_matches
find . -name '*.py' -not -path './.git/*' | xargs wc -l | tail -1
```

Note: the section-1 held-count code and the watermark are **no longer in HEAD** —
they were removed in `8bed350` when the SQLite DB became the single system of
record, since a soft-deleted DB row simply isn't in `live_jobs` and there's
nothing to infer. If asked, that's a good answer: the durable fix was deleting the
inference, not tuning the heuristic.
