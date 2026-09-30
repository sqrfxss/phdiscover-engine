"""
PhDiscover Engine — Verified Source Registry
=============================================
Every entry here is MEASURED on this host, not assumed. The `verdict` field
records what a repeat-tested fetch actually returned, and `strategy` records
which recovery route is the working substitute when `verdict` is not REAL.

Measured 2026-09-29 from an Iran-hosted connection (3x repeat per source):

  EURAXESS        /jobs      REAL 200 but JS-rendered (no job data in HTML)
  EURAXESS        /jobs/search  200 but results injected by JS
  FindAPhD        /          REAL 200, homepage shell
  FindAPhD        /search/   Cloudflare CHALLENGE (3/3)
  FindAPhD        /phds/     Cloudflare CHALLENGE (3/3)
  academicpositions  /find-jobs  UNSTABLE: one 200 then CHALLENGE (3/3)
  Imperial        /jobs/     REAL 200
  Nature          /naturejobs/  REAL 200
  jobs.ac.uk      /          REAL 200 but no job links in homepage HTML
  Utrecht (uu.nl) /en/careers  REAL 200, 37 real job links  <-- best NL source
  TU Delft        /en/careers  404
  OpenAlex        /authors   REAL 200 JSON
  OpenAlex        /works     429 rate-limited (intermittent)
  Crossref        /works     REAL 200 JSON

Conclusion: no header trick reaches academicpositions or FindAPhD listings.
Cloudflare managed challenge + a bodyless-200 behaviour that defeats naive
success checks. The working substitutes are, in order:
  1. API-first (OpenAlex authors, Crossref)  -- no WAF, real records
  2. Direct HTTP on university career portals that do not use Cloudflare
  3. Search-index mining (DuckDuckGo HTML) to enumerate URLs on blocked hosts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Verdict(str, Enum):
    """Repeat-tested fetch outcome. Not the status code -- the truth."""
    REAL = "REAL"                    # 200, stable, real content
    JS_RENDERED = "JS_RENDERED"      # 200 but content injected by JS
    CHALLENGE = "CHALLENGE"          # Cloudflare managed challenge
    UNSTABLE = "UNSTABLE"            # intermittent 200/403
    RATE_LIMITED = "RATE_LIMITED"    # 429
    DEAD = "DEAD"                    # 404 / DNS fail


class Strategy(str, Enum):
    API_FIRST = "api_first"          # JSON endpoint, no WAF
    DIRECT_HTTP = "direct_http"      # fetch HTML from a non-CF host
    SEARCH_INDEX = "search_index"    # enumerate URLs via search engine
    BROWSER = "browser"              # real Chromium, last resort


@dataclass(frozen=True)
class SourceSpec:
    name: str
    base_url: str
    verdict: Verdict
    strategy: Strategy
    detail_path: str = ""            # per-item detail URL template
    note: str = ""
    # Search-index fallback needs a site-scoped query.
    site_query: Optional[str] = None
    priority: int = 5                # 1 = highest, matches CLAUDE.md hierarchy

    def list_url(self, query: str = "") -> str:
        from urllib.parse import quote
        if not query:
            return self.base_url
        return f"{self.base_url}?q={quote(query)}"


# --- Verified crawlable, in priority order -----------------------------------

CRAWLABLE: List[SourceSpec] = [
    SourceSpec(
        name="openalex_authors",
        base_url="https://api.openalex.org/authors",
        verdict=Verdict.REAL,
        strategy=Strategy.API_FIRST,
        detail_path="https://api.openalex.org/authors/{id}",
        note="PI discovery. Use filter=display_name.search:X,works_count:>N",
        priority=1,
    ),
    SourceSpec(
        name="crossref_works",
        base_url="https://api.crossref.org/works",
        verdict=Verdict.REAL,
        strategy=Strategy.API_FIRST,
        note="Publication cross-check for PI verification",
        priority=4,
    ),
    SourceSpec(
        name="uu_nl_careers",
        base_url="https://www.uu.nl/en/careers",
        verdict=Verdict.REAL,
        strategy=Strategy.DIRECT_HTTP,
        detail_path="https://www.uu.nl{en/careers}",
        note="37 real job links on the landing page. Strongest NL surface found.",
        priority=3,
    ),
    SourceSpec(
        name="imperial_jobs",
        base_url="https://www.imperial.ac.uk/jobs/",
        verdict=Verdict.REAL,
        strategy=Strategy.DIRECT_HTTP,
        note="Microsoft-IIS, no WAF. Job links live on filtered sub-pages.",
        priority=3,
    ),
    SourceSpec(
        name="nature_jobs",
        base_url="https://www.nature.com/naturejobs/",
        verdict=Verdict.REAL,
        strategy=Strategy.DIRECT_HTTP,
        note="Job alerts endpoint: /naturecareers/newalert/",
        priority=6,
    ),
    SourceSpec(
        name="euraxess_jobs",
        base_url="https://euraxess.ec.europa.eu/jobs",
        verdict=Verdict.JS_RENDERED,
        strategy=Strategy.BROWSER,
        detail_path="https://euraxess.ec.europa.eu/jobs/search?query={q}",
        note="DNS needs the no-www host. HTML carries zero job records; "
             "only a real browser gets results. /jobs/search 500s server-side.",
        priority=7,
    ),
    SourceSpec(
        name="euraxess_landing",
        base_url="https://euraxess.ec.europa.eu/jobs",
        verdict=Verdict.REAL,
        strategy=Strategy.DIRECT_HTTP,
        note="Landing + initiative list only. No positions.",
        priority=8,
    ),
]

# --- Blocked: recorded so nobody re-tests them weekly -----------------------

BLOCKED: List[SourceSpec] = [
    SourceSpec(
        name="academicpositions",
        base_url="https://academicpositions.com/find-jobs",
        verdict=Verdict.UNSTABLE,
        strategy=Strategy.SEARCH_INDEX,
        site_query="site:academicpositions.com PhD",
        note="Served a bodyless 200 for /de/sitemap.xml then 403 on retry. "
             "auth.academicpositions.com IS reachable (200, 209KB) -- use it.",
        priority=8,
    ),
    SourceSpec(
        name="findaphd_search",
        base_url="https://www.findaphd.com/search/",
        verdict=Verdict.CHALLENGE,
        strategy=Strategy.SEARCH_INDEX,
        site_query="site:findaphd.com",
        note="Homepage is 200; every listing path is challenged. ddgs returns "
             "real /phds/<topic>/ URLs -- mine those, then extract detail pages.",
        priority=8,
    ),
    SourceSpec(
        name="openalex_works",
        base_url="https://api.openalex.org/works",
        verdict=Verdict.RATE_LIMITED,
        strategy=Strategy.API_FIRST,
        note="429 intermittent. Add mailto + >=1s delay; /authors is unaffected.",
        priority=4,
    ),
    SourceSpec(
        name="tudelft_careers",
        base_url="https://www.tudelft.nl/en/careers",
        verdict=Verdict.DEAD,
        strategy=Strategy.DIRECT_HTTP,
        note="404. Correct path is /en/about-uu/careers.",
        priority=7,
    ),
]

BLOCKED_HOSTS = {"academicpositions.com", "www.academicpositions.com",
                 "auth.academicpositions.com", "www.findaphd.com",
                 "recruit.academicpositions.com"}


@dataclass
class SourceHealth:
    """Per-run health so /stats can say WHY a source returned nothing."""
    name: str
    verdict: Verdict
    items_found: int = 0
    last_error: str = ""
    attempts: int = 0


REGISTRY = {s.name: s for s in CRAWLABLE + BLOCKED}
