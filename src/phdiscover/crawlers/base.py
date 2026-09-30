"""
PhDiscover Engine - Base Crawler
"""

from __future__ import annotations

import asyncio
import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import structlog
import httpx
from bs4 import BeautifulSoup

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.models import CrawlLog, FailureType
from phdiscover.models.discovery import RawPosition

logger = structlog.get_logger(__name__)


class BaseCrawler(ABC):
    """Base class for all crawlers"""

    # UA pool: rotate per-request. A single hardcoded browser UA is what most
    # Cloudflare WAFs fingerprint -- a stable, honest one is the first fallback.
    _UA_POOL: List[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    ]
    _UA_HONEST = "PhDiscover Research Bot/0.2 (+https://github.com/saeidsoraghi/phdiscover-engine; research crawler)"

    def __init__(self, source_name: str):
        self.source_name = source_name
        self.settings = get_settings()
        self.logger = logger.bind(crawler=source_name)
        self.parser_version = "v2"  # v1 returned empty HTML on 403-gated listing pages

        self.http_client = httpx.AsyncClient(
            timeout=self.settings.crawling.default_timeout,
            headers=self._base_headers(),
            follow_redirects=True,
            trust_env=False,
        )

        self._semaphore = asyncio.Semaphore(self.settings.crawling.max_concurrent)

    @classmethod
    def _base_headers(cls, ua: Optional[str] = None) -> Dict[str, str]:
        return {
            "User-Agent": ua or cls._UA_POOL[0],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }

    def _rotate_headers(self, attempt: int) -> Dict[str, str]:
        """Escalating header strategy -- see _fetch for the ladder."""
        if attempt == 0:
            return self._base_headers()
        if attempt == 1:
            return self._base_headers(ua=self._UA_POOL[attempt % len(self._UA_POOL)])
        if attempt == 2:
            return self._base_headers(ua=self._UA_POOL[attempt % len(self._UA_POOL)])
        return self._base_headers(ua=self._UA_HONEST)

    def _looks_like_challenge(self, body: str) -> bool:
        """Cloudflare/JsDelivr interstitials ship HTTP 200 or 403 with a real title.
        A 200 whose body is the challenge page is NOT a success -- CLAUDE.md
        forbids treating it as one."""
        head = body[:4000]
        markers = (
            "just a moment",
            "performing security verification",
            "verifying you are not a bot",
            "attention required! | cloudflare",
            "checking your browser before accessing",
            "enable javascript and cookies to continue",
        )
        low = head.lower()
        return any(m in low for m in markers)

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Base URL for this source"""
        pass

    @property
    @abstractmethod
    def search_paths(self) -> List[str]:
        """Search paths to crawl"""
        pass

    @abstractmethod
    async def parse_listing_page(self, html: str, url: str) -> List[RawPosition]:
        """Parse listing page for position links"""
        pass

    @abstractmethod
    async def parse_detail_page(self, html: str, url: str) -> Optional[RawPosition]:
        """Parse detail page for full position data"""
        pass

    async def _rate_limit(self) -> None:
        await asyncio.sleep(1.0 / self.settings.crawling.max_concurrent)

    async def _fetch(self, url: str) -> Optional[str]:
        """Fetch page with rate limiting + 403 recovery ladder.

        Measured on this host (2026-09): FindAPhD's /search/ returns 403
        "Just a moment..." on every header combination, and the homepage is
        200 with any UA. So a 403 is not always a header problem -- the
        ladder stops escalating and reports honestly instead of burning retries.
        """
        async with self._semaphore:
            await self._rate_limit()
            last_status = None
            for attempt in range(self.settings.recovery.max_attempts):
                try:
                    response = await self.http_client.get(
                        url, headers=self._rotate_headers(attempt)
                    )
                    last_status = response.status_code
                    if response.status_code == 200:
                        body = response.text
                        if self._looks_like_challenge(body):
                            self.logger.warning(
                                "challenge_body_on_200", url=url, attempt=attempt
                            )
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return body
                    self.logger.warning(
                        "fetch_failed", url=url, status=response.status_code,
                        attempt=attempt,
                    )
                    if response.status_code in (401, 403, 404, 451):
                        break  # header rotation cannot fix these
                    await asyncio.sleep(2 ** attempt)
                except Exception as e:
                    self.logger.warning(
                        "fetch_error", url=url, error=str(e)[:200], attempt=attempt
                    )
                    await asyncio.sleep(2 ** attempt)
            self.logger.error(
                "fetch_exhausted", url=url, last_status=last_status,
                source=self.source_name,
            )
            return None

    def _create_crawl_log(
        self,
        url: str,
        success: bool = False,
        status_code: Optional[int] = None,
        error_type: Optional[FailureType] = None,
        error_message: Optional[str] = None,
        items_extracted: int = 0,
    ) -> CrawlLog:
        return CrawlLog(
            source_name=self.source_name,
            url=url,
            success=success,
            status_code=status_code,
            error_type=error_type,
            error_message=error_message,
            items_extracted=items_extracted,
            parser_version=self.parser_version,
        )

    def _classify_failure(self, error: Exception, url: str = "") -> FailureType:
        error_str = str(error).lower()
        if any(kw in error_str for kw in ["timeout", "timed out"]):
            return FailureType.TIMEOUT
        elif any(kw in error_str for kw in ["rate limit", "429", "too many requests"]):
            return FailureType.RATE_LIMIT
        elif any(kw in error_str for kw in ["robots.txt", "disallowed", "403 forbidden"]):
            return FailureType.ROBOTS_BLOCK
        elif any(kw in error_str for kw in ["javascript", "js required", "dynamic"]):
            return FailureType.JS_REQUIRED
        elif any(kw in error_str for kw in ["empty", "no content"]):
            return FailureType.EMPTY_CONTENT
        elif any(kw in error_str for kw in ["selector", "element not found"]):
            return FailureType.SELECTOR_FAILURE
        elif any(kw in error_str for kw in ["network", "connection", "dns"]):
            return FailureType.NETWORK_ERROR
        elif any(kw in error_str for kw in ["pdf", "document"]):
            return FailureType.PDF_FAILURE
        elif any(kw in error_str for kw in ["auth", "unauthorized", "401", "login"]):
            return FailureType.AUTH_REQUIRED
        elif any(kw in error_str for kw in ["captcha", "challenge"]):
            return FailureType.CAPTCHA
        return FailureType.UNKNOWN

    async def crawl(self, keywords: List[str] = None) -> AgentResult:
        """Main crawl entry point"""
        all_positions = []
        crawl_logs = []

        for path in self.search_paths:
            url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
            self.logger.info("crawling_path", path=path, url=url)

            html = await self._fetch(url)
            if not html:
                crawl_logs.append(self._create_crawl_log(
                    url, success=False, error_type=FailureType.EMPTY_CONTENT
                ))
                continue

            positions = await self.parse_listing_page(html, url)
            crawl_logs.append(self._create_crawl_log(
                url, success=True, status_code=200, items_extracted=len(positions)
            ))

            # For each position, fetch detail page
            for pos in positions:
                if pos.application_url:
                    detail_html = await self._fetch(str(pos.application_url))
                    if detail_html:
                        detail_pos = await self.parse_detail_page(detail_html, str(pos.application_url))
                        if detail_pos:
                            # Merge detail info
                            pos.description = detail_pos.description or pos.description
                            pos.requirements = detail_pos.requirements or pos.requirements
                            pos.funding_info = detail_pos.funding_info or pos.funding_info
                            pos.application_deadline = detail_pos.application_deadline or pos.application_deadline

                all_positions.append(pos)

        # Deduplicate
        seen = set()
        unique_positions = []
        for pos in all_positions:
            key = f"{pos.title}_{pos.university}_{pos.country}"
            if key not in seen:
                seen.add(key)
                unique_positions.append(pos)

        self.logger.info("crawl_complete", source=self.source_name, positions=len(unique_positions))

        return AgentResult(
            success=True,
            data=unique_positions,
            agent_name=f"crawler_{self.source_name}",
            metadata={
                "positions_found": len(unique_positions),
                "paths_crawled": len(self.search_paths),
                "crawl_logs": [log.model_dump() for log in crawl_logs],
            },
        )


class UniversityPortalCrawler(BaseCrawler):
    """Crawler for university-specific portals"""

    def __init__(self, university: str, base_url: str, search_paths: List[str]):
        self.university = university
        self._base_url = base_url
        self._search_paths = search_paths
        super().__init__(f"university_{university.lower().replace(' ', '_')}")

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def search_paths(self) -> List[str]:
        return self._search_paths

    async def parse_listing_page(self, html: str, url: str) -> List[RawPosition]:
        # Generic parsing - would be customized per university
        soup = BeautifulSoup(html, 'html.parser')
        positions = []

        # Common selectors for position listings
        selectors = [
            'a[href*="phd"]', 'a[href*="PhD"]', 'a[href*="doctoral"]',
            'a[href*="vacancy"]', 'a[href*="position"]', 'a[href*="job"]',
            '.job-listing a', '.position-listing a', '.vacancy a',
        ]

        links = set()
        for selector in selectors:
            for a in soup.select(selector):
                href = a.get('href')
                if href:
                    links.add(href)

        for link in list(links)[:50]:  # Limit
            pos = RawPosition(
                source_name=self.source_name,
                source_url=url,
                title="",  # Will be filled from detail page
                university=self.university,
                country=self._infer_country(),
                application_url=link if link.startswith('http') else f"{self.base_url.rstrip('/')}/{link.lstrip('/')}",
            )
            positions.append(pos)

        return positions

    async def parse_detail_page(self, html: str, url: str) -> Optional[RawPosition]:
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()

        # Extract title
        title_elem = soup.select_one('h1, h2, .title, .position-title, .job-title')
        title = title_elem.get_text(strip=True) if title_elem else ""

        # Extract description
        desc_elem = soup.select_one('.description, .content, .job-description, .position-description, main')
        description = desc_elem.get_text(strip=True)[:2000] if desc_elem else text[:2000]

        # Extract deadline
        deadline = self._extract_deadline(text)

        # Extract funding
        funding = self._extract_funding(text)

        return RawPosition(
            source_name=self.source_name,
            source_url=url,
            title=title[:500],
            university=self.university,
            country=self._infer_country(),
            description=description,
            application_url=url,
            application_deadline=deadline,
            funding_info=funding,
        )

    def _infer_country(self) -> str:
        name = self.university.lower()
        if any(w in name for w in ["toronto", "ubc", "mcgill", "waterloo", "alberta"]):
            return "CA"
        elif any(w in name for w in ["delft", "amsterdam", "erasmus", "leiden"]):
            return "NL"
        elif any(w in name for w in ["munich", "heidelberg", "berlin", "bonn"]):
            return "DE"
        elif any(w in name for w in ["helsinki", "aalto", "turku"]):
            return "FI"
        elif any(w in name for w in ["auckland", "otago", "victoria"]):
            return "NZ"
        return "UNKNOWN"

    def _extract_deadline(self, text: str):
        import re
        from datetime import datetime
        patterns = [
            r'deadline[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'due[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"]:
                    try:
                        return datetime.strptime(date_str, fmt).date()
                    except ValueError:
                        continue
        return None

    def _extract_funding(self, text: str) -> Optional[str]:
        import re
        patterns = [
            r'(?:funded|stipend|salary|funding)[:\s]+([^.]+)',
            r'(?:€|\$|£|CAD|USD|EUR)\s*[\d,]+(?:\s*(?:per|/)\s*(?:month|year|annum))?',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)[:200]
        return None