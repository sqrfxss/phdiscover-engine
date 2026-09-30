"""
PhDiscover Engine - OpenAlex Crawler
Uses PyAlex library for research graph data
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from pyalex import Authors, Concepts, Institutions, Works

from phdiscover.crawlers.base import BaseCrawler
from phdiscover.models.discovery import RawPosition

logger = structlog.get_logger(__name__)


import pyalex
pyalex.config.email = "phdiscover@example.com"

class OpenAlexCrawler(BaseCrawler):
    """Crawler for OpenAlex research graph - discovers PIs and positions via grants/publications"""

    def __init__(self):
        super().__init__("openalex")
        self.parser_version = "v1"

        # Configure PyAlex
        from phdiscover.config import get_settings
        settings = get_settings()
        email = settings.openalex_email or "phdiscover@example.com"
        set_email = email
        Institutions.config.email = email
        Concepts.config.email = email
        Works.config.email = email

    @property
    def base_url(self) -> str:
        return "https://api.openalex.org"

    @property
    def search_paths(self) -> List[str]:
        # Not used for API-based crawler
        return []

    async def _rate_limit(self) -> None:
        await asyncio.sleep(0.6)  # ~100 req/min

    async def crawl_by_concepts(
        self,
        concept_ids: List[str],
        countries: List[str],
        max_per_concept: int = 50,
    ) -> List[RawPosition]:
        """Crawl positions via OpenAlex: Grants -> Projects -> Authors -> Positions"""
        all_positions = []

        for concept_id in concept_ids[:10]:
            for country in countries:
                await self._rate_limit()
                try:
                    # Find institutions in this country working on this concept
                    institutions = Institutions().filter(
                        concepts=concept_id,
                        country_code=country,
                        type="education",
                    ).get(per_page=20)

                    for inst in institutions:
                        # Find authors at this institution with this concept
                        authors = Authors().filter(
                            last_known_institution=inst.get("id"),
                            concepts=concept_id,
                        ).sort(cited_by_count="desc").get(per_page=30)

                        for author in authors:
                            # Get recent works (potential projects/grants)
                            works = Works().filter(author=author.get("id")).filter(
                                publication_year=datetime.utcnow().year
                            ).get(per_page=10)

                            for work in works:
                                # Check if work mentions PhD positions, grants, hiring
                                if self._indicates_hiring(work):
                                    pos = self._create_position_from_work(work, author, inst)
                                    if pos:
                                        all_positions.append(pos)

                except Exception as e:
                    logger.warning("openalex_crawl_failed", concept=concept_id, country=country, error=str(e))

        return all_positions

    def _indicates_hiring(self, work: Dict[str, Any]) -> bool:
        """Check if work indicates hiring/PhD positions"""
        text = f"{work.get('display_name', '')} {work.get('abstract', '')}".lower()
        hiring_keywords = [
            'phd position', 'phd student', 'doctoral position', 'doctoral student',
            'phd vacancy', 'phd opening', 'recruiting phd', 'hiring phd',
            'graduate position', 'graduate student', 'research assistant',
            'funded phd', 'fully funded', 'phd scholarship',
        ]
        return any(kw in text for kw in hiring_keywords)

    def _create_position_from_work(
        self,
        work: Dict[str, Any],
        author: Dict[str, Any],
        institution: Dict[str, Any],
    ) -> Optional[RawPosition]:
        """Create position from work indicating hiring"""
        try:
            # Extract institution info
            inst_name = institution.get("display_name", "")
            inst_country = institution.get("country_code", "").upper()

            # Extract author info
            author_name = author.get("display_name", "")
            author_id = author.get("id", "")

            # Create position
            title = f"PhD Position in {work.get('display_name', 'Research')}"
            if len(title) > 500:
                title = title[:500]

            # Try to find application URL from work DOI or related links
            apply_url = work.get("doi")
            if apply_url and not apply_url.startswith("http"):
                apply_url = f"https://doi.org/{apply_url}"

            return RawPosition(
                source_name=self.source_name,
                source_url=f"https://openalex.org/{work.get('id', '')}",
                title=title,
                university=inst_name,
                country=inst_country,
                city=institution.get("city"),
                description=work.get("abstract", "")[:2000] if work.get("abstract") else None,
                application_url=apply_url,
                pi_name=author_name,
                pi_profile_url=f"https://openalex.org/{author_id}" if author_id else None,
                raw_data={
                    "work_id": work.get("id"),
                    "author_id": author_id,
                    "institution_id": institution.get("id"),
                    "concepts": [c.get("display_name") for c in work.get("concepts", [])],
                    "grants": work.get("grants", []),
                    "publication_year": work.get("publication_year"),
                },
            )
        except Exception as e:
            logger.warning("create_position_from_work_failed", error=str(e))
            return None

    async def crawl_recent_grants(
        self,
        funder_ids: List[str],
        concept_ids: List[str],
    ) -> List[RawPosition]:
        """Crawl recent grants from funders"""
        all_positions = []

        for funder_id in funder_ids[:5]:
            await self._rate_limit()
            try:
                # Get works funded by this funder
                works = Works().filter(
                    grants_funder=funder_id,
                ).filter(
                    publication_year=datetime.utcnow().year
                ).get(per_page=50)

                for work in works:
                    # Check concepts match
                    work_concepts = [c.get("id") for c in work.get("concepts", [])]
                    if not any(c in concept_ids for c in work_concepts):
                        continue

                    if self._indicates_hiring(work):
                        # Get first author's institution
                        authorships = work.get("authorships", [])
                        if authorships:
                            first_author = authorships[0].get("author", {})
                            institutions = authorships[0].get("institutions", [])
                            if institutions:
                                pos = self._create_position_from_work(work, first_author, institutions[0])
                                if pos:
                                    all_positions.append(pos)

            except Exception as e:
                logger.warning("grant_crawl_failed", funder=funder_id, error=str(e))

        return all_positions

    # Abstract methods required by base class
    async def parse_listing_page(self, html: str, url: str) -> List[RawPosition]:
        return []

    async def parse_detail_page(self, html: str, url: str) -> Optional[RawPosition]:
        return None