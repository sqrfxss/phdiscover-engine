"""
PhDiscover Engine - Research Mapping Agent
Maps research ecosystem using OpenAlex API
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from uuid import UUID, uuid4

import structlog
from pyalex import Authors, Concepts, Institutions, Works

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.models import ResearchFingerprint
from phdiscover.models.discovery import (
    ResearchMappingResult,
    UniversityMapping,
    DepartmentMapping,
    ResearchGroupMapping,
)

logger = structlog.get_logger(__name__)


class ResearchMappingAgent(BaseAgent):
    """Agent 1: Maps research ecosystem around target domains using OpenAlex"""

    def __init__(self):
        super().__init__("research_mapping")
        self.settings = get_settings()
        self.openalex_email = self.settings.openalex_email or "phdiscover@example.com"

        # Configure PyAlex
        try:
            from pyalex import set_email
            set_email(self.openalex_email)
        except ImportError:
            pass

        # Rate limiting
        self._request_semaphore = asyncio.Semaphore(5)  # Max 5 concurrent
        self._request_times: List[float] = []
        self._min_interval = 0.6  # ~100 req/min

    async def _rate_limit(self) -> None:
        """Enforce rate limiting"""
        async with self._request_semaphore:
            now = datetime.utcnow().timestamp()
            # Remove old timestamps
            self._request_times = [t for t in self._request_times if now - t < 60]
            if len(self._request_times) >= 100:
                # Wait until we can make another request
                wait_time = 60 - (now - self._request_times[0])
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
            self._request_times.append(now)

    def _get_target_concepts(self, fingerprint: ResearchFingerprint) -> List[str]:
        """Get all target concepts from fingerprint"""
        concepts = []
        concepts.extend(fingerprint.core_topics)
        concepts.extend(fingerprint.adjacent_topics)
        concepts.extend(fingerprint.methods)
        concepts.extend(fingerprint.technical_methods)
        concepts.extend(fingerprint.data_modalities)
        return list(set(c.lower().strip() for c in concepts if c))

    def _get_target_countries(self) -> List[str]:
        """Get target countries from config"""
        return [c["code"] for c in self.settings.countries if c.get("priority", 99) <= 5]

    async def _search_concepts(self, keywords: List[str]) -> List[Dict[str, Any]]:
        """Search OpenAlex for relevant concepts"""
        all_concepts = []

        for keyword in keywords[:20]:  # Limit to top 20 keywords
            await self._rate_limit()
            try:
                results = Concepts().search(keyword).filter(cited_by_count=">100").get(per_page=10)
                for c in results:
                    all_concepts.append({
                        "id": c.get("id"),
                        "display_name": c.get("display_name"),
                        "level": c.get("level"),
                        "description": c.get("description"),
                        "works_count": c.get("works_count", 0),
                        "cited_by_count": c.get("cited_by_count", 0),
                        "ancestors": c.get("ancestors", []),
                        "related_concepts": c.get("related_concepts", []),
                        "matched_keyword": keyword,
                    })
            except Exception as e:
                self.logger.warning("concept_search_failed", keyword=keyword, error=str(e))

        # Deduplicate by concept ID
        seen = set()
        unique_concepts = []
        for c in all_concepts:
            if c["id"] not in seen:
                seen.add(c["id"])
                unique_concepts.append(c)

        return unique_concepts

    async def _search_institutions_by_concept(
        self,
        concept_ids: List[str],
        countries: List[str],
    ) -> List[Dict[str, Any]]:
        """Search institutions by concept and country"""
        all_institutions = []

        for concept_id in concept_ids[:10]:  # Top 10 concepts
            for country in countries:
                await self._rate_limit()
                try:
                    results = Institutions().filter(
                        concepts=concept_id,
                        country_code=country,
                        type="education",
                    ).get(per_page=50)

                    for inst in results:
                        inst["matched_concept"] = concept_id
                        inst["matched_country"] = country
                        all_institutions.append(inst)
                except Exception as e:
                    self.logger.warning(
                        "institution_search_failed",
                        concept=concept_id,
                        country=country,
                        error=str(e),
                    )

        # Deduplicate by institution ID
        seen = set()
        unique_institutions = []
        for inst in all_institutions:
            inst_id = inst.get("id")
            if inst_id and inst_id not in seen:
                seen.add(inst_id)
                unique_institutions.append(inst)

        return unique_institutions

    async def _get_institution_details(self, openalex_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed institution information"""
        await self._rate_limit()
        try:
            return Institutions()[openalex_id]
        except Exception as e:
            self.logger.warning("institution_detail_failed", id=openalex_id, error=str(e))
            return None

    async def _search_departments(self, institution_id: str) -> List[Dict[str, Any]]:
        """Search for departments/sub-institutions"""
        await self._rate_limit()
        try:
            # OpenAlex doesn't have explicit departments, but we can use sub-institutions
            # or filter authors by institution and group by their sub-institution
            results = Authors().filter(
                last_known_institution=institution_id,
            ).group_by("institutions.id").get(per_page=100)

            departments = []
            for group in results:
                inst_info = group.get("key", {})
                if inst_info.get("id") and inst_info["id"] != institution_id:
                    departments.append({
                        "id": inst_info["id"],
                        "display_name": inst_info.get("display_name", "Unknown"),
                        "parent_institution": institution_id,
                        "author_count": group.get("count", 0),
                    })
            return departments
        except Exception as e:
            self.logger.warning("department_search_failed", institution=institution_id, error=str(e))
            return []

    async def _search_pis_in_institution(
        self,
        institution_id: str,
        concept_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Search for relevant PIs in an institution"""
        all_pis = []

        for concept_id in concept_ids[:5]:  # Top 5 concepts
            await self._rate_limit()
            try:
                results = Authors().filter(
                    last_known_institution=institution_id,
                    concepts=concept_id,
                ).sort(cited_by_count="desc").get(per_page=50)

                for author in results:
                    author["matched_concept"] = concept_id
                    author["matched_institution"] = institution_id
                    all_pis.append(author)
            except Exception as e:
                self.logger.warning(
                    "pi_search_failed",
                    institution=institution_id,
                    concept=concept_id,
                    error=str(e),
                )

        # Deduplicate by author ID
        seen = set()
        unique_pis = []
        for pi in all_pis:
            pi_id = pi.get("id")
            if pi_id and pi_id not in seen:
                seen.add(pi_id)
                unique_pis.append(pi)

        return unique_pis

    async def _build_research_groups(
        self,
        institution_id: str,
        pis: List[Dict[str, Any]],
        department_mappings: List[DepartmentMapping],
    ) -> List[ResearchGroupMapping]:
        """Build research group mappings from PI clusters"""
        # Group PIs by shared concepts/keywords
        groups = {}

        for pi in pis:
            # Use concepts as group keys
            concepts = pi.get("concepts", [])
            for concept in concepts[:3]:  # Top 3 concepts
                concept_name = concept.get("display_name", "Unknown")
                key = f"{institution_id}_{concept_name}"

                if key not in groups:
                    groups[key] = {
                        "name": f"{concept_name} Research Group",
                        "institution_id": institution_id,
                        "concept": concept_name,
                        "pis": [],
                        "keywords": [],
                    }
                groups[key]["pis"].append(pi.get("id"))
                groups[key]["keywords"].append(concept_name)

        # Convert to ResearchGroupMapping
        research_groups = []
        dept_map = {d.openalex_id: d.id for d in department_mappings if d.openalex_id}

        for key, group_data in groups.items():
            if len(group_data["pis"]) >= 2:  # At least 2 PIs to form a group
                # Find matching department
                dept_id = None
                for pi in pis:
                    if pi.get("id") in group_data["pis"]:
                        # Try to match department from PI's affiliations
                        pass

                rg = ResearchGroupMapping(
                    name=group_data["name"],
                    department_id=dept_id or (department_mappings[0].id if department_mappings else uuid4()),
                    department_name="",
                    university_id=uuid4(),  # Will be resolved
                    university_name="",
                    country_code="",
                    pi_ids=[uuid4() for _ in group_data["pis"]],  # Placeholder
                    keywords=list(set(group_data["keywords"])),
                )
                research_groups.append(rg)

        return research_groups

    async def map_research_ecosystem(
        self,
        fingerprint: ResearchFingerprint,
    ) -> AgentResult:
        """Main entry point: map research ecosystem for fingerprint"""
        async with self.track_execution("map_research_ecosystem") as cid:
            try:
                self.logger.info("starting_research_mapping", fingerprint_id=str(fingerprint.id))

                # 1. Extract target concepts and countries
                target_keywords = self._get_target_concepts(fingerprint)
                target_countries = self._get_target_countries()

                self.logger.info(
                    "targets_identified",
                    keywords_count=len(target_keywords),
                    countries=target_countries,
                )

                # 2. Search for relevant concepts in OpenAlex
                concepts = await self._search_concepts(target_keywords)
                concept_ids = [c["id"] for c in concepts if c.get("id")]

                self.logger.info("concepts_found", count=len(concepts))

                # 3. Search institutions by concept and country
                institutions = await self._search_institutions_by_concept(
                    concept_ids[:15],  # Top 15 concepts
                    target_countries,
                )

                self.logger.info("institutions_found", count=len(institutions))

                # 4. Build university mappings
                universities = []
                for inst in institutions[:100]:  # Top 100 institutions
                    detail = await self._get_institution_details(inst["id"])
                    if not detail:
                        continue

                    univ = UniversityMapping(
                        openalex_id=detail.get("id", ""),
                        name=detail.get("display_name", ""),
                        country_code=detail.get("country_code", "").upper(),
                        city=detail.get("city"),
                        website=detail.get("homepage_url"),
                        concepts=detail.get("concepts", []),
                        works_count=detail.get("works_count", 0),
                        cited_by_count=detail.get("cited_by_count", 0),
                    )
                    universities.append(univ)

                self.logger.info("universities_mapped", count=len(universities))

                # 5. For each university, find departments and PIs
                all_departments = []
                all_research_groups = []
                total_pis = 0

                for univ in universities[:30]:  # Process top 30 universities
                    # Search departments
                    departments = await self._search_departments(univ.openalex_id)

                    for dept in departments[:10]:
                        dept_mapping = DepartmentMapping(
                            openalex_id=dept.get("id", ""),
                            name=dept.get("display_name", "Unknown"),
                            university_id=univ.id,
                            university_name=univ.name,
                            country_code=univ.country_code,
                        )
                        all_departments.append(dept_mapping)

                    # Search PIs
                    pis = await self._search_pis_in_institution(
                        univ.openalex_id,
                        concept_ids[:10],
                    )
                    total_pis += len(pis)

                    # Build research groups
                    univ_depts = [d for d in all_departments if d.university_id == univ.id]
                    groups = await self._build_research_groups(
                        univ.openalex_id,
                        pis,
                        univ_depts,
                    )
                    all_research_groups.extend(groups)

                self.logger.info(
                    "mapping_complete",
                    universities=len(universities),
                    departments=len(all_departments),
                    research_groups=len(all_research_groups),
                    total_pis=total_pis,
                )

                # 6. Build result
                result = ResearchMappingResult(
                    universities=universities,
                    departments=all_departments,
                    research_groups=all_research_groups,
                    concepts=concepts,
                    total_pis_found=total_pis,
                    total_positions_estimated=total_pis * 2,  # Rough estimate
                    fingerprint_version=fingerprint.version,
                    countries_covered=target_countries,
                )

                return AgentResult(
                    success=True,
                    data=result,
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={
                        "universities_count": len(universities),
                        "departments_count": len(all_departments),
                        "groups_count": len(all_research_groups),
                        "pis_found": total_pis,
                    },
                )

            except Exception as e:
                self.logger.exception("mapping_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def run(self, fingerprint: ResearchFingerprint) -> AgentResult:
        """Execute research mapping"""
        return await self.map_research_ecosystem(fingerprint)