"""
PhDiscover Engine - FindAPhD Crawler
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

import structlog
from bs4 import BeautifulSoup

from phdiscover.crawlers.base import BaseCrawler
from phdiscover.models.discovery import RawPosition

logger = structlog.get_logger(__name__)


class FindAPhDCrawler(BaseCrawler):
    """Crawler for FindAPhD.com"""

    def __init__(self):
        super().__init__("findaphd")
        self.parser_version = "v1"

    @property
    def base_url(self) -> str:
        return "https://www.findaphd.com"

    @property
    def search_paths(self) -> List[str]:
        return [
            "/phds/biomechanics/",
            "/phds/motor-control/",
            "/phds/gait-analysis/",
            "/phds/sensorimotor/",
            "/phds/wearable-technology/",
            "/phds/rehabilitation/",
            "/phds/human-movement/",
            "/phds/sports-science/",
            "/phds/kinesiology/",
            "/search/?keyword=biomechanics+phd",
            "/search/?keyword=motor+control+phd",
        ]

    async def parse_listing_page(self, html: str, url: str) -> List[RawPosition]:
        soup = BeautifulSoup(html, 'html.parser')
        positions = []

        # FindAPhD PhD cards
        phd_cards = soup.select('.phd-result, .result-item, .phd-card, article.phd')

        for card in phd_cards[:30]:
            try:
                # Title and link
                title_elem = card.select_one('h3 a, h2 a, .phd-title a, .title a')
                if not title_elem:
                    continue

                title = title_elem.get_text(strip=True)
                link = title_elem.get('href', '')

                if not link:
                    continue

                if not link.startswith('http'):
                    link = f"{self.base_url}{link}"

                # University
                uni_elem = card.select_one('.university, .institution, .org-name')
                university = uni_elem.get_text(strip=True) if uni_elem else ""

                # Location
                loc_elem = card.select_one('.location, .country, .city')
                location = loc_elem.get_text(strip=True) if loc_elem else ""

                # Funding
                funding_elem = card.select_one('.funding, .stipend, .salary')
                funding = funding_elem.get_text(strip=True) if funding_elem else ""

                # Deadline
                deadline_elem = card.select_one('.deadline, .closing-date')
                deadline_text = deadline_elem.get_text(strip=True) if deadline_elem else ""
                deadline = self._parse_deadline(deadline_text)

                pos = RawPosition(
                    source_name=self.source_name,
                    source_url=url,
                    title=title[:500],
                    university=university or "Unknown",
                    country=self._extract_country(location),
                    city=self._extract_city(location),
                    application_url=link,
                    application_deadline=deadline,
                    funding_info=funding,
                    raw_data={"location_raw": location},
                )
                positions.append(pos)

            except Exception as e:
                logger.warning("parse_phd_card_failed", error=str(e))
                continue

        return positions

    async def parse_detail_page(self, html: str, url: str) -> Optional[RawPosition]:
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()

        # Title
        title_elem = soup.select_one('h1, .phd-title, .page-title')
        title = title_elem.get_text(strip=True) if title_elem else ""

        # University
        uni_elem = soup.select_one('.university, .institution, .org-name, .host-organisation')
        university = uni_elem.get_text(strip=True) if uni_elem else ""

        # Location
        loc_elem = soup.select_one('.location, .country, .city')
        location = loc_elem.get_text(strip=True) if loc_elem else ""

        # Description
        desc_elem = soup.select_one('.description, .phd-description, .content, main')
        description = desc_elem.get_text(strip=True)[:3000] if desc_elem else text[:3000]

        # Deadline
        deadline = None
        deadline_elem = soup.select_one('.deadline, .closing-date, .application-deadline')
        if deadline_elem:
            deadline = self._parse_deadline(deadline_elem.get_text(strip=True))

        # Funding
        funding = None
        funding_elem = soup.select_one('.funding, .stipend, .salary, .financial-support')
        if funding_elem:
            funding = funding_elem.get_text(strip=True)
        else:
            funding = self._extract_funding(text)

        # Requirements
        requirements = self._extract_requirements(soup)

        # Supervisor
        supervisor = None
        sup_elem = soup.select_one('.supervisor, .academic-lead, .principal-investigator')
        if sup_elem:
            supervisor = sup_elem.get_text(strip=True)

        return RawPosition(
            source_name=self.source_name,
            source_url=url,
            title=title[:500],
            university=university or "Unknown",
            country=self._extract_country(location),
            city=self._extract_city(location),
            description=description,
            application_url=url,
            application_deadline=deadline,
            funding_info=funding,
            requirements=requirements,
            pi_name=supervisor,
            raw_data={"full_text": text[:5000]},
        )

    def _parse_deadline(self, text: str) -> Optional[datetime]:
        patterns = [
            r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
            r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"]:
                    try:
                        return datetime.strptime(date_str, fmt).date()
                    except ValueError:
                        continue
        return None

    def _extract_country(self, location: str) -> str:
        if not location:
            return "UNKNOWN"
        location_lower = location.lower()
        countries = {
            "ca": "CA", "canada": "CA",
            "nl": "NL", "netherlands": "NL", "holland": "NL",
            "de": "DE", "germany": "DE", "deutschland": "DE",
            "fi": "FI", "finland": "FI", "suomi": "FI",
            "nz": "NZ", "new zealand": "NZ",
        }
        for key, code in countries.items():
            if key in location_lower:
                return code
        return "UNKNOWN"

    def _extract_city(self, location: str) -> Optional[str]:
        if not location:
            return None
        parts = location.split(',')
        return parts[0].strip() if parts else None

    def _extract_funding(self, text: str) -> Optional[str]:
        patterns = [
            r'(?:funded|stipend|salary|funding)[:\s]+([^.]+)',
            r'(?:€|\$|£|CAD|USD|EUR)\s*[\d,]+(?:\s*(?:per|/)\s*(?:month|year|annum))?',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)[:200]
        return None

    def _extract_requirements(self, soup: BeautifulSoup) -> List[str]:
        requirements = []
        req_sections = soup.select('.requirements, .qualifications, .criteria, .entry-requirements')
        for section in req_sections:
            items = section.select('li, p')
            for item in items:
                text = item.get_text(strip=True)
                if len(text) > 20:
                    requirements.append(text)
        return requirements[:20]