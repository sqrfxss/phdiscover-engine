"""
PhDiscover Engine - Research Fit Agent
Scores PIs against user CV using weighted criteria
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import structlog
import rapidfuzz
from rapidfuzz import fuzz, process

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.config import get_settings
from phdiscover.models import PIProfile, ResearchFingerprint
from phdiscover.models.discovery import PIResearchFit, ConceptMatch

logger = structlog.get_logger(__name__)


class ResearchFitAgent(BaseAgent):
    """Agent 3: Scores PIs against user Research Fingerprint"""

    def __init__(self):
        super().__init__("research_fit")
        self.settings = get_settings()
        self.weights = self.settings.weights.get("research_fit_criteria", {
            "motor_control": 25,
            "biomechanics": 20,
            "sensorimotor_control": 20,
            "human_movement": 10,
            "machine_learning": 10,
            "neuroscience": 10,
            "methods_overlap": 5,
        })
        self.thresholds = self.settings.weights.get("research_fit_thresholds", {
            "exceptional": 90,
            "strong": 80,
            "good": 70,
            "reject_below": 70,
        })

    def _normalize_text(self, text: str) -> str:
        """Normalize text for matching"""
        return text.lower().strip().replace("_", " ").replace("-", " ")

    def _fuzzy_match_score(self, query: str, choices: List[str], threshold: int = 80) -> List[tuple]:
        """Find fuzzy matches with scores"""
        results = []
        query_norm = self._normalize_text(query)
        for choice in choices:
            choice_norm = self._normalize_text(choice)
            score = fuzz.token_set_ratio(query_norm, choice_norm)
            if score >= threshold:
                results.append((choice, score))
        return sorted(results, key=lambda x: x[1], reverse=True)

    def _match_concepts(
        self,
        fingerprint_concepts: List[str],
        pi_concepts: List[str],
        weight: float,
    ) -> Dict[str, Any]:
        """Match fingerprint concepts to PI concepts"""
        matched = []
        total_score = 0.0

        for fp_concept in fingerprint_concepts:
            matches = self._fuzzy_match_score(fp_concept, pi_concepts, threshold=75)
            if matches:
                best_match, score = matches[0]
                matched.append(ConceptMatch(
                    concept_id="",
                    concept_name=best_match,
                    score=score / 100.0 * weight,
                    level=0,
                    matched_keywords=[fp_concept],
                ))
                total_score += score / 100.0 * weight

        return {
            "score": min(total_score, weight),
            "matched": matched,
        }

    def _match_keywords(
        self,
        fingerprint_keywords: List[str],
        pi_text: str,
        weight: float,
    ) -> Dict[str, Any]:
        """Match fingerprint keywords against PI text (papers, grants, etc.)"""
        matched = []
        total_score = 0.0
        pi_text_lower = pi_text.lower()

        for keyword in fingerprint_keywords:
            kw_lower = keyword.lower()
            if kw_lower in pi_text_lower:
                # Count occurrences
                count = pi_text_lower.count(kw_lower)
                # Score based on frequency (capped)
                kw_score = min(count * 2, weight)
                matched.append(ConceptMatch(
                    concept_id="",
                    concept_name=keyword,
                    score=kw_score,
                    level=0,
                    matched_keywords=[keyword],
                ))
                total_score += kw_score

        return {
            "score": min(total_score, weight),
            "matched": matched,
        }

    async def score_pi(
        self,
        pi: PIProfile,
        fingerprint: ResearchFingerprint,
    ) -> PIResearchFit:
        """Score a single PI against fingerprint"""
        # Collect all PI text for keyword matching
        pi_text_parts = [
            pi.name,
            " ".join(pi.research_keywords),
            " ".join([p.title for p in pi.recent_papers]),
            " ".join([g.title for g in pi.active_grants]),
        ]
        pi_text = " ".join(pi_text_parts).lower()

        pi_concepts = [kw.lower() for kw in pi.research_keywords]
        fp_concepts = [c.lower() for c in fingerprint.core_topics + fingerprint.adjacent_topics]
        fp_methods = [m.lower() for m in fingerprint.methods + fingerprint.technical_methods]
        fp_data = [d.lower() for d in fingerprint.data_modalities]

        # 1. Topic matching (core topics + adjacent)
        topic_result = self._match_concepts(fp_concepts, pi_concepts, 50.0)

        # 2. Method matching
        method_result = self._match_keywords(fp_methods, pi_text, 25.0)

        # 3. Data modality matching
        data_result = self._match_keywords(fp_data, pi_text, 15.0)

        # 4. Population matching
        fp_pops = [p.lower() for p in fingerprint.populations]
        pop_result = self._match_keywords(fp_pops, pi_text, 10.0)

        # 5. Technical method matching (specific tools)
        tech_result = self._match_keywords(fingerprint.technical_methods, pi_text, 10.0)

        # Calculate total
        total_score = (
            topic_result["score"] +
            method_result["score"] +
            data_result["score"] +
            pop_result["score"] +
            tech_result["score"]
        )

        # Determine recommendation
        if total_score >= self.thresholds["exceptional"]:
            recommendation = "EXCEPTIONAL"
        elif total_score >= self.thresholds["strong"]:
            recommendation = "STRONG"
        elif total_score >= self.thresholds["good"]:
            recommendation = "GOOD"
        else:
            recommendation = "REJECT"

        # Combine all matched concepts
        all_matched = (
            topic_result["matched"] +
            method_result["matched"] +
            data_result["matched"] +
            pop_result["matched"] +
            tech_result["matched"]
        )

        return PIResearchFit(
            pi_id=pi.id,
            pi_name=pi.name,
            overall_score=round(total_score, 2),
            breakdown={
                "topics": round(topic_result["score"], 2),
                "methods": round(method_result["score"], 2),
                "data_modalities": round(data_result["score"], 2),
                "populations": round(pop_result["score"], 2),
                "technical_methods": round(tech_result["score"], 2),
            },
            matched_concepts=all_matched,
            matched_keywords=[m.concept_name for m in all_matched],
            matched_methods=[m.concept_name for m in method_result["matched"] + tech_result["matched"]],
            matched_papers=[p.title for p in pi.recent_papers[:5]],
            recommendation=recommendation,
            details={
                "topic_matches": len(topic_result["matched"]),
                "method_matches": len(method_result["matched"]),
                "data_matches": len(data_result["matched"]),
                "population_matches": len(pop_result["matched"]),
            },
        )

    async def score_all_pis(
        self,
        pis: List[PIProfile],
        fingerprint: ResearchFingerprint,
        min_score: float = 70.0,
    ) -> AgentResult:
        """Score all PIs and filter by minimum score"""
        async with self.track_execution("score_all_pis") as cid:
            try:
                self.logger.info("starting_fit_scoring", pi_count=len(pis))

                scored_pis = []
                fit_results = []

                for pi in pis:
                    fit = await self.score_pi(pi, fingerprint)
                    fit_results.append(fit)

                    if fit.overall_score >= min_score:
                        # Update PI with fit score
                        pi.research_fit_score = fit.overall_score
                        scored_pis.append(pi)

                # Sort by score descending
                scored_pis.sort(key=lambda p: p.research_fit_score, reverse=True)

                self.logger.info(
                    "fit_scoring_complete",
                    total=len(pis),
                    passed=len(scored_pis),
                    exceptional=sum(1 for f in fit_results if f.recommendation == "EXCEPTIONAL"),
                    strong=sum(1 for f in fit_results if f.recommendation == "STRONG"),
                    good=sum(1 for f in fit_results if f.recommendation == "GOOD"),
                    rejected=sum(1 for f in fit_results if f.recommendation == "REJECT"),
                )

                return AgentResult(
                    success=True,
                    data={
                        "filtered_pis": scored_pis,
                        "all_fit_results": fit_results,
                    },
                    agent_name=self.name,
                    correlation_id=cid,
                    metadata={
                        "total_pis": len(pis),
                        "passed_threshold": len(scored_pis),
                        "min_score": min_score,
                    },
                )

            except Exception as e:
                self.logger.exception("fit_scoring_failed", error=str(e))
                return AgentResult(
                    success=False,
                    error=str(e),
                    agent_name=self.name,
                    correlation_id=cid,
                )

    async def run(
        self,
        pis: List[PIProfile],
        fingerprint: ResearchFingerprint,
        min_score: float = 70.0,
    ) -> AgentResult:
        """Execute research fit scoring"""
        return await self.score_all_pis(pis, fingerprint, min_score)