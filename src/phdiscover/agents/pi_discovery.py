"""
PhDiscover Engine — PI Discovery Agent
Finds PIs with fallback chain: University → Lab → Department → ORCID → Scholar

The OpenAlex path here is the *concept-scoped* one: it needs an institution id
and a concept id, so it only finds PIs inside universities the mapping agent
already resolved. The verified recovery strategies in
``phdiscover.crawlers.recovery_strategies`` cover the other half — topic-first
discovery for hosts and queries where the scoped path returns nothing.
``discover_pis_topic_first`` is the entry point that uses them.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from uuid import UUID, uuid4

import structlog
from pyalex import Authors, Institutions, Works

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.crawlers.recovery_strategies import (
    DirectHttpLister,
    OpenAlexAuthors,
    SearchIndexMiner,
)
from phdiscover.crawlers.registry import CRAWLABLE, REGISTRY
from phdiscover.models import PIProfile, Paper, Grant
from phdiscover.models.discovery import ResearchMappingResult, UniversityMapping

logger = structlog.get_logger(__name__)


class PIDiscoveryAgent(BaseAgent):
    """Agent 2: Discovers PIs with multi-source fallback chain"""

    def __init__(self):
        super().__init__("pi_discovery")
        self.settings = get_settings()
        self.openalex_email = self.settings.openalex_email or "phdiscover@example.com"
        # Verified strategies. OpenAlexAuthors uses topic->works->authorships
        # because display_name.search matches PI names, not research topics.
        self._openalex = OpenAlexAuthors(mailto=self.openalex_email)
        self._direct = {n: DirectHttpLister(s) for n, s in REGISTRY.items()}
        self._miner = {n: SearchIndexMiner(s) for n, s in REGISTRY.items()}

        try:
            from pyalex import set_email
            set_email(self.openalex_email)
        except ImportError:
            pass

        self._request_semaphore = asyncio.Semaphore(5)
        self._request_times: List[float] = []

    async def _rate_limit(self) -> None:
        async with self._request_semaphore:
            now = datetime.utcnow().timestamp()
            self._request_times = [t for t in self._request_times if now - t < 60]
            if len(self._request_times) >= 100:
                wait_time = 60 - (now - self._request_times[0])
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
            self._request_times.append(now)

    async def discover_pis_topic_first(
        self,
        topics: List[str],
        per_topic: int = 20,
        min_fit_score: float = 0.0,
        fingerprint_concepts: Optional[List[str]] = None,
        fingerprint_methods: Optional[List[str]] = None,
    ) -> AgentResult:
        """Topic-first PI discovery using the verified OpenAlex strategy.

        Unlike ``discover_pis``, this needs no institution id and no concept id,
        so it works even when the mapping agent resolved nothing. Each PI keeps
        the ORCID that makes the verification agent's email chain possible.
        """
        async with self.track_execution("discover_pis_topic_first") as cid:
            try:
                fp_concepts = [c.lower() for c in (fingerprint_concepts or topics)]
                fp_methods = [m.lower() for m in (fingerprint_methods or [])]
                all_pis: Dict[str, PIProfile] = {}

                for topic in topics:
                    self.logger.info("topic_discovery", topic=topic)
                    raw = await self._openalex.find_pis_by_topic(
                        topic, per_page=per_topic, top_n=per_topic)
                    for r in raw:
                        aid = r["openalex_id"]
                        if aid in all_pis:
                            continue
                        fit = self._score_topic_fit(topic, r, fp_concepts,
                                                    fp_methods)
                        if fit < min_fit_score:
                            continue
                        pi = PIProfile(
                            name=r.get("name") or "",
                            university=r.get("institution") or "UNKNOWN",
                            openalex_id=aid,
                            orcid_id=(r.get("orcid") or "").replace(
                                "https://orcid.org/", "") or None,
                            research_keywords=[e.get("title") or ""
                                               for e in r.get("example_papers", [])
                                               if e.get("title")],
                            recent_papers=[
                                Paper(
                                    title=e.get("title") or "",
                                    year=e.get("year") or 0,
                                    citations=e.get("cited_by") or 0,
                                )
                                for e in r.get("example_papers", [])
                                if e.get("title")
                            ],
                            research_fit_score=fit,
                        )
                        # ORCID present => the verification chain has a start.
                        pi.verification_level = (
                            "PROBABLE" if pi.orcid_id else "UNVERIFIED")
                        all_pis[aid] = pi
                    await asyncio.sleep(1.5)  # /works is intermittently 429

                verified = sum(1 for p in all_pis.values() if p.orcid_id)
                self.logger.info(
                    "topic_discovery_complete",
                    total=len(all_pis), with_orcid=verified,
                )
                return AgentResult(
                    success=True,
                    data=list(all_pis.values()),
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={
                        "total_pis": len(all_pis),
                        "with_orcid": verified,
                        "topics_searched": len(topics),
                        "strategy": "openalex_topic_works",
                    },
                )
            except Exception as e:
                self.logger.exception("topic_discovery_failed", error=str(e))
                return AgentResult(success=False, error=str(e),
                                   agent_name=self.name, correlation_id=cid)

    def _score_topic_fit(
        self,
        topic: str,
        pi: Dict[str, Any],
        fp_concepts: List[str],
        fp_methods: List[str],
    ) -> float:
        """0-100 fit. Topic match is the base; fingerprint overlap refines it."""
        score = 40.0  # discovered under a fingerprint topic at all
        blob = " ".join([
            topic,
            " ".join(p.get("title") or "" for p in pi.get("example_papers", [])),
        ]).lower()
        for c in fp_concepts:
            if c.lower() in blob:
                score += 20
                break
        for m in fp_methods:
            if m.lower() in blob:
                score += 15
                break
        if pi.get("orcid"):
            score += 15  # verifiable identity
        if (pi.get("cited_in_set") or 0) > 1000:
            score += 10
        return float(min(score, 100.0))

    async def discover_positions(
        self,
        topics: List[str],
        per_query: int = 15,
    ) -> AgentResult:
        """Position discovery across every verified source, by strategy.

        Routes each spec through the strategy the registry recorded for it:
        direct_http for the non-Cloudflare portals, search_index for the hosts
        that refuse to serve listings. Returns raw candidate links with real
        URLs — extraction and verification stay downstream, as CLAUDE.md requires.
        """
        async with self.track_execution("discover_positions") as cid:
            try:
                found: List[Dict[str, Any]] = []
                health: List[Dict[str, Any]] = []

                for spec in CRAWLABLE:
                    if spec.strategy.value == "direct_http":
                        lister = self._direct[spec.name]
                        links = await lister.list_links()
                        found += [vars(l) for l in links]
                        health.append({"source": spec.name,
                                       "verdict": spec.verdict.value,
                                       "items": len(links), "via": "direct_http"})
                        await asyncio.sleep(1.0)

                for name, spec in REGISTRY.items():
                    if spec.strategy.value != "search_index":
                        continue
                    for t in topics[:3]:
                        links = await self._miner[name].mine(t, limit=per_query)
                        found += [vars(l) for l in links]
                        await asyncio.sleep(3.0)  # ddgs goes silent when hammered

                seen, uniq = set(), []
                for f in found:
                    k = (f.get("title"), f.get("url"))
                    if k in seen:
                        continue
                    seen.add(k)
                    uniq.append(f)

                self.logger.info("positions_complete", raw=len(found),
                                 unique=len(uniq))
                return AgentResult(
                    success=True,
                    data=uniq,
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={"total_links": len(uniq),
                              "sources_tried": len(health),
                              "source_health": health},
                )
            except Exception as e:
                self.logger.exception("positions_failed", error=str(e))
                return AgentResult(success=False, error=str(e),
                                   agent_name=self.name, correlation_id=cid)

    def _extract_email_from_text(self, text: str) -> Optional[str]:
        """Extract email from text using regex"""
        if not text:
            return None
        email_pattern = r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'
        matches = re.findall(email_pattern, text)
        # Filter for university domains
        for email in matches:
            domain = email.split('@')[1].lower()
            if any(tld in domain for tld in ['.edu', '.ac.', '.university', '.college']):
                return email
        return matches[0] if matches else None

    async def _search_pis_by_concepts(
        self,
        institution_id: str,
        concept_ids: List[str],
        max_per_concept: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search PIs by concepts within institution"""
        all_pis = []

        for concept_id in concept_ids[:8]:
            await self._rate_limit()
            try:
                results = Authors().filter(
                    last_known_institution=institution_id,
                    concepts=concept_id,
                ).sort(cited_by_count="desc").get(per_page=max_per_concept)

                for author in results:
                    author["_matched_concept"] = concept_id
                    all_pis.append(author)
            except Exception as e:
                self.logger.warning("pi_search_failed", concept=concept_id, error=str(e))

        # Deduplicate
        seen = set()
        unique_pis = []
        for pi in all_pis:
            pi_id = pi.get("id")
            if pi_id and pi_id not in seen:
                seen.add(pi_id)
                unique_pis.append(pi)

        return unique_pis

    async def _get_author_details(self, author_id: str) -> Optional[Dict[str, Any]]:
        """Get full author details from OpenAlex"""
        await self._rate_limit()
        try:
            return Authors()[author_id]
        except Exception as e:
            self.logger.warning("author_detail_failed", id=author_id, error=str(e))
            return None

    async def _fetch_recent_papers(self, author_id: str, limit: int = 10) -> List[Paper]:
        """Fetch recent papers for an author"""
        await self._rate_limit()
        papers = []
        try:
            results = Works().filter(author=author_id).sort(publication_date="desc").get(per_page=limit)
            for work in results:
                papers.append(Paper(
                    title=work.get("display_name", ""),
                    year=work.get("publication_year", 0),
                    venue=work.get("host_venue", {}).get("display_name"),
                    doi=work.get("doi"),
                    openalex_id=work.get("id"),
                    citations=work.get("cited_by_count", 0),
                    authors=[a.get("author", {}).get("display_name", "") for a in work.get("authorships", [])],
                    topics=[c.get("display_name", "") for c in work.get("concepts", [])],
                ))
        except Exception as e:
            self.logger.warning("papers_fetch_failed", author=author_id, error=str(e))
        return papers

    async def _fetch_grants(self, author_id: str) -> List[Grant]:
        """Fetch grants for an author (from OpenAlex funding info)"""
        await self._rate_limit()
        grants = []
        try:
            results = Works().filter(author=author_id).filter(has_funding=True).get(per_page=50)
            seen_funders = set()
            for work in results:
                for funding in work.get("grants", []):
                    funder = funding.get("funder_display_name", "")
                    award_id = funding.get("award_id", "")
                    key = f"{funder}_{award_id}"
                    if key not in seen_funders:
                        seen_funders.add(key)
                        grants.append(Grant(
                            title=work.get("display_name", ""),
                            funder=funder,
                            amount=None,  # OpenAlex doesn't provide amounts
                            currency=None,
                            openalex_id=work.get("id"),
                        ))
        except Exception as e:
            self.logger.warning("grants_fetch_failed", author=author_id, error=str(e))
        return grants[:10]

    async def _fallback_email_search(self, pi: PIProfile) -> PIProfile:
        """Fallback chain for email discovery"""
        # Priority 1: University faculty page
        if pi.profile_url:
            email = await self._scrape_email_from_url(str(pi.profile_url))
            if email and self._is_university_email(email, pi.university):
                pi.email = email
                pi.email_source = "official_university"
                pi.email_confidence = 1.0
                pi.email_verified = True
                return pi

        # Priority 2: Lab page
        if pi.lab_url:
            email = await self._scrape_email_from_url(str(pi.lab_url))
            if email:
                pi.email = email
                pi.email_source = "lab_page"
                pi.email_confidence = 0.95
                return pi

        # Priority 3: Department page
        dept_url = await self._find_department_page(pi.university, pi.department)
        if dept_url:
            email = await self._scrape_email_from_url(dept_url)
            if email:
                pi.email = email
                pi.email_source = "department_page"
                pi.email_confidence = 0.95
                return pi

        # Priority 4: University directory
        dir_url = await self._find_university_directory(pi.university)
        if dir_url:
            email = await self._scrape_email_from_url(dir_url)
            if email:
                pi.email = email
                pi.email_source = "university_directory"
                pi.email_confidence = 0.95
                return pi

        # Priority 5: ORCID
        if pi.orcid_id:
            email = await self._get_email_from_orcid(pi.orcid_id)
            if email:
                pi.email = email
                pi.email_source = "orcid"
                pi.email_confidence = 0.80
                return pi

        # Priority 6: Google Scholar
        if pi.google_scholar_id:
            email = await self._get_email_from_scholar(pi.google_scholar_id)
            if email:
                pi.email = email
                pi.email_source = "google_scholar"
                pi.email_confidence = 0.75
                return pi

        return pi

    async def _scrape_email_from_url(self, url: str) -> Optional[str]:
        """Scrape email from a URL (placeholder - would use httpx + BeautifulSoup)"""
        # This would be implemented with actual HTTP scraping
        # For now, return None
        return None

    def _is_university_email(self, email: str, university: str) -> bool:
        """Check if email belongs to university domain"""
        if not email or '@' not in email:
            return False
        domain = email.split('@')[1].lower()
        # Extract university keywords
        uni_keywords = university.lower().split()
        return any(kw in domain for kw in uni_keywords if len(kw) > 3)

    async def _find_department_page(self, university: str, department: Optional[str]) -> Optional[str]:
        """Find department page URL"""
        # Placeholder - would search for department page
        return None

    async def _find_university_directory(self, university: str) -> Optional[str]:
        """Find university directory URL"""
        return None

    async def _get_email_from_orcid(self, orcid_id: str) -> Optional[str]:
        """Get email from ORCID profile"""
        return None

    async def _get_email_from_scholar(self, scholar_id: str) -> Optional[str]:
        """Get email from Google Scholar profile"""
        return None

    def _calculate_research_fit(
        self,
        author: Dict[str, Any],
        fingerprint_concepts: List[str],
        fingerprint_methods: List[str],
    ) -> Dict[str, Any]:
        """Calculate research fit score for a PI"""
        score = 0.0
        breakdown = {}
        matched_concepts = []
        matched_methods = []

        # Check concepts
        author_concepts = [c.get("display_name", "").lower() for c in author.get("concepts", [])]
        for fp_concept in fingerprint_concepts:
            fp_lower = fp_concept.lower()
            for auth_concept in author_concepts:
                if fp_lower in auth_concept or auth_concept in fp_lower:
                    score += 10
                    breakdown[fp_concept] = breakdown.get(fp_concept, 0) + 10
                    matched_concepts.append(auth_concept)

        # Check methods
        author_keywords = " ".join([
            w.get("display_name", "") for w in author.get("works", [])[:20]
        ]).lower()

        for method in fingerprint_methods:
            if method.lower() in author_keywords:
                score += 5
                breakdown[f"method_{method}"] = breakdown.get(f"method_{method}", 0) + 5
                matched_methods.append(method)

        # Normalize to 0-100
        score = min(score, 100.0)

        return {
            "score": score,
            "breakdown": breakdown,
            "matched_concepts": list(set(matched_concepts)),
            "matched_methods": list(set(matched_methods)),
        }

    async def discover_pis(
        self,
        mapping: ResearchMappingResult,
        fingerprint: ResearchFingerprint,
        min_fit_score: float = 70.0,
    ) -> AgentResult:
        """Discover PIs from research mapping"""
        async with self.track_execution("discover_pis") as cid:
            try:
                self.logger.info("starting_pi_discovery", universities=len(mapping.universities))

                fingerprint_concepts = [c.lower() for c in fingerprint.core_topics + fingerprint.adjacent_topics]
                fingerprint_methods = [m.lower() for m in fingerprint.methods + fingerprint.technical_methods]

                # Get concept IDs from mapping
                concept_ids = [c["id"] for c in mapping.concepts if c.get("id")]

                all_pi_profiles = []

                for univ in mapping.universities[:25]:  # Top 25 universities
                    # Search PIs in this university
                    pis = await self._search_pis_by_concepts(univ.openalex_id, concept_ids[:10])

                    for pi_data in pis[:30]:  # Top 30 PIs per university
                        # Get full details
                        details = await self._get_author_details(pi_data.get("id", ""))
                        if not details:
                            continue

                        # Calculate research fit
                        fit_result = self._calculate_research_fit(
                            details,
                            fingerprint_concepts,
                            fingerprint_methods,
                        )

                        if fit_result["score"] < min_fit_score:
                            continue

                        # Build PI profile
                        pi = PIProfile(
                            name=details.get("display_name", ""),
                            university=univ.name,
                            department=None,  # Would need to resolve
                            lab_name=None,
                            profile_url=details.get("homepage"),
                            orcid_id=details.get("orcid"),
                            google_scholar_id=None,  # Would need separate lookup
                            openalex_id=details.get("id"),
                            research_keywords=[c.get("display_name", "") for c in details.get("concepts", [])],
                            recent_papers=await self._fetch_recent_papers(details.get("id", "")),
                            active_grants=await self._fetch_grants(details.get("id", "")),
                            phd_students_count=0,  # Would need to count from co-authors
                            research_fit_score=fit_result["score"],
                        )

                        # Run fallback email search
                        pi = await self._fallback_email_search(pi)

                        # Set verification level based on email source
                        if pi.email_verified and pi.email_confidence >= 0.9:
                            pi.verification_level = "VERIFIED"
                        elif pi.email and pi.email_confidence >= 0.7:
                            pi.verification_level = "PROBABLE"
                        else:
                            pi.verification_level = "UNVERIFIED"

                        all_pi_profiles.append(pi)

                self.logger.info(
                    "pi_discovery_complete",
                    total_pis=len(all_pi_profiles),
                    verified=sum(1 for p in all_pi_profiles if p.verification_level == "VERIFIED"),
                    probable=sum(1 for p in all_pi_profiles if p.verification_level == "PROBABLE"),
                )

                return AgentResult(
                    success=True,
                    data=all_pi_profiles,
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={
                        "total_pis": len(all_pi_profiles),
                        "verified": sum(1 for p in all_pi_profiles if p.verification_level == "VERIFIED"),
                        "probable": sum(1 for p in all_pi_profiles if p.verification_level == "PROBABLE"),
                    },
                )

            except Exception as e:
                self.logger.exception("pi_discovery_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def run(
        self,
        mapping: ResearchMappingResult,
        fingerprint: ResearchFingerprint,
        min_fit_score: float = 70.0,
    ) -> AgentResult:
        """Execute PI discovery"""
        return await self.discover_pis(mapping, fingerprint, min_fit_score)