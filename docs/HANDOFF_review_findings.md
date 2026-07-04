# Handoff: code-review findings — FIXED 2026-07-03

## Review #3 (consolidation + review-2 fixes diff) — fixes applied 2026-07-03

A third review caught two bugs in review #2's own fixes, plus small gaps.
All addressed:

1. **held>=20 warning counted a standing total (CONFIRMED)** — held includes
   every hand-deletion ever (they stay in the cumulative CSV), so it would
   eventually warn forever and its advice would resurrect deliberate
   deletions. Now delta-based: previous count persisted in
   `data/held_count.txt` (gitignored); warns only when held JUMPS ≥20 in one
   run — the actual backup-rollback signature.
2. **jobs_left URL-intersection counting (CONFIRMED)** — four mechanisms
   could still yield negative/wrong counts (duplicate CSV rows, fingerprint
   vs URL keying, URL-less matches, whitespace URLs). Replaced with
   fingerprint-keyed `count_scrape_matches(rows, jobs)` shared from
   filter_jobs.py (NOT matches.py — that import direction would cycle);
   pipeline_stats now uses the same helper (its copy shared 3 of 4 flaws).
3. **time-sync.target was a no-op without systemd-time-wait-sync
   (CONFIRMED)** — README's Pi setup now includes
   `sudo systemctl enable systemd-time-wait-sync.service`.
4. **Watermark timestamp format was an uncoupled literal (CONFIRMED)** —
   `matches.TS_FORMAT` now owns "%Y-%m-%d %H:%M" with the lexical-ordering
   contract documented; filter_jobs writes with it; export's comparator
   comment references it.
5. **CSV_COLUMNS alias dropped (CONFIRMED)** — filter_jobs uses CSV_FIELDS
   directly.
6. **paths.py docstring (CONFIRMED)** — now "directory layout and shared
   file paths".
7. **jobs_left output semantics (CONFIRMED, disclosure)** — "-> Matches:" is
   scrape-scoped since the rewrite; all-time total is its own labeled line.
8. **Not done (recorded options):** folding jobs_left into
   `pipeline_stats --brief` (recurring recommendation; drift surface now
   mostly dead via the shared helper — fold if it gets flagged again);
   threading the watermark through export's stdout instead of the .pending
   file (future hardening; current design audited safe); the
   EXPORT_MARK-alias grep-split (cut by the cap; cosmetic).

---

## Review #2 (watermark + cleanup diff) — small fixes applied 2026-07-03

A second multi-agent review of the watermark commit + consolidation cleanup
found 10 issues (2 confirmed bugs at the watermark's seams, the rest
edge-case disclosures/cleanups). Small fixes applied:

1. **Stale .pending promotion (CONFIRMED)** — export now unlinks
   `export_mark.pending` at the very start of every run (before any early
   return), so only a pending written by that run can be promoted.
2. **Bootstrap misclassification (CONFIRMED)** — inherent to the scalar
   bootstrap; mitigations: held-count printed every run + README documents
   the one-time "newest deleted row returns once" edge.
3. **Backup-rollback rows held (PLAUSIBLE)** — export warns loudly when one
   run holds ≥ 20 rows; README documents deleting `data/export_mark.txt` as
   the recovery knob.
4. **Pre-NTP clock skew (PLAUSIBLE)** — jobfilter.service now orders
   After/Wants `time-sync.target` (note: with plain systemd-timesyncd this
   improves but doesn't guarantee ordering; enable
   `systemd-time-wait-sync.service` on the Pi for a hard guarantee).
5. **Same-minute `ts <= mark` collision (PLAUSIBLE)** — accepted tradeoff
   (`<` would resurrect same-minute deletions instead); documented here.
   A per-key exported ledger would eliminate findings 2–5 as a class if this
   ever matters in practice.
6. **README overpromise (CONFIRMED)** — "Deletions are respected" bullet now
   lists the edge cases + recovery knob.
7. **MARK constants duplicated (CONFIRMED)** — moved to paths.py
   (`EXPORT_MARK_PATH` / `EXPORT_MARK_PENDING`); both modules import them.
8. **jobs_left issues (CONFIRMED)** — added `__main__` guard; "Matches" now
   scrape-scoped (negative "No match" impossible) with a separate all-time
   line.
9. **Blank-timestamp rows bypass watermark (PLAUSIBLE)** — accepted
   limitation (only reachable via hand-edited CSV lines); documented in
   README's edge cases.
10. **pipeline_stats semantic tightening (PLAUSIBLE)** — noted: rows with no
    title AND no url no longer count in the distribution sections (benign —
    such rows identify no job). Also dropped the dead `SCRIPTS` constant.

Refuted by verification (do NOT "fix"): pruned keys advancing the watermark
past unconfirmed rows (safe by the append→push→commit ordering); gating the
tiny .pending write for efficiency (dwarfed by the unconditional xlsx save,
and conflicts with fix #1).

---

**Status:** All 10 findings from the 2026-07-02 review are fixed, tested where
runnable on the dev box, and sitting **uncommitted** in the working tree along
with the original change set. Remaining items are the optional cleanups and
the Pi smoke test listed at the bottom.

## What was fixed (per finding)

1. **Catch-up resumed at leftover "sync" phase** — `run_pipeline` now resets
   `state["phase"] = "idle"` (+ save) at pipeline completion, so back-to-back
   catch-up runs start a full new cycle. Shutdown/failure early-returns keep
   the checkpoint. *Tested: stubbed pipeline; phase is "idle" after completion.*
2. **All-Ollama-failed exit crash-looped systemd** — `run_pipeline` wraps the
   phase dispatch in `try/except RuntimeError`: a failed phase logs, keeps the
   checkpoint, and returns. `main()`'s loop resumes it after
   `phase_retry_interval` (new local.json key, default 900s) via
   `interruptible_sleep`. Unexpected exceptions still crash → systemd restart.
   *Tested: failing phase contained, checkpoint kept, resume re-ran only the
   failed phase onward.*
3. **`--apply --local` prune silently undone** — prints a prominent TRANSIENT
   warning when targeting the live workbook, and records no suppress keys in
   local mode (keys for rows never authoritatively removed were lies).
4. **Workday parser drift** — orchestrator now imports
   `scraper._parse_workday_slug`; the second copy is gone.
   *Tested: plain, `|tenant`, and double-slash slugs.*
5. **Wedged Ollama burned 300s/job** — `filter_jobs.py` aborts after
   `MAX_CONSECUTIVE_ERRORS = 3` failures in a row and exits nonzero; success
   resets the counter. *Tested against a dead port: aborted after 3, exit 1,
   nothing marked seen.*
6. **Verify retry outage amplification** — consecutive-failure circuit
   breaker: after 3 unreachable in a row, no more retries until a probe
   succeeds again.
7. **Dry-run previewed the stale local copy** — the dry run now pulls the
   laptop's workbook to a temp file (zero side effects, deleted after) and
   falls back to the local copy with a warning if unreachable. A custom
   workbook path implies `--local`.
8. **Pruned keys recorded before push confirmed** — `append_pruned_keys` now
   runs only after a successful push; a failed push records nothing and says
   so ("nothing is inconsistent — re-run --apply").
9. **Empty `scrape_hours_local` crash** — orchestrator refuses to start with
   an empty schedule (clear `sys.exit` at startup). *Tested.*
10. **Duplicated scp/local.json plumbing** — new `scripts/remote.py`
    (`LOCAL_JSON`, `load_local_config`, `remote_base`, `scp`) used by both the
    orchestrator and the prune CLI. Orchestrator builds `REMOTE_BASE` at
    startup so missing keys still fail fast.

Also done (from the cut-cleanup list): prune CLI imports moved to module top;
`append_pruned_keys` moved into `prune_workbook.py` (export keeps
`load_pruned_keys`, its only reader). README + `config/local.example.json`
updated for all of the above (`phase_retry_interval`, prune semantics,
fail-fast filter, in-place phase retry bullet).

## Sync watermark (added after the fixes, user request)

**Hand-deleted workbook rows now stay deleted — as standard pipeline
behavior.** `export_workbook.py` keeps a sync watermark
(`data/export_mark.txt`): the `date_processed` of the newest CSV row in a
batch the laptop confirmed receiving. A CSV row older than the watermark
that's missing from the pulled workbook was deleted by the user and is never
re-added; newer rows are new matches and always append. Mechanics:
- Export writes the candidate to `export_mark.pending`; the orchestrator
  promotes it to `export_mark.txt` only after a successful push — so a failed
  push never strands unexported rows behind the watermark.
- No watermark file yet (first run after deploy): bootstrapped as the newest
  timestamp among CSV rows already in the workbook or pruned-keys list, so
  existing hand-deletions are honored immediately.
- A missing workbook = full rebuild: watermark ignored (pruned keys still
  suppress), so a lost tracker regenerates completely from the CSV.
- openpyxl was installed on the dev box; tested end-to-end: fresh build,
  hand-delete + new match, bootstrap, and rebuild all behave. Prune CLI
  dry-run also smoke-tested (clean run, file untouched).

## Cleanup items — DONE 2026-07-03

- **Shared CSV reader:** new `scripts/matches.py` (stdlib-only, so
  pipeline_stats stays openpyxl-free) owns `CSV_FIELDS`, the header-tolerant
  `read_matches()`, and `month_day()`. Consumers: filter_jobs (writer schema),
  export_workbook, pipeline_stats, jobs_left (its naive line-count reader is
  gone). pipeline_stats and jobs_left also now import `job_fingerprint` /
  `load_seen` from filter_jobs instead of re-implementing them.
- **Shared date helper:** `matches.month_day()` replaces both `%-d`
  workaround copies.
- **Slot-helper merge:** `_today_slots(now)` + 2-line `next_run_time` /
  `most_recent_slot`; frozen-clock tested on all boundaries (between slots,
  before first, after last, exactly on a slot).
- **Do NOT "fix" (unchanged, deliberate):** the double `save_state` in
  `run_pipeline` (persistence guarantee) and Windows scp drive-letter
  handling (non-issue).

## Watchlist sanity checks (carried over, still pending user)

- `lever:convergentresearch` — assumes "Convergent" = Convergent Research
- `greenhouse:capstoneinvestmentadvisors` — assumes "Capstone" = Capstone
  Investment Advisors

## Deployment notes

- First restart after deploying runs one catch-up cycle immediately (old state
  files lack `last_cycle_started`). Harmless; everything dedupes.
- Optionally add `"phase_retry_interval": 900` to the Pi's `config/local.json`
  (defaults to 900 if absent).
- `sudo cp jobfilter.logrotate /etc/logrotate.d/jobfilter` (adjust path inside).
- openpyxl paths (`export_workbook`, prune CLI) compile-checked but not
  executed on the dev box — on the Pi, run `python3 scripts/prune_workbook.py`
  (dry run) once as a smoke test.
