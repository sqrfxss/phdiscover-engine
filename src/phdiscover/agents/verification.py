"""
PhDiscover Engine - Verification Agent
Verifies positions, PIs, and emails with evidence
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import structlog
import httpx
from bs4 import BeautifulSoup

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.models import Position, PIProfile, Evidence, SourceType, VerificationLevel
from phdiscover.models.discovery import RawPosition

logger = structlog.get_logger(__name__)


class VerificationAgent(BaseAgent):
    """Agent 5: Verifies positions, PIs, and emails with evidence"""

    def __init__(self):
        super().__init__("verification")
        self.settings = get_settings()
        self.http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": self.settings.crawling.user_agent},
            follow_redirects=True,
        )

        # Source hierarchy for verification
        self.source_priorities = {
            SourceType.OFFICIAL_UNIVERSITY: 10,
            SourceType.OFFICIAL_DOCTORAL_SCHOOL: 9,
            SourceType.OFFICIAL_PI: 9,
            SourceType.OFFICIAL_PROJECT: 8,
            SourceType.GOVERNMENT: 8,
            SourceType.UNIVERSITY_DIRECTORY: 7,
            SourceType.ORCID: 6,
            SourceType.GOOGLE_SCHOLAR: 5,
            SourceType.EURAXESS: 4,
            SourceType.ACADEMIC_POSITIONS: 3,
            SourceType.FINDAPHD: 2,
            SourceType.AGGREGATOR: 1,
            SourceType.SEARCH_SNIPPET: 1,
        }

    async def _rate_limit(self) -> None:
        await asyncio.sleep(0.3)

    def _calculate_content_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch page content"""
        await self._rate_limit()
        try:
            response = await self.http_client.get(url)
            if response.status_code == 200:
                return response.text
        except Exception as e:
            self.logger.warning("fetch_failed", url=url, error=str(e))
        return None

    def _extract_emails(self, html: str) -> List[str]:
        """Extract emails from HTML"""
        email_pattern = r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'
        emails = re.findall(email_pattern, html)
        # Filter for likely academic emails
        filtered = []
        for email in emails:
            domain = email.split('@')[1].lower()
            if any(tld in domain for tld in ['.edu', '.ac.', '.university', '.college', '.gov']):
                filtered.append(email)
        return list(set(filtered))

    def _extract_emails_from_text(self, text: str) -> List[str]:
        """Extract emails from plain text"""
        email_pattern = r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b'
        return list(set(re.findall(email_pattern, text)))

    def _classify_source(self, url: str, html: str = "") -> SourceType:
        """Classify source type from URL"""
        url_lower = url.lower()

        if any(domain in url_lower for domain in [
            "euraxess.ec.europa.eu",
            "findaphd.com",
            "academicpositions.com",
            "scholarshipdb.net",
            "phdportal.com",
        ]):
            if "euraxess" in url_lower:
                return SourceType.EURAXESS
            elif "findaphd" in url_lower:
                return SourceType.FINDAPHD
            elif "academicpositions" in url_lower:
                return SourceType.ACADEMIC_POSITIONS
            return SourceType.AGGREGATOR

        # Check for official university domains
        if any(tld in url_lower for tld in ['.edu/', '.ac.', '.university.', '.college.']):
            if any(kw in url_lower for kw in ['faculty', 'staff', 'profile', 'people', 'directory']):
                return SourceType.OFFICIAL_UNIVERSITY
            elif any(kw in url_lower for kw in ['doctoral', 'phd', 'graduate', 'graduatestudies']):
                return SourceType.OFFICIAL_DOCTORAL_SCHOOL
            elif any(kw in url_lower for kw in ['lab', 'laboratory', 'research-group', 'group']):
                return SourceType.OFFICIAL_PI
            elif any(kw in url_lower for kw in ['project', 'grant', 'funding']):
                return SourceType.OFFICIAL_PROJECT
            return SourceType.UNIVERSITY_DIRECTORY

        if "orcid.org" in url_lower:
            return SourceType.ORCID
        if "scholar.google" in url_lower:
            return SourceType.GOOGLE_SCHOLAR

        return SourceType.SEARCH_SNIPPET

    async def verify_position(
        self,
        raw_position: RawPosition,
    ) -> AgentResult:
        """Verify a raw position"""
        async with self.track_execution("verify_position") as cid:
            try:
                # Fetch the position page
                html = await self._fetch_page(str(raw_position.source_url))
                if not html:
                    return AgentResult(
                        success=False,
                        error="Could not fetch position page",
                        error_type="EMPTY_CONTENT",
                        agent_name=self.name,
                        correlation_id=cid,
                    )

                soup = BeautifulSoup(html, 'html.parser')
                text = soup.get_text()

                # Extract evidence
                evidence = []

                # Check for position status keywords
                status_keywords = {
                    "active": ["open", "accepting applications", "apply now", "position available"],
                    "closed": ["closed", "no longer accepting", "filled", "deadline passed"],
                    "expired": ["expired", "deadline was", "past deadline"],
                }

                position_status = "UNKNOWN"
                for status, keywords in status_keywords.items():
                    if any(kw in text.lower() for kw in keywords):
                        position_status = status.upper()
                        evidence.append(Evidence(
                            candidate_id=raw_position.id if hasattr(raw_position, 'id') else uuid4(),
                            candidate_type="position",
                            claim=f"position_status_{status}",
                            value=True,
                            source_url=raw_position.source_url,
                            source_type=self._classify_source(str(raw_position.source_url), html),
                            source_priority=self.source_priorities.get(
                                self._classify_source(str(raw_position.source_url), html), 1
                            ),
                            evidence_text=f"Found keyword indicating {status}",
                            confidence=0.8,
                        ))
                        break

                # Verify deadline
                deadline_confirmed = raw_position.application_deadline
                deadline_patterns = [
                    r'deadline[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                    r'due[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                    r'apply by[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                ]
                for pattern in deadline_patterns:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        # Would parse date here
                        evidence.append(Evidence(
                            candidate_id=raw_position.id if hasattr(raw_position, 'id') else uuid4(),
                            candidate_type="position",
                            claim="application_deadline",
                            value=match.group(1),
                            source_url=raw_position.source_url,
                            source_type=self._classify_source(str(raw_position.source_url), html),
                            source_priority=self.source_priorities.get(
                                self._classify_source(str(raw_position.source_url), html), 1
                            ),
                            evidence_text=match.group(0),
                            confidence=0.9,
                        ))
                        break

                # Verify funding
                funding_confirmed = False
                funding_keywords = ["fully funded", "stipend", "salary", "funding provided", "scholarship"]
                for kw in funding_keywords:
                    if kw in text.lower():
                        funding_confirmed = True
                        evidence.append(Evidence(
                            candidate_id=raw_position.id if hasattr(raw_position, 'id') else uuid4(),
                            candidate_type="position",
                            claim="funding_confirmed",
                            value=True,
                            source_url=raw_position.source_url,
                            source_type=self._classify_source(str(raw_position.source_url), html),
                            source_priority=self.source_priorities.get(
                                self._classify_source(str(raw_position.source_url), html), 1
                            ),
                            evidence_text=f"Found funding keyword: {kw}",
                            confidence=0.85,
                        ))
                        break

                # Determine verification level
                source_type = self._classify_source(str(raw_position.source_url), html)
                source_priority = self.source_priorities.get(source_type, 1)

                if source_priority >= 8 and position_status == "ACTIVE":
                    verification_level = VerificationLevel.STRONG
                elif source_priority >= 5 and position_status == "ACTIVE":
                    verification_level = VerificationLevel.MEDIUM
                else:
                    verification_level = VerificationLevel.NONE

                return AgentResult(
                    success=True,
                    data={
                        "position_status": position_status,
                        "verification_level": verification_level,
                        "funding_confirmed": funding_confirmed,
                        "evidence": evidence,
                        "source_type": source_type,
                        "source_priority": source_priority,
                    },
                    agent_name=self.name,
                    correlation_id=cid,
                )

            except Exception as e:
                self.logger.exception("position_verification_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def verify_pi_email(
        self,
        pi: PIProfile,
    ) -> AgentResult:
        """Verify PI email using fallback chain"""
        async with self.track_execution("verify_pi_email") as cid:
            try:
                evidence = []
                verified = False
                confidence = 0.0
                source_type = None

                # Priority 1: Official university page
                if pi.profile_url:
                    html = await self._fetch_page(str(pi.profile_url))
                    if html:
                        emails = self._extract_emails(html)
                        for email in emails:
                            if self._is_university_email(email, pi.university):
                                verified = True
                                confidence = 1.0
                                source_type = SourceType.OFFICIAL_UNIVERSITY
                                evidence.append(Evidence(
                                    candidate_id=pi.id,
                                    candidate_type="pi",
                                    claim="email_verified",
                                    value=email,
                                    source_url=pi.profile_url,
                                    source_type=source_type,
                                    source_priority=self.source_priorities[source_type],
                                    evidence_text=f"Found email on official profile: {email}",
                                    confidence=confidence,
                                ))
                                break

                # Priority 2: Lab page
                if not verified and pi.lab_url:
                    html = await self._fetch_page(str(pi.lab_url))
                    if html:
                        emails = self._extract_emails(html)
                        for email in emails:
                            if self._is_university_email(email, pi.university):
                                verified = True
                                confidence = 0.95
                                source_type = SourceType.OFFICIAL_PI
                                evidence.append(Evidence(
                                    candidate_id=pi.id,
                                    candidate_type="pi",
                                    claim="email_verified",
                                    value=email,
                                    source_url=pi.lab_url,
                                    source_type=source_type,
                                    source_priority=self.source_priorities[source_type],
                                    evidence_text=f"Found email on lab page: {email}",
                                    confidence=confidence,
                                ))
                                break

                # Priority 3: Department page
                if not verified:
                    dept_url = await self._find_department_page(pi.university, pi.department)
                    if dept_url:
                        html = await self._fetch_page(dept_url)
                        if html:
                            emails = self._extract_emails(html)
                            for email in emails:
                                if self._is_university_email(email, pi.university):
                                    verified = True
                                    confidence = 0.95
                                    source_type = SourceType.UNIVERSITY_DIRECTORY
                                    evidence.append(Evidence(
                                        candidate_id=pi.id,
                                        candidate_type="pi",
                                        claim="email_verified",
                                        value=email,
                                        source_url=dept_url,
                                        source_type=source_type,
                                        source_priority=self.source_priorities[source_type],
                                        evidence_text=f"Found email on department page: {email}",
                                        confidence=confidence,
                                    ))
                                    break

                # Priority 4: ORCID
                if not verified and pi.orcid_id:
                    orcid_url = f"https://orcid.org/{pi.orcid_id}"
                    html = await self._fetch_page(orcid_url)
                    if html:
                        emails = self._extract_emails_from_text(html)
                        for email in emails:
                            if self._is_university_email(email, pi.university):
                                verified = True
                                confidence = 0.80
                                source_type = SourceType.ORCID
                                evidence.append(Evidence(
                                    candidate_id=pi.id,
                                    candidate_type="pi",
                                    claim="email_verified",
                                    value=email,
                                    source_url=orcid_url,
                                    source_type=source_type,
                                    source_priority=self.source_priorities[source_type],
                                    evidence_text=f"Found email on ORCID: {email}",
                                    confidence=confidence,
                                ))
                                break

                # Priority 5: Google Scholar
                if not verified and pi.google_scholar_id:
                    scholar_url = f"https://scholar.google.com/citations?user={pi.google_scholar_id}"
                    html = await self._fetch_page(scholar_url)
                    if html:
                        emails = self._extract_emails_from_text(html)
                        for email in emails:
                            if self._is_university_email(email, pi.university):
                                verified = True
                                confidence = 0.75
                                source_type = SourceType.GOOGLE_SCHOLAR
                                evidence.append(Evidence(
                                    candidate_id=pi.id,
                                    candidate_type="pi",
                                    claim="email_verified",
                                    value=email,
                                    source_url=scholar_url,
                                    source_type=source_type,
                                    source_priority=self.source_priorities[source_type],
                                    evidence_text=f"Found email on Google Scholar: {email}",
                                    confidence=confidence,
                                ))
                                break

                # Determine verification level
                if verified and confidence >= 0.9:
                    verification_level = "VERIFIED"
                elif verified and confidence >= 0.7:
                    verification_level = "PROBABLE"
                else:
                    verification_level = "UNVERIFIED"

                return AgentResult(
                    success=True,
                    data={
                        "verified": verified,
                        "confidence": confidence,
                        "verification_level": verification_level,
                        "source_type": source_type,
                        "evidence": evidence,
                    },
                    agent_name=self.name,
                    correlation_id=cid,
                )

            except Exception as e:
                self.logger.exception("pi_verification_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    def _is_university_email(self, email: str, university: str) -> bool:
        if not email or '@' not in email:
            return False
        domain = email.split('@')[1].lower()
        uni_keywords = [w for w in university.lower().split() if len(w) > 3]
        return any(kw in domain for kw in uni_keywords)

    async def _find_department_page(self, university: str, department: Optional[str]) -> Optional[str]:
        # Placeholder - would search for department page
        return None

    async def verify_batch(
        self,
        positions: List[RawPosition],
        pis: List[PIProfile],
    ) -> AgentResult:
        """Verify batch of positions and PIs"""
        async with self.track_execution("verify_batch") as cid:
            try:
                self.logger.info("starting_batch_verification", positions=len(positions), pis=len(pis))

                position_results = []
                pi_results = []

                # Verify positions
                for pos in positions:
                    result = await self.verify_position(pos)
                    if result.success:
                        position_results.append((pos, result.data))

                # Verify PIs
                for pi in pis:
                    result = await self.verify_pi_email(pi)
                    if result.success:
                        pi_results.append((pi, result.data))

                self.logger.info(
                    "batch_verification_complete",
                    positions_verified=len(position_results),
                    pis_verified=len(pi_results),
                )

                return AgentResult(
                    success=True,
                    data={
                        "position_verifications": position_results,
                        "pi_verifications": pi_results,
                    },
                    agent_name=self.name,
                    correlation_id=cid,
                )

            except Exception as e:
                self.logger.exception("batch_verification_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def run(
        self,
        positions: List[RawPosition],
        pis: List[PIProfile],
    ) -> AgentResult:
        """Execute verification"""
        return await self.verify_batch(positions, pis)