#!/usr/bin/env python3
"""
Job scraper for the job_filter pipeline. Stdlib only, Python 3.9+.

Pulls listings from machine-readable sources (no fragile HTML scraping):
  - RemoteOK public JSON API
  - We Work Remotely RSS feeds
  - Community job-list GitHub repos (raw README tables)

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
import urllib.parse
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


# Community job-list GitHub repos (SimplifyJobs/New-Grad-Positions,
# vanshb03/New-Grad-2027, the Summer*-Internships forks, ...). These are curated
# markdown files whose raw text is one big table of Company / Role / Location /
# Apply-link, so the "API" is just the raw.githubusercontent.com URL. Two table
# dialects show up in the wild and both are handled here:
#   - markdown pipe rows:  | **Company** | Role | Boston, MA | <a href=...> | Aug 05 |
#   - HTML <table> rows:   <tr><td><strong><a ...>Company</a></strong></td>...
# Shared conventions: "↳" in the company cell repeats the row above, and the
# legend emoji 🔒 (closed) / 🛂 (no sponsorship) / 🇺🇸 (citizenship) mark rows.
GITHUB_RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
GITHUB_LIST_DEFAULT_TAGS = ["new grad", "entry level"]

_GH_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_GH_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.I | re.S)
_GH_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                           re.I | re.S)
_GH_HREF_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', re.I)
_GH_MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(\s*<?([^)>\s]+)")
_GH_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_GH_MD_LABEL_RE = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")
_GH_BR_RE = re.compile(r"</?br\s*/?>", re.I)
# Multi-location cells collapse behind <details><summary>5 locations</summary>...
# The summary is just a count label; the real list is the details body.
_GH_SUMMARY_RE = re.compile(r"<summary\b[^>]*>.*?</summary>", re.I | re.S)
_GH_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
_GH_TRACKING_RE = re.compile(r"^(utm_.*|ref)$", re.I)  # not "source" — some ATSes use it

# The URL you actually have in hand is the one in the browser bar:
#   https://github.com/speedyapply/2027-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md
# Fetching that verbatim *appears* to work — GitHub renders the markdown, and the
# rendered table parses as the HTML dialect — but every "## FAANG+" heading comes
# back as an <h3>, so _gh_iter_rows never sets a section and include_sections /
# exclude_sections silently become no-ops (measured: 13 quant rows leak into an
# exclude_sections:["quant"] source). It's also ~9x the bytes. So rewrite any
# GitHub page URL to raw.githubusercontent.com rather than trusting the caller.
_GH_BLOB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)/(?:blob|raw)/([^/?#]+)/([^?#]+)",
    re.I)
_GH_TREE_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)/tree/([^/?#]+)/?([^?#]*)", re.I)
_GH_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+?)(?:\.git)?/?(?:[?#]|$)", re.I)
_GH_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/([^/?#]+)/([^/?#]+)/", re.I)


def _gh_normalize_url(url, branch, path):
    """Any GitHub URL for a list file -> (raw URL, "owner/name").

    Accepts the raw URL, the /blob/ (or /raw/) file page, a /tree/ directory,
    and a bare repo root; the last two fall back to the configured branch/path.
    A non-GitHub URL passes through untouched with an empty repo, so pointing
    at raw markdown on any other host still works.

    Returning the repo separately is what keeps the "source" field stable: all
    of these forms then label rows github_list/owner/name, so switching a config
    entry between "url" and "repo" doesn't fork the source in pipeline_stats.
    """
    blob = _GH_BLOB_RE.match(url)
    if blob:
        owner, name, ref, file_path = blob.groups()
        # A branch containing "/" is indistinguishable from the file path here
        # without hitting the API; these lists all use main/dev, so take one
        # segment. Pass repo/branch/path explicitly if you have such a branch.
        return GITHUB_RAW.format(repo=f"{owner}/{name}", branch=ref,
                                 path=file_path), f"{owner}/{name}"
    tree = _GH_TREE_RE.match(url)
    if tree:
        owner, name, ref, subdir = tree.groups()
        prefix = f"{subdir.rstrip('/')}/" if subdir else ""
        return GITHUB_RAW.format(repo=f"{owner}/{name}", branch=ref,
                                 path=prefix + path), f"{owner}/{name}"
    bare = _GH_REPO_RE.match(url)
    if bare:
        owner, name = bare.groups()
        return GITHUB_RAW.format(repo=f"{owner}/{name}", branch=branch,
                                 path=path), f"{owner}/{name}"
    raw = _GH_RAW_RE.match(url)
    if raw:
        return url, "/".join(raw.groups())
    return url, ""

# Header label -> our field. Covers the variants across these repos.
_GH_COLUMNS = {
    "company": "company", "employer": "company", "organization": "company",
    "role": "title", "position": "title", "title": "title", "job title": "title",
    "location": "location", "locations": "location",
    "application": "url", "application/link": "url", "application link": "url",
    "apply": "url", "apply link": "url", "link": "url", "links": "url",
    "posting": "url", "postings": "url",
    "salary": "salary", "compensation": "salary", "pay": "salary",
    "date posted": "posted", "date": "posted", "posted": "posted", "age": "posted",
}

# Anchors these lists add alongside the real apply link (referral trackers and
# the button images). Never the destination we want.
_GH_LINK_NOISE = ("simplify.jobs", "imgur.com", "github.com", "discord.gg")

_GH_CLOSED = "\U0001F512"       # 🔒 application closed
_GH_NO_SPONSOR = "\U0001F6C2"   # 🛂 does NOT offer sponsorship
_GH_CITIZENSHIP = "\U0001F1FA\U0001F1F8"  # 🇺🇸 requires U.S. citizenship
_GH_ADV_DEGREE = "\U0001F393"   # 🎓 advanced degree required (MS/PhD/MBA)

# Legend markers plus the decorations these lists sprinkle on rows ("🔥 ByteDance",
# "Engineer 🛂"). Read as flags, then stripped so titles/companies stay clean —
# they end up in the tracker and the LLM prompt as-is.
_GH_MARKERS = (_GH_CLOSED, _GH_NO_SPONSOR, _GH_CITIZENSHIP, _GH_ADV_DEGREE,
               "\U0001F1FA", "\U0001F1F8",   # bare regional indicators
               "\U0001F525", "⭐", "\U0001F195", "❗")  # 🔥 ⭐ 🆕 ❗


def _gh_demark(text):
    for mark in _GH_MARKERS:
        text = text.replace(mark, " ")
    return WS_RE.sub(" ", text).strip(" -–—|")


def _gh_cell_text(cell):
    """One table cell -> plain text. Line-break tags become ' / ' so the
    multi-location cells ("Chicago, IL</br>New York, NY") stay on one line."""
    txt = _GH_SUMMARY_RE.sub(" ", cell)
    txt = _GH_BR_RE.sub(" / ", txt)
    txt = _GH_MD_IMG_RE.sub(" ", txt)
    txt = _GH_MD_LABEL_RE.sub(r"\1", txt)   # [label](url) -> label
    txt = strip_html(txt)
    txt = txt.replace("**", "").replace("`", "")
    return WS_RE.sub(" ", txt).strip()


def _gh_cell_urls(cell):
    urls = list(_GH_HREF_RE.findall(cell))
    urls += _GH_MD_LINK_RE.findall(cell)
    return [html.unescape(u) for u in urls]


def _gh_clean_url(url):
    """Drop utm_*/ref tracking params. These are per-repo constants, so leaving
    them in would be harmless for applying but noisy in the tracker — and they
    make the URL (which is the dedupe fingerprint) churn if a repo retags."""
    try:
        parts = urllib.parse.urlsplit(url)
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return url
    kept = [(k, v) for k, v in pairs if not _GH_TRACKING_RE.match(k)]
    if len(kept) == len(pairs):
        return url  # nothing to strip — never rewrite a URL we didn't change
    query = urllib.parse.urlencode(kept, quote_via=urllib.parse.quote, safe=":/,;=")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _gh_pick_url(cell):
    """The employer's apply link from an application cell. The button anchor
    labels itself (alt="Apply"); anything else in the cell is a tracker."""
    for href, inner in _GH_ANCHOR_RE.findall(cell):
        if "apply" in inner.lower():
            return _gh_clean_url(html.unescape(href))
    for url in _gh_cell_urls(cell):
        if not any(noise in url.lower() for noise in _GH_LINK_NOISE):
            return _gh_clean_url(url)
    return ""


def _gh_split_md_row(line):
    """'| a | b | c |' -> ['a', 'b', 'c']."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _gh_header_map(cells):
    """Header row -> {field: column index}, or None if this isn't a header."""
    mapping = {}
    for idx, cell in enumerate(cells):
        label = _gh_cell_text(cell).lower().strip(" *:")
        field = _GH_COLUMNS.get(label)
        if field and field not in mapping:
            mapping[field] = idx
    if "company" in mapping and "title" in mapping:
        return mapping
    return None


def _gh_section_allowed(section, include, exclude):
    low = section.lower()
    if exclude and any(x.lower() in low for x in exclude):
        return False
    if include and not any(x.lower() in low for x in include):
        return False
    return True


def _gh_iter_rows(text):
    """Walk the document yielding (section_heading, cells) for every table row,
    in both dialects. Heading tracking is why this is a line walk and not a
    single findall — section is what lets you keep SWE roles and drop quant."""
    section = ""
    pending = None   # accumulating a multi-line <tr>...</tr>
    for raw in text.splitlines():
        line = raw.strip()
        if pending is not None:
            pending.append(line)
            if "</tr>" in line.lower():
                yield section, _GH_CELL_RE.findall("\n".join(pending))
                pending = None
            continue
        low = line.lower()
        if low.startswith("<tr"):
            if "</tr>" in low:
                yield section, _GH_CELL_RE.findall(line)
            else:
                pending = [line]
            continue
        heading = _GH_HEADING_RE.match(line)
        if heading:
            section = _gh_cell_text(heading.group(2))
            continue
        if line.startswith("|") and line.count("|") >= 3:
            yield section, _gh_split_md_row(line)


def scrape_github_list(source_cfg):
    """A community job-list GitHub repo's markdown file.

    Config: {"type": "github_list", "repo": "vanshb03/New-Grad-2027",
             "branch": "dev", "path": "README.md",
             "include_sections": [...], "exclude_sections": [...],
             "skip_closed": true, "skip_no_sponsorship": false,
             "skip_citizenship": false, "skip_advanced_degree": false,
             "tags": [...], "max_jobs": 0}
    Or put any GitHub URL for the file in "url" instead of repo/branch/path —
    the browser-bar /blob/ link included; see _gh_normalize_url.

    Rows carry no job description — only Company / Role / Location / link — so
    the description is synthesised from those fields, the same list-only shape
    the Workday and Oracle connectors produce when descriptions are off.
    """
    repo = source_cfg.get("repo", "")
    branch = source_cfg.get("branch", "main")
    path = source_cfg.get("path", "README.md").lstrip("/")
    configured_url = source_cfg.get("url", "")
    if configured_url:
        url, found_repo = _gh_normalize_url(configured_url, branch, path)
        repo = repo or found_repo
    elif repo:
        url = GITHUB_RAW.format(repo=repo, branch=branch, path=path)
    else:
        raise ValueError("github_list source needs a 'repo' (owner/name) or a 'url'")

    include = source_cfg.get("include_sections", [])
    exclude = source_cfg.get("exclude_sections", [])
    skip_closed = source_cfg.get("skip_closed", True)
    skip_no_sponsor = source_cfg.get("skip_no_sponsorship", False)
    skip_citizen = source_cfg.get("skip_citizenship", False)
    skip_advanced = source_cfg.get("skip_advanced_degree", False)
    tags = source_cfg.get("tags", GITHUB_LIST_DEFAULT_TAGS)
    max_jobs = source_cfg.get("max_jobs", 0)
    label = repo or urllib.parse.urlsplit(url).path.strip("/")
    # No ":" — recruitment_watch.py and pipeline_stats.py read that as "watchlist".
    source_name = f"github_list/{label}"

    text = fetch(url)
    jobs, cols, last_company = [], None, ""
    for section, cells in _gh_iter_rows(text):
        if len(cells) < 3:
            continue
        if all(_GH_SEP_CELL_RE.match(c.strip()) for c in cells if c.strip()):
            continue  # markdown's |---|---| separator
        header = _gh_header_map(cells)
        if header:
            cols, last_company = header, ""
            continue
        if cols is None:
            continue  # a table we never saw a usable header for

        def cell(field):
            idx = cols.get(field)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        company = _gh_demark(_gh_cell_text(cell("company")))
        # "↳" repeats the company from the row above (and blank does too).
        if not company or company.startswith("↳"):
            company = last_company
        else:
            last_company = company

        title = _gh_demark(_gh_cell_text(cell("title")))
        if not title:
            continue

        row_text = " ".join(cells)
        closed = _GH_CLOSED in row_text
        no_sponsor = _GH_NO_SPONSOR in row_text
        citizenship = _GH_CITIZENSHIP in row_text
        advanced = _GH_ADV_DEGREE in row_text
        if (skip_closed and closed) or (skip_no_sponsor and no_sponsor) \
                or (skip_citizen and citizenship) or (skip_advanced and advanced):
            continue
        if not _gh_section_allowed(section, include, exclude):
            continue

        job_url = _gh_pick_url(cell("url"))
        if not job_url:
            continue  # closed/withdrawn rows have the emoji instead of a link

        location = _gh_cell_text(cell("location"))
        posted = _gh_cell_text(cell("posted"))
        salary = _gh_cell_text(cell("salary"))
        notes = []
        if no_sponsor:
            notes.append("does NOT offer visa sponsorship")
        if citizenship:
            notes.append("requires U.S. citizenship")
        if advanced:
            notes.append("requires an advanced degree (Master's/PhD/MBA)")
        desc_lines = []
        if tags:
            desc_lines.append("TAGS: " + ", ".join(tags))
        desc_lines.append(f"{title} at {company or 'unknown company'}")
        if location:
            desc_lines.append(f"Location: {location}")
        if salary:
            desc_lines.append(f"Salary: {salary}")
        if posted:
            desc_lines.append(f"Posted: {posted}")
        if section:
            desc_lines.append(f"Category: {section}")
        if notes:
            desc_lines.append("Notes: " + "; ".join(notes))
        desc_lines.append(f"Listed by {label} (curated new-grad job list). "
                          "Full description is on the linked application page.")

        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "salary": salary,
            "url": job_url,
            "description": "\n".join(desc_lines)[:MAX_DESC_CHARS],
            "source": source_name,
        })
        if max_jobs and len(jobs) >= max_jobs:
            break
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
# A posting open in several cities reports locationsText as "3 Locations" — a
# count, not a place. Only the detail endpoint names the cities, so such jobs
# start with no location and get resolved after prefiltering; see
# resolve_workday_locations().
# re.M so the same pattern can rewrite the placeholder where it leads a
# multi-line description ("3 Locations\nPosted Today").
WORKDAY_MULTI_LOC_RE = re.compile(r"^\d+\s+locations?$", re.I | re.M)


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


def _workday_locations(detail_url):
    """Every city on one posting, "A; B; C", from its detail endpoint. "" on failure."""
    try:
        req = urllib.request.Request(
            detail_url, headers={"Accept": "application/json", "User-Agent": WORKDAY_UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ""
    posting = info.get("jobPostingInfo") or {}
    places = [posting.get("location", "")] + list(posting.get("additionalLocations") or [])
    # Workday pads some entries ("San Francisco,  CA"), so squeeze the whitespace.
    return "; ".join(re.sub(r"\s+", " ", p).strip() for p in places if p and p.strip())


def resolve_workday_locations(jobs):
    """Name the cities for Workday's "N Locations" postings. Returns how many.

    One request per posting, so this runs after the keyword prefilter — only the
    handful of plausible jobs cost anything. A posting whose detail fetch fails
    keeps its empty location and gets judged by the LLM like any other job with
    no location info.
    """
    pending = [j for j in jobs if j.get("_workday_detail")]
    for i, job in enumerate(pending):
        loc = _workday_locations(job.pop("_workday_detail"))
        if loc:
            job["location"] = loc
            # The description leads with the same "N Locations" placeholder.
            job["description"] = WORKDAY_MULTI_LOC_RE.sub(
                loc, job.get("description", ""), count=1)
        if i + 1 < len(pending):
            time.sleep(0.3)  # polite between detail calls
    return len(pending)


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
            multi_loc = bool(WORKDAY_MULTI_LOC_RE.match(loc.strip()))
            job = {
                "title": item.get("title", ""),
                "company": slug,
                "location": "" if multi_loc else loc,
                "salary": "",
                "url": f"https://{host}/{site}{ext}" if ext else f"https://{host}/{site}",
                "description": desc[:MAX_DESC_CHARS],
                "source": f"workday:{host}/{site}",
            }
            if multi_loc and ext:
                job["_workday_detail"] = f"https://{host}/wday/cxs/{tenant}/{site}{ext}"
            jobs.append(job)
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
    "github_list": scrape_github_list,
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
                json.JSONDecodeError, ValueError) as e:
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

    # Workday's multi-city postings got this far with no location. Now that the
    # list is small, name their cities and apply the location filter for real.
    resolved = resolve_workday_locations(all_jobs)
    if resolved:
        all_jobs = location_prefilter(all_jobs, cfg)

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
          f"-> {len(all_jobs)} new ({already_seen} already evaluated"
          f"{f'; {resolved} Workday locations resolved' if resolved else ''})")
    print(f"Wrote {args.out}")
    print(f"Next: python3 filter_jobs.py {Path(args.out).name}")


if __name__ == "__main__":
    main()
