# Handoff: code-review findings — FIXED 2026-07-03

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

## Still open (optional, low priority)

- **Shared CSV reader:** the quoted-header sniff + `CSV_FIELDS` are still
  duplicated between `export_workbook.read_matches` and
  `pipeline_stats.load_matches` (and `jobs_left.py:16` naively assumes a
  header). Blocker for the naive merge: pipeline_stats deliberately has no
  openpyxl dependency, so the shared reader needs its own small module.
- **Shared date helper:** `f"{d:%b} {d.day}"` still in two places (same
  openpyxl-dependency consideration).
- **`most_recent_slot` vs `next_run_time` merge:** verifier judged a merged
  helper possibly denser, not clearly simpler — take or leave.
- **Do NOT "fix":** the double `save_state` in `run_pipeline` (deliberate
  persistence guarantee) and Windows scp drive-letter handling (non-issue).

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
