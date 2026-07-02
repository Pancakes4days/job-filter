#!/usr/bin/env python3
"""
Job scraper for the job_filter pipeline. Stdlib only, Python 3.9+.

Pulls listings from machine-readable sources (no fragile HTML scraping):
  - RemoteOK public JSON API
  - We Work Remotely RSS feeds

Applies cheap keyword pre-filtering (so the slow LLM step only sees
plausible candidates), then writes scraped_jobs.json in the exact format
filter_jobs.py expects.

Usage:
    python3 scraper.py                      # uses scraper_config.json
    python3 scraper.py --out myjobs.json
    python3 scraper.py --no-prefilter       # keep everything, let the LLM judge
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from paths import CONFIG_DIR, DATA_DIR  # noqa: E402

CONFIG_PATH = CONFIG_DIR / "scraper_config.json"

# Reuse the filter's fingerprint + seen-list so "seen" means "already
# evaluated by the LLM", not merely "already scraped". A job that gets
# scraped but never filtered keeps reappearing until it's processed.
from filter_jobs import job_fingerprint, load_seen  # noqa: E402

USER_AGENT = "JobFilterBot/1.0 (personal job search; contact: see config)"
MAX_DESC_CHARS = 4000  # keep descriptions within the LLM's context budget

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


class _HTMLTextExtractor(HTMLParser):
    """Stdlib HTML-to-text — tolerates malformed markup better than regexes."""
    _NEWLINE_ON_OPEN = {"br", "hr"}
    _NEWLINE_ON_CLOSE = {"p", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._buf = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1
        if tag in self._NEWLINE_ON_OPEN:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._depth = max(0, self._depth - 1)
        if tag in self._NEWLINE_ON_CLOSE:
            self._buf.append("\n")

    def handle_data(self, data):
        if not self._depth:
            self._buf.append(data)

    def text(self):
        lines = [WS_RE.sub(" ", ln).strip() for ln in "".join(self._buf).splitlines()]
        return "\n".join(ln for ln in lines if ln)


def strip_html(text):
    """Dependency-free HTML -> plain text via stdlib HTMLParser."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(text or "")
        extractor.close()
    except Exception:
        # Severely broken markup — fall back to regex
        text = html.unescape(text or "")
        text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.I)
        text = TAG_RE.sub(" ", text)
        lines = [WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln)[:MAX_DESC_CHARS]
    return extractor.text()[:MAX_DESC_CHARS]


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- sources

def scrape_remoteok(source_cfg):
    """RemoteOK public API: https://remoteok.com/api (first element is metadata)."""
    raw = fetch(source_cfg.get("url", "https://remoteok.com/api"))
    data = json.loads(raw)
    jobs = []
    for item in data:
        if not isinstance(item, dict) or "position" not in item:
            continue  # skips the legal/metadata header element
        salary = ""
        lo, hi = item.get("salary_min"), item.get("salary_max")
        if lo and hi:
            salary = f"${lo:,} - ${hi:,}"
        tags = ", ".join(item.get("tags", []))
        desc = strip_html(item.get("description", ""))
        if tags:
            desc = f"TAGS: {tags}\n{desc}"
        jobs.append({
            "title": item.get("position", ""),
            "company": item.get("company", ""),
            "location": item.get("location") or "Remote",
            "salary": salary,
            "url": item.get("url", ""),
            "description": desc,
            "source": "remoteok",
        })
    return jobs


def scrape_wwr_rss(source_cfg):
    """We Work Remotely RSS feed (one feed per category)."""
    raw = fetch(source_cfg["url"])
    root = ET.fromstring(raw)
    jobs = []
    for item in root.iter("item"):
        title_raw = (item.findtext("title") or "").strip()
        # WWR titles look like "Company Name: Job Title"
        company, _, title = title_raw.partition(":")
        if not title:
            title, company = title_raw, ""
        region = (item.findtext("region") or "").strip()
        jobs.append({
            "title": title.strip(),
            "company": company.strip(),
            "location": region or "Remote",
            "salary": "",
            "url": (item.findtext("link") or "").strip(),
            "description": strip_html(item.findtext("description") or ""),
            "source": "weworkremotely",
        })
    return jobs


def scrape_remotive(source_cfg):
    """Remotive public API: https://remotive.com/api/remote-jobs
    Their terms ask for low request volume — fine for a nightly cron."""
    raw = fetch(source_cfg.get("url", "https://remotive.com/api/remote-jobs"))
    data = json.loads(raw)
    jobs = []
    for item in data.get("jobs", []):
        tags = ", ".join(item.get("tags", []))
        desc = strip_html(item.get("description", ""))
        if tags:
            desc = f"TAGS: {tags}\n{desc}"
        jobs.append({
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "location": item.get("candidate_required_location") or "Remote",
            "salary": item.get("salary", ""),
            "url": item.get("url", ""),
            "description": desc,
            "source": "remotive",
        })
    return jobs


def scrape_arbeitnow(source_cfg):
    """Arbeitnow public API (paginated). Listings skew Europe/Germany."""
    base = source_cfg.get("url", "https://www.arbeitnow.com/api/job-board-api")
    pages = source_cfg.get("pages", 2)
    jobs = []
    for page in range(1, pages + 1):
        raw = fetch(f"{base}?page={page}")
        data = json.loads(raw)
        for item in data.get("data", []):
            extras = ", ".join(item.get("tags", []) + item.get("job_types", []))
            desc = strip_html(item.get("description", ""))
            if extras:
                desc = f"TAGS: {extras}\n{desc}"
            loc = item.get("location", "")
            if item.get("remote"):
                loc = f"{loc} (Remote)" if loc else "Remote"
            jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "location": loc,
                "salary": "",
                "url": item.get("url", ""),
                "description": desc,
                "source": "arbeitnow",
            })
        if not data.get("links", {}).get("next"):
            break
        time.sleep(1)
    return jobs


def scrape_hn_hiring(source_cfg):
    """Latest monthly 'Ask HN: Who is hiring?' thread via the Algolia API.
    One request finds the thread, one fetches every comment in it."""
    search_url = ("https://hn.algolia.com/api/v1/search_by_date"
                  "?tags=story,author_whoishiring&query=who%20is%20hiring")
    hits = json.loads(fetch(search_url)).get("hits", [])
    thread = next((h for h in hits
                   if "who is hiring" in (h.get("title") or "").lower()), None)
    if thread is None:
        raise ValueError("Could not locate a 'Who is hiring?' thread")
    story_id = thread.get("story_id") or thread.get("objectID")
    time.sleep(source_cfg.get("request_delay", 1))
    item = json.loads(fetch(f"https://hn.algolia.com/api/v1/items/{story_id}"))

    jobs = []
    for c in item.get("children", []):
        text = strip_html(c.get("text") or "")
        if not text or len(text) < 40:
            continue  # deleted/empty/noise comments
        lines = text.splitlines()
        first = lines[0]
        # Convention: "Company | Role | Location | extras..."
        parts = [p.strip() for p in first.split("|")]
        if len(parts) >= 2:
            company, title = parts[0], parts[1]
            location = parts[2] if len(parts) > 2 else ""
        else:
            company, title, location = "", first[:120], ""
        jobs.append({
            "title": title[:150],
            "company": company[:100],
            "location": location[:100],
            "salary": "",
            "url": f"https://news.ycombinator.com/item?id={c.get('id','')}",
            "description": text,
            "source": "hn_hiring",
        })
    return jobs


def _fetch_greenhouse(slug):
    raw = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    data = json.loads(raw)
    jobs = []
    for item in data.get("jobs", []):
        jobs.append({
            "title": item.get("title", ""),
            "company": slug,
            "location": (item.get("location") or {}).get("name", ""),
            "salary": "",
            "url": item.get("absolute_url", ""),
            "description": strip_html(item.get("content", "")),
            "source": f"greenhouse:{slug}",
        })
    return jobs


def _fetch_lever(slug):
    raw = fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    data = json.loads(raw)
    jobs = []
    for item in data:
        cats = item.get("categories") or {}
        desc = item.get("descriptionPlain") or strip_html(item.get("description", ""))
        extras = ", ".join(filter(None, [cats.get("team"), cats.get("commitment")]))
        if extras:
            desc = f"{extras}\n{desc}"
        jobs.append({
            "title": item.get("text", ""),
            "company": slug,
            "location": cats.get("location", ""),
            "salary": "",
            "url": item.get("hostedUrl", ""),
            "description": desc[:MAX_DESC_CHARS],
            "source": f"lever:{slug}",
        })
    return jobs


def _fetch_ashby(slug):
    raw = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    data = json.loads(raw)
    jobs = []
    for item in data.get("jobs", []):
        loc = item.get("location") or item.get("locationName") or ""
        if item.get("isRemote"):
            loc = f"{loc} (Remote)" if loc else "Remote"
        jobs.append({
            "title": item.get("title", ""),
            "company": slug,
            "location": loc,
            "salary": "",
            "url": item.get("jobUrl") or item.get("applyUrl", ""),
            "description": strip_html(item.get("descriptionHtml", ""))
                           or item.get("departmentName", ""),
            "source": f"ashby:{slug}",
        })
    return jobs


def _fetch_smartrecruiters(slug):
    jobs = []
    limit, offset = 100, 0
    while True:
        raw = fetch(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                    f"?limit={limit}&offset={offset}")
        data = json.loads(raw)
        page = data.get("content", [])
        for item in page:
            loc = item.get("location") or {}
            loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"),
                                               loc.get("country")]))
            if loc.get("remote"):
                loc_str = f"{loc_str} (Remote)" if loc_str else "Remote"
            jobs.append({
                "title": item.get("name", ""),
                "company": slug,
                "location": loc_str,
                "salary": "",
                "url": f"https://jobs.smartrecruiters.com/{slug}/{item.get('id','')}",
                "description": strip_html(((item.get("jobAd") or {}).get("sections")
                                           or {}).get("jobDescription", {}).get("text", "")),
                "source": f"smartrecruiters:{slug}",
            })
        offset += len(page)
        if offset >= data.get("totalFound", 0) or not page:
            break
        time.sleep(1)
    return jobs


def _fetch_workable(slug):
    raw = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    data = json.loads(raw)
    jobs = []
    for item in data.get("jobs", []):
        loc = item.get("location") or {}
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"),
                                           loc.get("country")]))
        if item.get("remote") or loc.get("workplace") == "remote":
            loc_str = f"{loc_str} (Remote)" if loc_str else "Remote"
        jobs.append({
            "title": item.get("title", ""),
            "company": slug,
            "location": loc_str,
            "salary": "",
            "url": item.get("url") or item.get("shortlink", ""),
            "description": strip_html(item.get("description", "")),
            "source": f"workable:{slug}",
        })
    return jobs


def _fetch_recruitee(slug):
    raw = fetch(f"https://{slug}.recruitee.com/api/offers/")
    data = json.loads(raw)
    jobs = []
    for item in data.get("offers", []):
        loc_str = item.get("location") or ", ".join(
            filter(None, [item.get("city"), item.get("country_code")]))
        jobs.append({
            "title": item.get("title", ""),
            "company": slug,
            "location": loc_str,
            "salary": "",
            "url": item.get("careers_url") or item.get("url", ""),
            "description": strip_html(item.get("description", "")),
            "source": f"recruitee:{slug}",
        })
    return jobs


# Workday has no single-slug public API like the others. Each employer runs a
# tenant at {tenant}.{dc}.myworkdayjobs.com/{site} and exposes an undocumented
# JSON endpoint the hosted career site itself calls:
#     POST https://{host}/wday/cxs/{tenant}/{site}/jobs   (paginated list)
#     GET  https://{host}/wday/cxs/{tenant}/{site}{path}  (one posting's detail)
# The watchlist slug encodes host + site as "host/site", e.g.
#     "bitsight.wd1.myworkdayjobs.com/Bitsight"
# tenant is the first host label. Workday rejects non-browser UAs, so use a
# browser one here.
WORKDAY_UA = "Mozilla/5.0 (compatible; JobFilterBot/1.0; personal job search)"
WORKDAY_PAGE_LIMIT = 20        # Workday caps the list endpoint at 20 per page
WORKDAY_MAX_PAGES = 25         # bound per-company requests (~500 most-recent jobs)
# The list endpoint omits descriptions. Enabling this fetches each posting's
# detail for a full description — richer for the LLM, but one request per job.
WORKDAY_FETCH_DESCRIPTIONS = False


def _parse_workday_slug(slug):
    """"host/site" (or a full careers URL) -> (host, tenant, site).

    tenant defaults to the first label of the host; append "|tenant" to the slug
    to override it for the rare tenant whose cxs name differs from its subdomain.
    """
    s = slug.strip()
    s = re.sub(r"^https?://", "", s).strip("/")
    s, _, tenant_override = s.partition("|")
    host, _, rest = s.strip("/").partition("/")
    site = rest.strip("/").split("/")[0]  # first path segment
    if not host or not site:
        raise ValueError(f"bad workday slug {slug!r} (expected 'host/site')")
    tenant = tenant_override.strip() or host.split(".")[0]
    return host, tenant, site


def _workday_post(url, offset):
    payload = json.dumps({"appliedFacets": {}, "limit": WORKDAY_PAGE_LIMIT,
                          "offset": offset, "searchText": ""}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": WORKDAY_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _workday_description(host, tenant, site, external_path):
    """Fetch one posting's full description (HTML -> text). Best-effort."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": WORKDAY_UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read().decode("utf-8", errors="replace"))
        return strip_html((info.get("jobPostingInfo") or {}).get("jobDescription", ""))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ""


def _fetch_workday(slug):
    host, tenant, site = _parse_workday_slug(slug)
    list_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset = [], 0
    for _page in range(WORKDAY_MAX_PAGES):
        data = _workday_post(list_url, offset)
        postings = data.get("jobPostings", [])
        for item in postings:
            ext = item.get("externalPath", "")
            loc = item.get("locationsText", "")
            desc_bits = [b for b in [loc, item.get("postedOn", "")] if b]
            desc = "\n".join(desc_bits)
            if WORKDAY_FETCH_DESCRIPTIONS and ext:
                full = _workday_description(host, tenant, site, ext)
                if full:
                    desc = full
                time.sleep(0.3)  # polite between detail calls
            jobs.append({
                "title": item.get("title", ""),
                "company": slug,
                "location": loc,
                "salary": "",
                "url": f"https://{host}/{site}{ext}" if ext else f"https://{host}/{site}",
                "description": desc[:MAX_DESC_CHARS],
                "source": f"workday:{host}/{site}",
            })
        offset += len(postings)
        if not postings or offset >= data.get("total", 0):
            break
        time.sleep(1)  # polite between list pages
    return jobs


# Oracle Recruiting Cloud (Candidate Experience) — the ATS behind large finance/
# enterprise careers sites (JPMorgan, Akamai, ...). Each tenant lives at a host
# like {tenant}.fa.oraclecloud.com or a shared pod fa-ext...saasfaprod1.fa.ocs.
# oraclecloud.com, and a career-site view identified by a "CX_####" site number.
# The hosted site calls a public REST endpoint:
#     GET .../hcmRestApi/resources/latest/recruitingCEJobRequisitions   (list)
#     GET .../recruitingCEJobRequisitionDetails                          (one job)
# The watchlist slug encodes host + site as "host/site", e.g.
#     "jpmc.fa.oraclecloud.com/CX_1001"
# NB: the list needs expand=requisitionList... or the job array comes back empty,
# and the payload nests jobs under items[0].requisitionList with the running total
# at items[0].TotalJobsCount.
ORACLE_UA = "Mozilla/5.0 (compatible; JobFilterBot/1.0; personal job search)"
ORACLE_EXPAND = "requisitionList.secondaryLocations,flexFieldsFacet.values"
ORACLE_PAGE_LIMIT = 200        # the list endpoint honours limits up to 200
ORACLE_MAX_PAGES = 10          # bound per-company requests (~2000 most-recent jobs)
ORACLE_FETCH_DESCRIPTIONS = False  # True fetches each posting's detail (1 req/job)


def _parse_oracle_slug(slug):
    """"host/site" (or a full careers URL) -> (host, site).

    site is the CX career-site id, which may be "CX", "CX_1001", "jobsearch", etc.
    Handles both the canonical "host/site" slug and a full .../sites/<site>/... URL."""
    s = re.sub(r"^https?://", "", slug.strip()).strip("/")
    host = s.split("/")[0]
    m = re.search(r"/sites/([^/?#]+)", s)         # full careers URL form
    site = m.group(1) if m else (s.split("/", 1)[1] if "/" in s else "")
    if not host or not site:
        raise ValueError(f"bad oracle slug {slug!r} (expected 'host/site')")
    return host, site


def _oracle_get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": ORACLE_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _oracle_description(host, site, job_id):
    """Full description (HTML -> text) for one requisition. Best-effort."""
    url = (f"https://{host}/hcmRestApi/resources/latest/"
           f"recruitingCEJobRequisitionDetails?expand=all"
           f'&finder=ById;Id="{job_id}",siteNumber={site}')
    try:
        items = _oracle_get(url).get("items", [])
        if items:
            return strip_html(items[0].get("ExternalDescriptionStr", ""))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        pass
    return ""


def _fetch_oracle(slug):
    host, site = _parse_oracle_slug(slug)
    base = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand={ORACLE_EXPAND}")
    jobs, offset = [], 0
    for _page in range(ORACLE_MAX_PAGES):
        url = (f"{base}&finder=findReqs;siteNumber={site},"
               f"limit={ORACLE_PAGE_LIMIT},offset={offset},sortBy=POSTING_DATES_DESC")
        items = _oracle_get(url).get("items", [])
        if not items:
            break
        reqs = items[0].get("requisitionList", [])
        total = items[0].get("TotalJobsCount", 0)
        for r in reqs:
            jid = r.get("Id", "")
            loc = r.get("PrimaryLocation", "")
            desc = "\n".join(b for b in [loc, r.get("PostedDate", "")] if b)
            if ORACLE_FETCH_DESCRIPTIONS and jid:
                full = _oracle_description(host, site, jid)
                if full:
                    desc = full
                time.sleep(0.3)
            jobs.append({
                "title": r.get("Title", ""),
                "company": slug,
                "location": loc,
                "salary": "",
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                "description": desc[:MAX_DESC_CHARS],
                "source": f"oracle:{host}/{site}",
            })
        offset += len(reqs)
        if not reqs or offset >= total:
            break
        time.sleep(1)
    return jobs


PLATFORM_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "smartrecruiters": _fetch_smartrecruiters,
    "workable": _fetch_workable,
    "recruitee": _fetch_recruitee,
    "workday": _fetch_workday,
    "oracle": _fetch_oracle,
}


def scrape_watchlist(source_cfg):
    """Company career pages via their ATS platform APIs (Greenhouse/Lever/Ashby).
    Config: {"type": "watchlist", "companies":
             [{"platform": "greenhouse", "slug": "datadog", "label": "Datadog"}, ...]}
    Use detect_platforms.py to build the companies list from company names."""
    jobs = []
    companies = source_cfg.get("companies", [])
    for c in companies:
        platform, slug = c.get("platform"), c.get("slug")
        fetcher = PLATFORM_FETCHERS.get(platform)
        if not fetcher or not slug:
            print(f"\n  ! watchlist entry missing/unknown platform: {c}", end="")
            continue
        try:
            found = fetcher(slug)
            label = c.get("label", slug)
            for j in found:
                j["company"] = label
            jobs.extend(found)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ValueError) as e:
            print(f"\n  ! {platform}:{slug} failed ({e}) — continuing", end="")
        time.sleep(1)  # be polite across many small requests
    return jobs


SCRAPERS = {
    "remoteok": scrape_remoteok,
    "wwr_rss": scrape_wwr_rss,
    "remotive": scrape_remotive,
    "arbeitnow": scrape_arbeitnow,
    "hn_hiring": scrape_hn_hiring,
    "watchlist": scrape_watchlist,
}

# ---------------------------------------------------------------- pipeline

def _compile_keywords(keywords):
    """Whole-word/phrase regexes, so 'AI' doesn't match 'maintain'
    and 'ML' doesn't match 'html'. Phrases match across whitespace."""
    patterns = []
    for kw in keywords:
        escaped = r"\s+".join(re.escape(part) for part in kw.lower().split())
        patterns.append(re.compile(r"(?<!\w)" + escaped + r"(?!\w)"))
    return patterns


def keyword_prefilter(jobs, cfg):
    """Cheap text filter so the LLM only sees plausible listings.
    include: must match at least one (if list non-empty)
    require: must ALSO match at least one (if list non-empty) — use for
             e.g. early-career terms to triage large watchlist volumes
    exclude: any match drops the job"""
    include = _compile_keywords(cfg.get("include_keywords", []))
    require = _compile_keywords(cfg.get("require_keywords", []))
    exclude = _compile_keywords(cfg.get("exclude_keywords", []))
    kept = []
    for job in jobs:
        text = f"{job['title']} {job['description']}".lower()
        if exclude and any(p.search(text) for p in exclude):
            continue
        if include and not any(p.search(text) for p in include):
            continue
        if require and not any(p.search(text) for p in require):
            continue
        kept.append(job)
    return kept


def location_prefilter(jobs, cfg):
    """Filter on the location field. Jobs with NO location info pass through
    (the LLM judges those). exclude beats include."""
    inc = _compile_keywords(cfg.get("location_include", []))
    exc = _compile_keywords(cfg.get("location_exclude", []))
    if not inc and not exc:
        return jobs
    kept = []
    for job in jobs:
        loc = (job.get("location") or "").lower().strip()
        if not loc:
            kept.append(job)
            continue
        if exc and any(p.search(loc) for p in exc):
            continue
        if inc and not any(p.search(loc) for p in inc):
            continue
        kept.append(job)
    return kept


def dedupe(jobs):
    seen, out = set(), []
    for job in jobs:
        key = (job.get("url") or f"{job['title']}|{job['company']}").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def main():
    parser = argparse.ArgumentParser(description="Scrape job listings to JSON.")
    parser.add_argument("--out", default=str(DATA_DIR / "scraped_jobs.json"))
    parser.add_argument("--config", default=str(CONFIG_PATH),
                        help="Path to scraper config JSON (default: scraper_config.json)")
    parser.add_argument("--no-prefilter", action="store_true",
                        help="Skip keyword filtering; pass everything to the LLM")
    parser.add_argument("--include-seen", action="store_true",
                        help="Also emit jobs the filter has already evaluated")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"Missing {cfg_path} — create it (see README).")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    all_jobs = []
    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            continue
        kind = source.get("type")
        scraper = SCRAPERS.get(kind)
        if scraper is None:
            print(f"  ! unknown source type '{kind}', skipping")
            continue
        name = source.get("name", kind)
        print(f"Fetching {name} ... ", end="", flush=True)
        try:
            jobs = scraper(source)
            print(f"{len(jobs)} listings")
            all_jobs.extend(jobs)
        except (urllib.error.URLError, TimeoutError, ET.ParseError,
                json.JSONDecodeError) as e:
            print(f"FAILED ({e}) — continuing with other sources")
        time.sleep(cfg.get("delay_between_sources", 2))  # be polite

    fetched = len(all_jobs)
    all_jobs = dedupe(all_jobs)
    deduped = len(all_jobs)
    all_jobs = location_prefilter(all_jobs, cfg)
    located = len(all_jobs)
    if not args.no_prefilter:
        all_jobs = keyword_prefilter(all_jobs, cfg)
    prefiltered = len(all_jobs)

    already_seen = 0
    if not args.include_seen:
        seen = load_seen()
        if seen:
            fresh = [j for j in all_jobs if job_fingerprint(j) not in seen]
            already_seen = len(all_jobs) - len(fresh)
            all_jobs = fresh

    out = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "jobs": all_jobs,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\n{fetched} fetched -> {deduped} after dedupe -> "
          f"{located} after location filter -> {prefiltered} after prefilter "
          f"-> {len(all_jobs)} new ({already_seen} already evaluated)")
    print(f"Wrote {args.out}")
    print(f"Next: python3 filter_jobs.py {Path(args.out).name}")


if __name__ == "__main__":
    main()
