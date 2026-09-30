"""
PhDiscover Engine — Recovery Strategies
======================================
Concrete substitutes for the sources that cannot be fetched directly.

Each strategy returns real records with real URLs. Nothing here fabricates a
position: if a route yields nothing, it reports nothing.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from phdiscover.crawlers.registry import SourceSpec, Verdict

# Cloudflare interstitials hide their marker text inside <script>, so a scan of
# visible text misses them entirely. Detection must run on raw bytes.
CF_MARKERS = (
    "_cf_chl_opt",
    "__cf_chl_tk",
    "challenges.cloudflare.com",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/",
)

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PHD_RE = re.compile(
    r"\b(ph\.?\s?d\.?|phd|doctoral|doctorate|studentship|studentships|"
    r"post-?doctoral|postdoc)\b", re.I)
JOB_RE = re.compile(
    r"\b(vacanc|job|jobs|position|opening|career|employment|"
    r"research (assistant|associate|fellow)|traineeship)\b", re.I)
NOISE_RE = re.compile(
    r"\b(privacy|cookie|login|sign in|newsletter|subscribe|alerts|"
    r"job alerts|about us|contact|terms of use|accessibility)\b", re.I)


@dataclass
class RecoveredLink:
    """A candidate record discovered by any strategy. Always carries a real URL."""
    title: str
    url: str
    source: str
    host: str
    snippet: str = ""
    discovered_via: str = ""

    def is_position(self) -> bool:
        return bool(PHD_RE.search(self.title)) or bool(PHD_RE.search(self.snippet))


def is_challenge(body: str) -> bool:
    """True for ANY Cloudflare interstitial, including a hollow 200."""
    head = body[:30000].lower()
    return any(m in head for m in CF_MARKERS)


def is_hollow_200(status: int, body: str, expect: Optional[str] = None) -> bool:
    """A 200 that is not the page you asked for.

    academicpositions.com served HTTP 200 with 2902 bytes and 16 <loc> elements
    that were all part of the challenge template. Status + size both lie.
    """
    if status != 200:
        return False
    if is_challenge(body):
        return True
    if len(body) < 3000:
        return True
    if expect and expect.lower() not in body.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Strategy 1: API-first
# ---------------------------------------------------------------------------

class OpenAlexAuthors:
    """PI discovery. The project's core surface, and it is fully open.

    Two search shapes, because they answer different questions:

    * ``find_pis_by_name`` uses ``display_name.search`` -- matches PI *names*
      only. "biomechanics" returns one research group, not researchers.
    * ``find_pis_by_topic`` is the correct PI-first path: search
      ``title_and_abstract`` on works, then read ``authorships`` to get real
      named researchers with ORCID and institution. Measured 2026-09-29:
      "sensorimotor control" -> Goodale, Milner, Wolpert with ORCIDs;
      name search for the same term returns nothing useful.
    """

    BASE_AUTHORS = "https://api.openalex.org/authors"
    BASE_WORKS = "https://api.openalex.org/works"

    def __init__(self, mailto: str = "") -> None:
        self.mailto = mailto

    def _headers(self) -> Dict[str, str]:
        ua = "PhDiscover-Research/0.2"
        if self.mailto:
            ua += f" (mailto:{self.mailto})"
        return {"User-Agent": ua, "Accept": "application/json"}

    def _params(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(extra)
        if self.mailto:
            p["mailto"] = self.mailto
        return p

    async def _get(self, client: httpx.AsyncClient, url: str,
                   params: Dict[str, Any], tries: int = 4) -> Optional[httpx.Response]:
        """/works is intermittently 429. Back off, do not give up."""
        r = None
        for i in range(tries):
            r = await client.get(url, params=params, headers=self._headers())
            if r.status_code == 200:
                return r
            if r.status_code != 429:
                return r
            await asyncio.sleep(3.0 * (i + 1))
        return r

    async def find_pis_by_name(
        self,
        topic: str,
        min_works: int = 30,
        per_page: int = 25,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Dict[str, Any]]:
        params = self._params({
            "filter": f"display_name.search:{topic},works_count:>{min_works}",
            "per-page": per_page,
        })
        own = client is None
        client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=True, trust_env=False)
        try:
            r = await self._get(client, self.BASE_AUTHORS, params)
            if r is None or r.status_code != 200:
                return []
            out = []
            for a in r.json().get("results", []):
                insts = a.get("last_known_institutions") or []
                out.append({
                    "name": a.get("display_name"),
                    "openalex_id": a.get("id"),
                    "orcid": a.get("orcid"),
                    "works_count": a.get("works_count"),
                    "cited_by_count": a.get("cited_by_count"),
                    "institution": insts[0]["display_name"] if insts else None,
                    "country": (insts[0].get("country_code") if insts else None),
                    "discovered_via": "openalex_name_search",
                })
            return out
        finally:
            if own:
                await client.aclose()

    async def find_pis_by_topic(
        self,
        topic: str,
        per_page: int = 25,
        top_n: int = 30,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Dict[str, Any]]:
        """PI-first discovery: topic -> works -> authorships -> real PIs."""
        params = self._params({
            "filter": f"title_and_abstract.search:{topic}",
            "per-page": min(per_page, 50),
            "sort": "cited_by_count:desc",
        })
        own = client is None
        client = client or httpx.AsyncClient(
            timeout=40, follow_redirects=True, trust_env=False)
        try:
            r = await self._get(client, self.BASE_WORKS, params)
            if r is None or r.status_code != 200:
                return []
            agg: Dict[str, Dict[str, Any]] = {}
            for w in r.json().get("results", []):
                for au in (w.get("authorships") or [])[:3]:
                    a = au.get("author") or {}
                    aid = a.get("id")
                    if not aid:
                        continue
                    insts = au.get("institutions") or []
                    e = agg.setdefault(aid, {
                        "name": a.get("display_name"),
                        "openalex_id": aid,
                        "orcid": a.get("orcid"),
                        "institution": insts[0]["display_name"] if insts else None,
                        "country": (insts[0].get("country_code") if insts else None),
                        "papers_in_set": 0,
                        "cited_in_set": 0,
                        "example_papers": [],
                    })
                    e["papers_in_set"] += 1
                    e["cited_in_set"] += w.get("cited_by_count") or 0
                    if len(e["example_papers"]) < 3:
                        e["example_papers"].append({
                            "title": w.get("title"),
                            "year": w.get("publication_year"),
                            "cited_by": w.get("cited_by_count"),
                        })
            ranked = sorted(agg.values(), key=lambda x: -x["cited_in_set"])
            for e in ranked:
                e["discovered_via"] = "openalex_topic_works"
            return ranked[:top_n]
        finally:
            if own:
                await client.aclose()


# ---------------------------------------------------------------------------
# Strategy 2: direct HTTP with hollow-200 rejection
# ---------------------------------------------------------------------------

class DirectHttpLister:
    """Fetch a listing page and extract real job links, or report nothing."""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    async def list_links(
        self,
        client: Optional[httpx.AsyncClient] = None,
        limit: int = 60,
    ) -> List[RecoveredLink]:
        own = client is None
        client = client or httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": BROWSER_UA,
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            r = await client.get(self.spec.base_url)
            if is_hollow_200(r.status_code, r.text, expect="job"):
                return []
            if r.status_code != 200:
                return []
            return self.parse_links(r.text, r.url)
        finally:
            if own:
                await client.aclose()

    def parse_links(self, html: str, page_url: Any) -> List[RecoveredLink]:
        soup = BeautifulSoup(html, "html.parser")
        host = urlparse(str(page_url)).netloc
        seen: Dict[str, RecoveredLink] = {}
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            if len(title) < 20 or NOISE_RE.search(title):
                continue
            if not (PHD_RE.search(title) or JOB_RE.search(title)):
                continue
            if href.startswith("/"):
                href = f"{urlparse(str(page_url)).scheme}://{host}{href}"
            if not href.startswith("http"):
                continue
            if href in seen:
                continue
            seen[href] = RecoveredLink(
                title=title[:200], url=href, source=self.spec.name,
                host=host, discovered_via="direct_http",
            )
        return list(seen.values())[:200]


# ---------------------------------------------------------------------------
# Strategy 3: search-index mining -- the blocked-site escape hatch
# ---------------------------------------------------------------------------

class SearchIndexMiner:
    """Enumerate real URLs on a host that refuses to serve us.

    academicpositions.com and findaphd.com both refuse direct listing fetches.
    The search index already crawled them and will hand over title + URL +
    snippet. That is enough to build a candidate list, and each candidate keeps
    its real source URL so the verification agent can decide whether to trust
    it. Per CLAUDE.md this is a `search_snippet` source (priority 10) and can
    never alone satisfy email verification -- but it is real, and it works.

    Multi-engine on purpose. Measured 2026-09-29:
      * ``ddgs`` library    -> 7 real findaphd.com URLs, 200
      * raw DuckDuckGo HTML -> 202 "bots use DuckDuckGo too" challenge, 0 results
      * Brave               -> 429
    So the library is the primary engine and raw HTTP is only a fallback.
    """

    ENDPOINTS = (
        "https://html.duckduckgo.com/html/",
        "https://lite.duckduckgo.com/lite/",
    )

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    @staticmethod
    def _unwrap(href: str) -> str:
        """DDG wraps results in //duckduckgo.com/l/?uddg=<encoded>."""
        if "uddg=" in href:
            qs = parse_qs(urlparse(href if "//" in href else "https:" + href).query)
            if qs.get("uddg"):
                return unquote(qs["uddg"][0])
        return href

    def _build_query(self, query: str = "") -> str:
        q = self.spec.site_query or f"site:{self.spec.name}"
        return f"{q} {query}".strip() if query else q

    def _in_scope(self, url: str) -> bool:
        """Is this URL on the host we are trying to mine?

        The spec name is often a *role* ("findaphd_search"), not a hostname, so
        matching name-vs-host silently drops every result. Derive the host from
        the spec's own base_url instead, plus a small alias table.
        """
        host = urlparse(url).netloc.lower().lstrip("www.")
        base_host = urlparse(self.spec.base_url).netloc.lower().lstrip("www.")
        # site: query in the spec is the most reliable scope signal.
        for token in (self.spec.site_query or "").split():
            if token.startswith("site:") and token[5:].lower() in host:
                return True
        if base_host and (base_host in host or host in base_host):
            return True
        alias = self.spec.name.split("_")[0].lower()
        return bool(alias) and (alias in host or host in alias)

    def _ddgs_engine(self, q: str, limit: int) -> List[RecoveredLink]:
        """Primary engine: the ddgs library. Blocking-safe, no key needed."""
        try:
            from ddgs import DDGS
        except ImportError:
            return []
        try:
            with DDGS() as d:
                rows = list(d.text(q, max_results=limit))
        except Exception:
            return []
        out: List[RecoveredLink] = []
        for x in rows:
            href = x.get("href") or x.get("url") or ""
            if not href.startswith("http") or not self._in_scope(href):
                continue
            out.append(RecoveredLink(
                title=(x.get("title") or "")[:200],
                url=href,
                source=self.spec.name,
                host=urlparse(href).netloc,
                snippet=(x.get("body") or "")[:400],
                discovered_via="search_index:ddgs",
            ))
        return out

    async def _http_engine(self, client: httpx.AsyncClient, q: str,
                           limit: int) -> List[RecoveredLink]:
        """Fallback: raw DDG HTML. Often challenged (202), so try both hosts."""
        for ep in self.ENDPOINTS:
            try:
                r = await client.get(ep, params={"q": q})
            except Exception:
                continue
            if r.status_code != 200 or len(r.text) < 2000:
                continue
            if is_challenge(r.text):
                continue
            links = self._parse(r.text, limit)
            if links:
                return links
            await asyncio.sleep(2.0)
        return []

    async def mine(
        self,
        query: str = "",
        client: Optional[httpx.AsyncClient] = None,
        limit: int = 30,
    ) -> List[RecoveredLink]:
        q = self._build_query(query)
        links = await asyncio.to_thread(self._ddgs_engine, q, limit)
        if links:
            return links[:limit]
        own = client is None
        client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=True, trust_env=False,
            headers={"User-Agent": BROWSER_UA,
                     "Accept": "text/html,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            return (await self._http_engine(client, q, limit))[:limit]
        finally:
            if own:
                await client.aclose()

    def _parse(self, html: str, limit: int = 30) -> List[RecoveredLink]:
        soup = BeautifulSoup(html, "html.parser")
        out: List[RecoveredLink] = []
        seen = set()
        for a in soup.select("a.result__a, a.result-link, h2 a, a[href*='uddg=']"):
            href = self._unwrap(a.get("href") or "")
            if not href.startswith("http") or href in seen:
                continue
            if not self._in_scope(href):
                continue
            seen.add(href)
            title = a.get_text(" ", strip=True)
            snippet = ""
            parent = a.find_parent(["div", "article", "tr"])
            if parent:
                sn = parent.select_one(".result__snippet, .result-snippet")
                snippet = sn.get_text(" ", strip=True) if sn else ""
            out.append(RecoveredLink(
                title=title[:200], url=href, source=self.spec.name,
                host=urlparse(href).netloc, snippet=snippet[:400],
                discovered_via="search_index:http",
            ))
        return out
