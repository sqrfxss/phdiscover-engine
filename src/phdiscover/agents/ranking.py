"""
PhDiscover Engine - Ranking Agent
Multi-criteria weighted ranking with contact priority
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import structlog
from rapidfuzz import fuzz

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.models import Position, PIProfile, MatchScore, ContactPriority, OpportunityType
from phdiscover.models.discovery import RawPosition, ExtractedPosition

logger = structlog.get_logger(__name__)


class RankingAgent(BaseAgent):
    """Agent 6: Final ranking with contact priority"""

    def __init__(self):
        super().__init__("ranking")
        self.settings = get_settings()

        # Load weights from config
        weights_config = self.settings.weights.get("ranking_weights", {})
        self.weights = {
            "research_fit": weights_config.get("research_fit", 0.25),
            "method_fit": weights_config.get("method_fit", 0.15),
            "supervisor_fit": weights_config.get("supervisor_fit", 0.12),
            "lab_fit": weights_config.get("lab_fit", 0.10),
            "eligibility": weights_config.get("eligibility", 0.08),
            "funding": weights_config.get("funding", 0.08),
            "career_alignment": weights_config.get("career_alignment", 0.07),
            "freshness": weights_config.get("freshness", 0.05),
            "opportunity_strength": weights_config.get("opportunity_strength", 0.05),
            "institutional_fit": weights_config.get("institutional_fit", 0.05),
        }

        # Contact priority thresholds
        priority_config = self.settings.weights.get("contact_priority_thresholds", {})
        self.priority_thresholds = {
            "high": priority_config.get("high", 85),
            "medium": priority_config.get("medium", 70),
            "low": priority_config.get("low", 0),
        }

        # Freshness decay
        freshness_config = self.settings.weights.get("freshness_decay", {})
        self.freshness_half_life = freshness_config.get("half_life_days", 30)

    def _calculate_freshness_score(self, discovered_at: datetime) -> float:
        """Calculate freshness score based on age"""
        days_old = (datetime.utcnow() - discovered_at).days
        if days_old <= 0:
            return 1.0
        # Exponential decay
        import math
        return math.exp(-days_old / self.freshness_half_life)

    def _calculate_method_fit(
        self,
        position: Position,
        fingerprint_methods: List[str],
    ) -> float:
        """Calculate method fit score"""
        if not fingerprint_methods:
            return 50.0

        position_methods = [m.lower() for m in position.methods]
        position_text = " ".join([
            position.title,
            position.description or "",
            " ".join(position.requirements),
        ]).lower()

        matched = 0
        for method in fingerprint_methods:
            method_lower = method.lower()
            if method_lower in position_methods or method_lower in position_text:
                matched += 1

        return min((matched / len(fingerprint_methods)) * 100, 100.0)

    def _calculate_supervisor_fit(
        self,
        position: Position,
        pi: Optional[PIProfile],
    ) -> float:
        """Calculate supervisor fit score"""
        if not pi:
            return 30.0

        score = pi.research_fit_score

        # Boost for verified email
        if pi.email_verified:
            score += 10

        # Boost for active grants
        if pi.active_grants:
            score += 10

        # Boost for hiring signals
        if pi.hiring_signals:
            score += 5

        return min(score, 100.0)

    def _calculate_lab_fit(
        self,
        position: Position,
        pi: Optional[PIProfile],
    ) -> float:
        """Calculate lab fit score"""
        score = 50.0  # Base

        if pi:
            # Equipment match would need more data
            if pi.recent_papers:
                score += 10

            # Team size indicator
            if pi.phd_students_count > 0:
                score += 10
            if pi.phd_students_count > 3:
                score += 5

            # International collaboration
            if pi.active_grants:
                score += 5

        return min(score, 100.0)

    def _calculate_eligibility(
        self,
        position: Position,
        fingerprint: "ResearchFingerprint",
    ) -> float:
        """Calculate eligibility score"""
        score = 70.0  # Base assumption

        # Check degree requirements
        requirements_text = " ".join(position.requirements).lower()
        if "master" in requirements_text or "msc" in requirements_text:
            score += 10
        if "phd" in requirements_text and "candidate" in requirements_text:
            score -= 20  # Might be for current PhD candidates only

        # Language requirements
        if "ielts" in requirements_text or "toefl" in requirements_text:
            score += 5

        # Citizenship/residency
        if "citizen" in requirements_text or "resident" in requirements_text:
            score -= 10  # Might restrict international applicants

        return max(0, min(score, 100.0))

    def _calculate_funding_score(self, position: Position) -> float:
        """Calculate funding score"""
        if not position.funding_amount and not position.funding_details:
            return 20.0

        text = (position.funding_details or "").lower()
        funding_amount = (position.funding_amount or "").lower()

        # Explicit funding amount
        if any(c.isdigit() for c in funding_amount):
            return 90.0

        # Funding keywords
        if any(kw in text for kw in ["fully funded", "full funding", "stipend", "salary"]):
            return 85.0

        if any(kw in text for kw in ["funded", "funding available", "scholarship"]):
            return 70.0

        if any(kw in text for kw in ["partial", "partially", "tuition"]):
            return 40.0

        return 50.0

    def _calculate_career_alignment(
        self,
        position: Position,
        fingerprint: "ResearchFingerprint",
    ) -> float:
        """Calculate career alignment score"""
        score = 50.0

        goals = [g.lower() for g in fingerprint.career_goals]

        if "academic" in goals or "research" in goals:
            if position.opportunity_type in [
                OpportunityType.FUNDED_PHD,
                OpportunityType.PHD_PROJECT,
                OpportunityType.DOCTORAL_RESEARCHER,
            ]:
                score += 30

        if "industry" in goals:
            if "industry" in position.description.lower() or "collaboration" in position.description.lower():
                score += 20

        return min(score, 100.0)

    def _calculate_opportunity_strength(
        self,
        position: Position,
        pi: Optional[PIProfile],
    ) -> float:
        """Calculate opportunity strength (pre-vacancy signals)"""
        score = 50.0

        if pi:
            # New grants
            recent_grants = [g for g in pi.active_grants if g.start_date and
                            (datetime.utcnow().date() - g.start_date).days < 365]
            if recent_grants:
                score += 20

            # Hiring signals
            if pi.hiring_signals:
                score += 15

            # Lab expansion (multiple PhD students)
            if pi.phd_students_count > 2:
                score += 10

        # Recurring recruitment pattern (would need historical data)
        # Infrastructure match
        if position.methods:
            score += 5

        return min(score, 100.0)

    def _calculate_institutional_fit(
        self,
        position: Position,
    ) -> float:
        """Calculate institutional fit score"""
        score = 60.0  # Base

        # Top universities would get higher scores
        # This would need a university ranking database
        top_universities = [
            "toronto", "ubc", "mcgill", "waterloo", "alberta",
            "amsterdam", "delft", "leiden", "utrecht", "groningen",
            "munich", "heidelberg", "berlin", "bonn", "freiburg",
            "helsinki", "aalto", "turku", "tampere",
            "auckland", "otago", "victoria", "canterbury",
        ]

        uni_lower = position.university.lower()
        if any(top in uni_lower for top in top_universities):
            score += 25

        # Department reputation (simplified)
        dept_keywords = ["biomechanics", "motor control", "kinesiology", "human kinetics", "rehabilitation"]
        if any(kw in position.department.lower() for kw in dept_keywords if position.department):
            score += 10

        return min(score, 100.0)

    def _calculate_match_score(
        self,
        position: Position,
        pi: Optional[PIProfile],
        fingerprint: "ResearchFingerprint",
    ) -> MatchScore:
        """Calculate comprehensive match score"""
        return MatchScore(
            research_fit=position.research_fit_score,
            method_fit=self._calculate_method_fit(position, fingerprint.methods + fingerprint.technical_methods),
            supervisor_fit=self._calculate_supervisor_fit(position, pi),
            lab_fit=self._calculate_lab_fit(position, pi),
            eligibility=self._calculate_eligibility(position, fingerprint),
            funding=self._calculate_funding_score(position),
            career_alignment=self._calculate_career_alignment(position, fingerprint),
            freshness=self._calculate_freshness_score(position.discovered_at) * 100,
            opportunity_strength=self._calculate_opportunity_strength(position, pi),
            institutional_fit=self._calculate_institutional_fit(position),
        )

    def _determine_contact_priority(self, final_score: float) -> ContactPriority:
        """Determine contact priority from final score"""
        if final_score >= self.priority_thresholds["high"]:
            return ContactPriority.HIGH
        elif final_score >= self.priority_thresholds["medium"]:
            return ContactPriority.MEDIUM
        return ContactPriority.LOW

    async def rank_positions(
        self,
        positions: List[Position],
        pis: Dict[UUID, PIProfile],
        fingerprint: "ResearchFingerprint",
    ) -> AgentResult:
        """Rank all positions"""
        async with self.track_execution("rank_positions") as cid:
            try:
                self.logger.info("starting_ranking", position_count=len(positions))

                ranked = []

                for position in positions:
                    pi = pis.get(position.pi_id) if position.pi_id else None

                    # Calculate match score
                    match_score = self._calculate_match_score(position, pi, fingerprint)

                    # Update position with scores
                    position.research_fit_score = match_score.research_fit
                    position.final_rank_score = match_score.overall
                    position.contact_priority = self._determine_contact_priority(match_score.overall)

                    ranked.append((position, match_score))

                # Sort by final rank score descending
                ranked.sort(key=lambda x: x[0].final_rank_score, reverse=True)

                self.logger.info(
                    "ranking_complete",
                    total=len(ranked),
                    high_priority=sum(1 for p, _ in ranked if p.contact_priority == ContactPriority.HIGH),
                    medium_priority=sum(1 for p, _ in ranked if p.contact_priority == ContactPriority.MEDIUM),
                    low_priority=sum(1 for p, _ in ranked if p.contact_priority == ContactPriority.LOW),
                )

                return AgentResult(
                    success=True,
                    data={
                        "ranked_positions": [p for p, _ in ranked],
                        "match_scores": {str(p.id): ms for p, ms in ranked},
                    },
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={
                        "total_ranked": len(ranked),
                        "high_priority": sum(1 for p, _ in ranked if p.contact_priority == ContactPriority.HIGH),
                    },
                )

            except Exception as e:
                self.logger.exception("ranking_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def run(
        self,
        positions: List[Position],
        pis: Dict[UUID, PIProfile],
        fingerprint: "ResearchFingerprint",
    ) -> AgentResult:
        """Execute ranking"""
        return await self.rank_positions(positions, pis, fingerprint)