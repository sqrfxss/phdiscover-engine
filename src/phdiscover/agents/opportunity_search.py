"""
PhDiscover Engine - Opportunity Search Agent
Searches for positions only within verified PI domains
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import structlog
import httpx
from ddgs import DDGS

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.models import PIProfile
from phdiscover.models.discovery import (
    DiscoveryQuery,
    RawPosition,
    ExtractedPosition,
)

logger = structlog.get_logger(__name__)


class OpportunitySearchAgent(BaseAgent):
    """Agent 4: Discovers PhD positions using scoped queries"""

    def __init__(self):
        super().__init__("opportunity_search")
        self.settings = get_settings()
        self.ddgs = DDGS()
        self.http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": self.settings.crawling.user_agent},
        )

        # Search templates for different sources
        self.search_templates = {
            "google": [
                '"{pi_name}" PhD position',
                '"{pi_name}" doctoral position',
                '"{pi_name}" PhD vacancy',
                '"{pi_name}" funded PhD',
                '"{lab_name}" PhD position',
                '"{pi_name}" biomechanics PhD',
                '"{pi_name}" motor control PhD',
                'site:{university_domain} "{pi_name}" PhD',
            ],
            "euraxess": [
                '"{pi_name}"',
                '"{lab_name}"',
                '{university} PhD {field}',
            ],
        }

    async def _rate_limit(self) -> None:
        await asyncio.sleep(0.5)  # Basic rate limiting

    def _build_queries(
        self,
        pi: PIProfile,
        fingerprint_fields: List[str],
    ) -> List[str]:
        """Build search queries for a PI"""
        queries = []

        pi_name = pi.name
        lab_name = pi.lab_name or ""
        university = pi.university
        uni_domain = self._extract_domain(university)

        # Google/Bing scoped queries
        for template in self.search_templates["google"]:
            query = template.format(
                pi_name=pi_name,
                lab_name=lab_name,
                university=university,
                university_domain=uni_domain,
                field=" ".join(fingerprint_fields[:3]),
            )
            queries.append(query)

        # Add field-specific queries
        for field in fingerprint_fields[:5]:
            queries.append(f'"{pi_name}" {field} PhD')

        return queries[:15]  # Limit queries

    def _extract_domain(self, university: str) -> str:
        """Extract likely domain from university name"""
        # Simple heuristic - would be better with a mapping
        name = university.lower()
        if "toronto" in name:
            return "utoronto.ca"
        elif "ubc" in name or "british columbia" in name:
            return "ubc.ca"
        elif "mcgill" in name:
            return "mcgill.ca"
        elif "waterloo" in name:
            return "uwaterloo.ca"
        elif "alberta" in name:
            return "ualberta.ca"
        elif "amsterdam" in name:
            return "uva.nl"
        elif "delft" in name:
            return "tudelft.nl"
        elif "leiden" in name:
            return "leidenuniv.nl"
        elif "utrecht" in name:
            return "uu.nl"
        elif "munich" in name or "tum" in name:
            return "tum.de"
        elif "heidelberg" in name:
            return "uni-heidelberg.de"
        elif "berlin" in name:
            return "hu-berlin.de"
        elif "hamburg" in name:
            return "uni-hamburg.de"
        elif "helsinki" in name:
            return "helsinki.fi"
        elif "aalto" in name:
            return "aalto.fi"
        elif "auckland" in name:
            return "auckland.ac.nz"
        elif "otago" in name:
            return "otago.ac.nz"
        else:
            # Generic fallback
            words = [w for w in university.lower().split() if len(w) > 3]
            return f"{''.join(words[:2])}.edu"

    async def _search_ddgs(self, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """Search using DuckDuckGo"""
        await self._rate_limit()
        results = []
        try:
            for r in self.ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
        except Exception as e:
            self.logger.warning("ddgs_search_failed", query=query, error=str(e))
        return results

    async def _search_euraxess(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """Search EURAXESS"""
        await self._rate_limit()
        results = []
        try:
            # EURAXESS search would need proper API or scraping
            # For now, use DDGS with site restriction
            euraxess_query = f"site:euraxess.ec.europa.eu {query}"
            for r in self.ddgs.text(euraxess_query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                    "source": "euraxess",
                })
        except Exception as e:
            self.logger.warning("euraxess_search_failed", query=query, error=str(e))
        return results

    def _extract_position_from_result(
        self,
        result: Dict[str, Any],
        pi: PIProfile,
        query: str,
    ) -> Optional[RawPosition]:
        """Extract position data from search result"""
        url = result.get("url", "")
        if not url:
            return None

        # Skip obvious non-position pages
        skip_patterns = [
            "linkedin.com", "facebook.com", "twitter.com", "x.com",
            "youtube.com", "scholar.google", "orcid.org",
            "wikipedia.org", "github.com", "gitlab.com",
        ]
        if any(p in url for p in skip_patterns):
            return None

        # Extract potential deadline
        deadline = self._extract_deadline(result.get("snippet", "") + " " + result.get("title", ""))

        # Extract funding info
        funding = self._extract_funding(result.get("snippet", "") + " " + result.get("title", ""))

        return RawPosition(
            source_name="ddgs",
            source_url=url,
            title=result.get("title", "")[:500],
            university=pi.university,
            department=pi.department,
            country=self._infer_country(pi.university),
            description=result.get("snippet", "")[:1000],
            application_url=url,
            application_deadline=deadline,
            funding_info=funding,
            pi_name=pi.name,
            pi_email=pi.email,
            pi_profile_url=pi.profile_url,
            raw_data={
                "query": query,
                "snippet": result.get("snippet", ""),
                "pi_id": str(pi.id),
            },
        )

    def _extract_deadline(self, text: str) -> Optional[date]:
        """Extract deadline from text"""
        patterns = [
            r'deadline[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'due[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'apply by[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'closing[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
            r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"]:
                    try:
                        return datetime.strptime(date_str, fmt).date()
                    except ValueError:
                        continue
        return None

    def _extract_funding(self, text: str) -> Optional[str]:
        """Extract funding information from text"""
        patterns = [
            r'(?:funded|stipend|salary|funding)[:\s]+([^.]+)',
            r'(?:€|\$|£|CAD|USD|EUR)\s*[\d,]+(?:\s*(?:per|/)\s*(?:month|year|annum))?',
            r'fully funded',
            r'funded position',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)[:200]
        return None

    def _infer_country(self, university: str) -> str:
        """Infer country from university name"""
        name = university.lower()
        if any(w in name for w in ["toronto", "ubc", "mcgill", "waterloo", "alberta", "calgary", "ottawa", "queen", "dalhousie", "simon fraser", "victoria", "guelph", "manitoba", "saskatchewan", "memorial", "new brunswick", "regina", "lakehead", "laurentian", "nipissing", "ontario tech", "toronto metropolitan", "trent", "windsor", "wilfrid", "york", "brock", "carleton", "concordia", "laval", "montreal", "sherbrooke", "quebec", "hec", "polytechnique", "ets"]):
            return "CA"
        elif any(w in name for w in ["delft", "amsterdam", "erasmus", "leiden", "maastricht", "radboud", "groningen", "twente", "utrecht", "vrije", "wageningen", "eindhoven", "tilburg", "open university"]):
            return "NL"
        elif any(w in name for w in ["munich", "heidelberg", "berlin", "bonn", "cologne", "freiburg", "gottingen", "hamburg", "mannheim", "rhab", "karlsruhe", "stuttgart", "tubingen", "wurzburg", "munster", "erlangen", "sport university cologne", "mainz", "regensburg", "ulm", "konstanz", "bielefeld", "bremen", "darmstadt", "duisburg", "essen", "giessen", "halle", "hanover", "jena", "kiel", "leipzig", "magdeburg", "marburg", "paderborn", "passau", "potsdam", "rostock", "saarland", "siegen", "trier", "witten"]):
            return "DE"
        elif any(w in name for w in ["helsinki", "aalto", "turku", "tampere", "oulu", "jyvaskyla", "eastern finland", "lut", "hanken", "vaasa", "lapland", "abo", "defence", "police"]):
            return "FI"
        elif any(w in name for w in ["auckland", "otago", "victoria wellington", "canterbury", "massey", "waikato", "aut", "lincoln"]):
            return "NZ"
        return "UNKNOWN"

    async def search_positions_for_pi(
        self,
        pi: PIProfile,
        fingerprint_fields: List[str],
    ) -> List[RawPosition]:
        """Search positions for a single PI"""
        queries = self._build_queries(pi, fingerprint_fields)
        all_positions = []

        for query in queries:
            # Search general web
            results = await self._search_ddgs(query, max_results=10)
            for result in results:
                pos = self._extract_position_from_result(result, pi, query)
                if pos:
                    all_positions.append(pos)

            # Search EURAXESS
            euraxess_results = await self._search_euraxess(query, max_results=10)
            for result in euraxess_results:
                pos = self._extract_position_from_result(result, pi, query)
                if pos:
                    pos.source_name = "euraxess"
                    all_positions.append(pos)

        # Deduplicate by URL
        seen = set()
        unique_positions = []
        for pos in all_positions:
            if pos.source_url not in seen:
                seen.add(pos.source_url)
                unique_positions.append(pos)

        return unique_positions

    async def search_all_pis(
        self,
        pis: List[PIProfile],
        fingerprint: "ResearchFingerprint",
    ) -> AgentResult:
        """Search positions for all PIs"""
        async with self.track_execution("search_all_pis") as cid:
            try:
                fingerprint_fields = (
                    fingerprint.core_topics +
                    fingerprint.adjacent_topics +
                    fingerprint.methods
                )

                self.logger.info("starting_position_search", pi_count=len(pis))

                all_positions = []
                pi_position_counts = {}

                for pi in pis:
                    positions = await self.search_positions_for_pi(pi, fingerprint_fields)
                    pi_position_counts[pi.id] = len(positions)
                    all_positions.extend(positions)

                self.logger.info(
                    "position_search_complete",
                    total_positions=len(all_positions),
                    pis_with_positions=sum(1 for c in pi_position_counts.values() if c > 0),
                )

                return AgentResult(
                    success=True,
                    data=all_positions,
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={
                        "total_positions": len(all_positions),
                        "pi_position_counts": {str(k): v for k, v in pi_position_counts.items()},
                    },
                )

            except Exception as e:
                self.logger.exception("position_search_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def run(
        self,
        pis: List[PIProfile],
        fingerprint: "ResearchFingerprint",
    ) -> AgentResult:
        """Execute opportunity search"""
        return await self.search_all_pis(pis, fingerprint)