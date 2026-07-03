# Handoff: resolving the remaining watchlist companies

**Goal:** get as many of the still-uncovered companies in `config/companies.txt`
onto a *working, identity-verified* job-board entry in the watchlist so the pipeline
scrapes them.

**Status at handoff:** 363 of 476 companies covered (watchlist = 364 entries).
**113 remain uncovered** (listed at the bottom).

> **UPDATE 2026-07-01:** 386 of 478 covered (watchlist = 387 entries; companies.txt grew
> by 2 after splitting the mangled lines `AppDynamicsWEX` and `Zus HealthWorkhelio`).
> 23 companies were resolved and added (verified): Arcesium, Auzmor, Capstone, Convergent,
> Credo AI, Crowe, dbt Labs, Five Rings, Harness, Headspace, Hugging Face, ICF,
> Integral Ad Science, KnitWell Group, NBA, Nuvei, OneStream, Pulumi, SoundCloud, Ubie,
> WEX, Weights and Biases (via CoreWeave's board), Zus Health.
> Everything else on the remaining-92 list has been investigated and recorded in
> `data/watchlist_unsupported.txt` (unsupported ATS, custom portal, acquired/defunct, or
> no ATS found after probing all 6 single-slug platforms) — the remaining pool is
> exhausted short of the companies changing ATS. Two entries need a user sanity-check:
> `lever:convergentresearch` (assumes "Convergent" = Convergent Research) and
> `greenhouse:capstoneinvestmentadvisors` (assumes "Capstone" = Capstone Investment
> Advisors).

---

## How the watchlist works (read first)

The scraper pulls each company's jobs directly from its ATS API. A watchlist entry is:

```json
{ "platform": "greenhouse", "slug": "datadog", "label": "Datadog" }
```

**Supported platforms** and what the `slug` is:

| platform | slug format | example |
|---|---|---|
| `greenhouse` | board slug | `andurilindustries` |
| `lever` | board slug | `anyscale` |
| `ashby` | board slug | `cognition` |
| `smartrecruiters` | company id | `NBCUniversal3` |
| `workable` | account slug | `metomic` |
| `recruitee` | subdomain | `acme` |
| `workday` | `host/site` | `bitsight.wd1.myworkdayjobs.com/Bitsight` |
| `oracle` | `host/site` | `jpmc.fa.oraclecloud.com/CX_1001` |

Anything **not** on one of these (Gem, Comeet, Avature, iCIMS, Taleo, BrassRing,
custom/vanity sites) is **out of scope** — there is no connector for it. Record it as
unreachable and move on; don't sink time into it.

---

## Tools (all in `scripts/`, run with the Anaconda python)

Interpreter: `"/c/Users/lbrug/anaconda3/python.exe"` (the default `python3` is broken).

- **`detect_platforms.py`** — auto-probes Greenhouse/Lever/Ashby/SmartRecruiters/
  Workable/Recruitee for a list of company names. Good first pass for the
  "modern tech startup" companies. Writes `data/watchlist_found.json`.
  `python scripts/detect_platforms.py config/companies.txt`
- **`detect_workday.py`** — resolve a careers/Workday URL → `host/site`, verified.
  `python scripts/detect_workday.py https://www.bitsight.com/careers`
  `python scripts/detect_workday.py --batch file.txt`  (lines: `Label | url`)
- **`detect_oracle.py`** — same, for Oracle Recruiting Cloud (`oraclecloud.com`).
- **`verify_watchlist.py`** — spot-check any entries you add (prints the board's real
  name + sample jobs so you can confirm identity). Run it on your candidates before
  trusting them: `python scripts/verify_watchlist.py mycandidates.json`

---

## The method (per company)

1. **Find the careers page.** Try `https://www.<company>.com/careers`, then
   `careers.<company>.com` / `jobs.<company>.com`, then a web search
   `"<company>" careers` .
2. **Identify the ATS.** Open a *job posting* and read the URL, or view the careers
   page HTML for a link to one of: `boards.greenhouse.io` / `job-boards.greenhouse.io`,
   `jobs.lever.co`, `jobs.ashbyhq.com`, `careers.smartrecruiters.com`,
   `apply.workable.com`, `*.recruitee.com`, `*.myworkdayjobs.com`, `*.oraclecloud.com`.
   - Many big-company careers pages are **JavaScript-rendered** and won't show the ATS
     link in static HTML — in that case a targeted web search is more reliable, e.g.
     `"<company>" myworkdayjobs.com` or `"<company>" oraclecloud CandidateExperience`.
3. **Get the slug** from the URL (see table above).
4. **VERIFY IDENTITY** (this is the whole game — see pitfalls). The board's real name /
   sample job titles must actually match the company.
5. **Add it** to *both* files (see next section).

---

## How to add a resolved company

The **live pipeline reads `config/scraper_config.json`**, not `watchlist_found.json`.
Add the entry to **both** (dedupe by `platform`+`slug`):

- `data/watchlist_found.json` — the audited master (staging).
- `config/scraper_config.json` → the `sources` entry with `"type": "watchlist"` →
  its `companies` array.

Write both files as **UTF-8** (`encoding="utf-8"`, `ensure_ascii=False`). Then the
`verify` phase picks it up on the next cycle. No code changes needed — the connectors
already exist for all 8 platforms.

> Note: the orchestrator's auto-`detect` phase only resolves the six single-slug
> platforms from `companies.txt`. **Workday and Oracle are never auto-detected** — they
> must be added by hand via `detect_workday.py` / `detect_oracle.py`.

---

## Pitfalls learned this session (don't repeat these)

1. **SmartRecruiters false positives.** Its `/postings` API returns `200 {totalFound:0}`
   for *any* slug — a bogus slug looks identical to a real empty board. Only trust an SR
   entry if `totalFound > 0` **or** `careers.smartrecruiters.com/<slug>` renders a real
   company page (has an `og:title`; a bad slug shows the generic "SmartRecruiters Job
   Search" page). ~152 old entries were bogus because of this.
2. **Slug collisions.** A board existing at a guessed slug ≠ it's the right company
   (`greenhouse:general` was "General Interest", not General Dynamics; `greenhouse:new`
   was "Sonja Inc."). Always verify the board's `name` / job content matches.
3. **Greenhouse API is flaky** from this machine — a single probe can 404 a live board.
   Retry once before concluding a Greenhouse board is dead.
4. **Workday tenants are often abbreviations** (`ngc` = Northrop Grumman, `gdit` =
   General Dynamics) and use odd datacenters (`wd501`, `wd12`). Get the exact URL.
5. **Oracle shared pods:** on `fa-ext…saasfaprod1.fa.ocs.oraclecloud.com`, many companies
   share one host and the `CX_####` site number is the identity — verify by job content.

## Keep empty boards

**Do not require jobs > 0.** The user (a May 2027 grad) wants boards that are *valid but
currently empty* kept in the watchlist — they light up when the company posts fall/new-grad
roles. The distinction is real-vs-bogus, not job count. (Workday: a real tenant returns
`200` + `jobPostings` even at 0; bogus → 404/422. Oracle: real host returns items with
`TotalJobsCount`; bogus host → connection error.)

---

## Where to focus (highest yield first)

**A. Likely on Greenhouse/Ashby/Lever with a non-obvious slug — chase these first.**
These are modern tech/AI/security companies that almost certainly use a supported ATS;
they're uncovered only because the auto-guessed slug missed. Try `detect_platforms.py`
first, then a web search for the exact board slug:

> Retool, Monday.com, Harness, dbt Labs, Pulumi, Gantry, Segment, Unity, Hugging Face,
> Weights and Biases, Jasper, Adept, Replicate, Devo, Varonis, Secureworks, Claroty,
> Lacework, Pentera, Cyolo, Telos, Truera, Gretel, Credo AI, EclecticIQ, Nucleus Security,
> Somos, Ubie, Coursicle, Beli, Cadent, Integral Ad Science

**B. Finance / insurance / enterprise — likely Workday or Oracle (use the URL search).**
> Citadel, Balyasny, Five Rings, Wolverine Trading, D. E. Shaw, Renaissance Technologies,
> Two Sigma, SMBC, VanEck, Arcesium, MarketAxess, New York Life Insurance, Acuity Insurance,
> Greater New York Insurance Companies, Nuvei, Flutterwave, Crowe, West Monroe, Bechtel,
> ICF, Steampunk, ICE, KnitWell Group, Laserfiche

**C. Already investigated — KNOWN UNREACHABLE, do not re-chase:**
| Company | System |
|---|---|
| Groq | Gem |
| Cyera, Upwind Security | Comeet |
| Codeium | renamed Windsurf → folded into Cognition (already have `ashby:cognition`) |
| Red Canary | acquired by Zscaler |
| Dazz | acquired by Wiz |
| IBM | custom (BrassRing/Kenexa) |
| ADP | own (jobs.adp.com) |
| UBS | Avature |
| Keysight | own (jobs.keysight.com) |
| Kensho Technologies | folded into S&P Global |
| Two Sigma | custom portal (careers.twosigma.com) |
| Chainalysis | JS careers page, no detectable supported ATS |

> **Already covered, ignore if you see them flagged:** `Splunk` is in the watchlist as
> `Cisco (Splunk)` (Cisco's Workday board); `Akamai` and `NBCUniversal` are already added.
> `ICE` looked like Oracle (`egdd.fa.us6.oraclecloud.com`) but returned 0 — worth one
> retry with the exact job-posting URL, else leave it.

---

## Definition of done

For each company you resolve: entry added to **both** JSON files, verified via
`verify_watchlist.py` (real name / sample jobs match the company), identity double-checked
against the pitfalls above. Commit with a short message (the user drives git; **no
`Co-Authored-By` trailer**).

---

## Full list of the 113 uncovered companies

ACX, Acuity Insurance, ADP, AlteraSF, Arcesium, ASTi, AtlasQuo, Auzmor, Balyasny, Bechtel,
Beli, Cadent, Capstone, Careyaya Health Tech, Citadel, Clarity Innovations, Convergent,
Coursicle, Creative Spirit US, Crowe, EclecticIQ, Electrify America, Five Rings,
Greater New York Insurance Companies, Greek Learners, Hireshire, Hiscope Enterprises, IBM,
ICE, ICF, IQ Storage, iRocket, Retool, Monday.com, JMP, Kensho Technologies, Segment,
Keysight, KnitWell Group, Harness, Laserfiche, M Science, dbt Labs, Doordash Merchant,
MarketAxess, Unity, SoundCloud, Pulumi, narb, NBA, New Harbor, Headspace,
New York Life Insurance, Flutterwave, Nuvei, Ninth Wave, Nucleus Security, Olami, OneStream,
Sarcos, Orange Tail, Integral Ad Science, Princeton Biopartners, Chainalysis, Two Sigma,
QuickSlot Health, Wolverine Trading, Research Innovations, D. E. Shaw, Renaissance Technologies,
SalesPilot 365, Credo AI, Splunk, Security Innovation, TheGuarantors, Truera, Gantry,
Flatiron School, VanEck, West Monroe, AppDynamicsWEX, Wurl, Gretel, Zus HealthWorkhelio,
Visionary Integration Professionals, Versana, Jasper, U.S. Hunger, Vallera, Ubie, UBS, Adept,
Shee Atika Government Services, Steampunk, Replicate, Somos, SMBC, Lacework, Pentera,
Hugging Face, Weights and Biases, Devo, Varonis, Cyolo, Telos, Secureworks, Claroty, Groq,
Codeium, Red Canary, Cyera, Upwind Security, Dazz
