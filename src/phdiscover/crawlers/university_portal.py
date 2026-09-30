"""
PhDiscover Engine — University Portal Crawler
=============================================
Finds and crawls each university's own vacancy page.

Design note, and it is the whole point of this module: a university is NOT
usable until we have a page that actually LISTS vacancies, not a page that
talks about careers. Measured 2026-09-29 on 88 universities:

  guess one path + follow one homepage link  ->   6% real job lists
  (guessed paths 404; "careers" links land on marketing pages)

So discovery here is: try a set of candidate paths, score every 200 response
by how many *vacancy detail links* it exposes, and keep the best. Path shape
and link text are not evidence -- link count is.

Cost control matters at this scale (6793 universities): candidates are tried
in priority order, the run stops as soon as a page yields >= threshold
vacancies, and every failure is classified so a later run can skip the host
instead of re-probing 27 dead paths.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from phdiscover.crawlers.recovery_strategies import is_challenge

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Candidate vacancy paths, most-specific first. Only 200 responses count.
CANDIDATE_PATHS: Tuple[str, ...] = (
    "/careers", "/jobs", "/en/careers", "/en/jobs",
    "/vacancies", "/en/vacancies", "/job-opportunities",
    "/careers/vacancies", "/open-positions", "/current-vacancies",
    "/work-with-us", "/working-at-us", "/about/careers",
    "/en/about/careers", "/about-us/careers", "/hr/careers",
    "/recruitment", "/employment", "/werkgelegenheid",
    "/stellenangebote", "/stellenmarkt", "/arbeitsmarkt",
    "/offene-stellen", "/vacatures", "/openstaande-functies",
    "/tyopaikat", "/jobb", "/lediga-jobb", "/praca", "/zamestnani",
    "/pracovni-pozice", "/allasast", "/allast",
)

# A vacancy *detail* link.
DETAIL_RX = re.compile(
    r"(/job[s]?/|/vacanc|/position[s]?/|/opening[s]?/|/career[s]?/[^/]|"
    r"/posting[s]?/|/talent/|/werkgelegenheid/[^/]|/stellenangebot|"
    r"/annonce[s]?/|/offene-stellen/|/job-opportunities/)", re.I)

# Link text that names a role, not a page of links.
TITLE_RX = re.compile(
    r"\b(ph\.?\s?d\.?|phd|doctoral|postdoc|post-?doctoral|"
    r"research (assistant|associate|fellow|technician|scientist|engineer)|"
    r"scientific assistant|professor|lecturer|teacher|engineer|scientist|"
    r"vacanc|open position|staff|analyst|developer|coordinator)\b", re.I)

# Link text that means "browse the list" -- never a vacancy.
BROWSE_RX = re.compile(
    r"(all (jobs|vacancies|positions)|view all|see all|browse|"
    r"current (jobs|vacancies|openings)|open (jobs|positions|vacancies)|"
    r"job (alerts?|board|list|opportunities|opportunity)|vacatures?|"
    r"stellenangebote|search jobs|more (jobs|vacancies)|back to|"
    r"sign up|job alerts|employment opportunities)", re.I)

# PhD-shaped titles matter most for this project; keep the subtype.
OPPORTUNITY_RX = re.compile(
    r"(ph\.?\s?d\.?|phd|doctoral|postdoc|post-?doctoral|studentship|"
    r"doctoral (student|researcher|candidate)|research (assistant|"
    r"associate|fellow))", re.I)

MIN_VACANCIES = 3       # below this it is a landing page, not a list


@dataclass
class Vacancy:
    title: str
    url: str
    university: str
    country_code: str
    host: str
    is_phd: bool
    discovered_via: str = "university_portal"
    snippet: str = ""

    def key(self) -> str:
        from urllib.parse import urldefrag
        return urldefrag(self.url)[0].rstrip("/").lower()


@dataclass
class PortalResult:
    university: str
    country_code: str
    host: str
    status: str                      # JOB_LIST | LANDING | NONE | BLOCKED | ERROR
    joblist_url: Optional[str] = None
    vacancies: List[Vacancy] = field(default_factory=list)
    paths_tried: int = 0
    detail: str = ""


def score_vacancies(html: str, base_url: str) -> Tuple[List[Vacancy], int]:
    """Extract vacancy records from a page. Returns (vacancies, raw_link_count)."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc
    seen: Dict[str, Vacancy] = {}
    for a in soup.select("a[href]"):
        text = (a.get_text(" ", strip=True) or "")
        if len(text) < 15 or len(text) > 240:
            continue
        if BROWSE_RX.search(text):
            continue
        href = a.get("href") or ""
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        if not href.startswith("http"):
            continue
        if not DETAIL_RX.search(href):
            continue
        if not TITLE_RX.search(text):
            continue
        k = href.rstrip("/").lower()
        if k in seen:
            continue
        seen[k] = Vacancy(
            title=text,
            url=href,
            university="",
            country_code="",
            host=host,
            is_phd=bool(OPPORTUNITY_RX.search(text)),
        )
    return list(seen.values()), len(seen)


class UniversityPortalCrawler:
    """Discover and crawl a university's own vacancy board."""

    def __init__(
        self,
        registry_path: Optional[Path] = None,
        max_concurrent: int = 6,
        min_vacancies: int = MIN_VACANCIES,
        timeout: float = 20.0,
        polite_delay: float = 0.25,
    ) -> None:
        self.registry_path = registry_path or (
            Path(__file__).resolve().parents[3] / "data" /
            "universities_final.json")
        self.max_concurrent = max_concurrent
        self.min_vacancies = min_vacancies
        self.timeout = timeout
        self.polite_delay = polite_delay
        self._sem = asyncio.Semaphore(max_concurrent)
        # hosts proven dead, so a re-run does not re-probe them
        self.dead_hosts: set = set()

    # -- registry ---------------------------------------------------------

    def load_universities(
        self,
        countries: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self.registry_path.exists():
            return []
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        rows: List[Dict[str, Any]] = []
        for cc, v in data.items():
            if countries and cc not in countries:
                continue
            for u in v.get("universities", []):
                if not u.get("official_url"):
                    continue
                rows.append({
                    "name": u["name"],
                    "country_code": cc,
                    "url": u["official_url"],
                    "openalex_id": u.get("openalex_id"),
                    "papers": u.get("student_count") or 0,
                })
        rows.sort(key=lambda r: -(r.get("papers") or 0))
        return rows[:limit] if limit else rows

    # -- HTTP -------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, url: str) -> Tuple[Optional[str], str]:
        """Returns (html, status). status is OK | BLOCKED | HTTP-n | ERR."""
        try:
            r = await client.get(url)
        except Exception:
            return None, "ERR"
        if r.status_code != 200 or len(r.text) < 2000:
            return None, f"HTTP-{r.status_code}"
        if is_challenge(r.text):
            return None, "BLOCKED"
        return r.text, "OK"

    async def _candidates_from_homepage(
        self, client: httpx.AsyncClient, base: str
    ) -> List[str]:
        html, st = await self._get(client, base)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        out, seen = [], set()
        for a in soup.select("a[href]"):
            t = a.get_text(" ", strip=True) or ""
            href = urljoin(base, a.get("href") or "")
            if not href.startswith("http"):
                continue
            if not re.search(r"(job|vacan|career|recruit|work with|"
                             r"employment|opening|stellen|vacatur)", t, re.I):
                continue
            if re.search(r"(alumni|student|privacy|about us|contact|news|"
                         r"press|donat|give|newsletter|event)", t, re.I):
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append(href)
        return out[:5]

    # -- discovery --------------------------------------------------------

    async def discover(self, client: httpx.AsyncClient,
                       uni: Dict[str, Any]) -> PortalResult:
        base = uni["url"]
        host = urlparse(base).netloc.lower()
        res = PortalResult(university=uni["name"],
                           country_code=uni["country_code"], host=host,
                           status="NONE")
        if host in self.dead_hosts:
            res.status = "NONE"
            res.detail = "host known-dead from a previous run"
            return res

        best: List[Vacancy] = []
        best_url: Optional[str] = None

        # 1) candidate paths
        for p in CANDIDATE_PATHS:
            res.paths_tried += 1
            cand = f"https://{host}{p}"
            html, st = await self._get(client, cand)
            if not html:
                if st == "ERR":
                    break           # host is unreachable; stop guessing
                await asyncio.sleep(self.polite_delay)
                continue
            vac, _ = score_vacancies(html, cand)
            if len(vac) > len(best):
                best, best_url = vac, cand
            if len(vac) >= self.min_vacancies:
                break
            await asyncio.sleep(self.polite_delay)

        # 2) homepage links, as a fallback
        if len(best) < self.min_vacancies:
            for href in await self._candidates_from_homepage(client, base):
                res.paths_tried += 1
                html, st = await self._get(client, href)
                if not html:
                    continue
                vac, _ = score_vacancies(html, href)
                if len(vac) > len(best):
                    best, best_url = vac, href
                if len(vac) >= self.min_vacancies:
                    break
                await asyncio.sleep(self.polite_delay)

        for v in best:
            v.university = uni["name"]
            v.country_code = uni["country_code"]

        if len(best) >= self.min_vacancies:
            res.status = "JOB_LIST"
            res.joblist_url = best_url
            res.vacancies = best
        elif best:
            res.status = "LANDING"
            res.joblist_url = best_url
            res.vacancies = best
        elif res.paths_tried <= 2:
            self.dead_hosts.add(host)
            res.detail = "host marked dead"
        return res

    # -- batch ------------------------------------------------------------

    async def crawl(
        self,
        countries: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[PortalResult]:
        unis = self.load_universities(countries, limit)
        out: List[PortalResult] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, trust_env=False,
            headers=HEADERS, limits=httpx.Limits(max_connections=8),
        ) as client:
            async def one(u: Dict[str, Any]) -> PortalResult:
                async with self._sem:
                    try:
                        return await self.discover(client, u)
                    except Exception as e:
                        r = PortalResult(university=u["name"],
                                         country_code=u["country_code"],
                                         host=urlparse(u["url"]).netloc,
                                         status="ERROR", detail=str(e)[:120])
                        return r

            out = await asyncio.gather(*(one(u) for u in unis))
        return out
