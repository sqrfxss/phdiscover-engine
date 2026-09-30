"""
PhDiscover Engine - Main Pipeline Orchestrator
Coordinates all 6 agents in sequence
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import structlog

from phdiscover.agents import (
    ResearchMappingAgent,
    PIDiscoveryAgent,
    ResearchFitAgent,
    OpportunitySearchAgent,
    VerificationAgent,
    RankingAgent,
)
from phdiscover.config import get_settings
from phdiscover.models import ResearchFingerprint, Position, PIProfile
from phdiscover.models.discovery import ResearchMappingResult, RawPosition

logger = structlog.get_logger(__name__)


class PhDiscoverPipeline:
    """Main pipeline orchestrating all 6 agents"""

    def __init__(self):
        self.settings = get_settings()

        # Initialize agents
        self.research_mapping = ResearchMappingAgent()
        self.pi_discovery = PIDiscoveryAgent()
        self.research_fit = ResearchFitAgent()
        self.opportunity_search = OpportunitySearchAgent()
        self.verification = VerificationAgent()
        self.ranking = RankingAgent()

        # Pipeline state
        self.fingerprint: Optional[ResearchFingerprint] = None
        self.mapping: Optional[ResearchMappingResult] = None
        self.pis: List[PIProfile] = []
        self.raw_positions: List[RawPosition] = []
        self.verified_positions: List[Position] = []
        self.ranked_positions: List[Position] = []

    async def run_full_pipeline(
        self,
        fingerprint: ResearchFingerprint,
        min_fit_score: float = 70.0,
    ) -> Dict[str, Any]:
        """Execute complete pipeline"""
        logger.info("starting_full_pipeline", fingerprint_id=str(fingerprint.id))

        results = {
            "fingerprint": fingerprint,
            "stages": {},
            "errors": [],
        }

        try:
            # Stage 1: Research Mapping
            logger.info("stage_1_research_mapping")
            mapping_result = await self.research_mapping.run(fingerprint)
            if not mapping_result.success:
                raise RuntimeError(f"Research mapping failed: {mapping_result.error}")
            self.mapping = mapping_result.data
            results["stages"]["research_mapping"] = {
                "success": True,
                "universities": len(self.mapping.universities),
                "departments": len(self.mapping.departments),
                "research_groups": len(self.mapping.research_groups),
                "total_pis_estimated": self.mapping.total_pis_found,
            }

            # Stage 2: PI Discovery
            logger.info("stage_2_pi_discovery")
            pi_result = await self.pi_discovery.run(self.mapping, fingerprint, min_fit_score)
            if not pi_result.success:
                raise RuntimeError(f"PI discovery failed: {pi_result.error}")
            self.pis = pi_result.data
            results["stages"]["pi_discovery"] = {
                "success": True,
                "total_pis": len(self.pis),
                "verified": sum(1 for p in self.pis if p.verification_level == "VERIFIED"),
                "probable": sum(1 for p in self.pis if p.verification_level == "PROBABLE"),
            }

            # Stage 3: Research Fit Scoring (already done in PI discovery)
            logger.info("stage_3_research_fit")
            # PIs already scored during discovery
            results["stages"]["research_fit"] = {
                "success": True,
                "pis_above_threshold": len(self.pis),
            }

            # Stage 4: Opportunity Search
            logger.info("stage_4_opportunity_search")
            position_result = await self.opportunity_search.run(self.pis, fingerprint)
            if not position_result.success:
                raise RuntimeError(f"Opportunity search failed: {position_result.error}")
            self.raw_positions = position_result.data
            results["stages"]["opportunity_search"] = {
                "success": True,
                "raw_positions_found": len(self.raw_positions),
            }

            # Stage 5: Verification
            logger.info("stage_5_verification")
            verification_result = await self.verification.run(self.raw_positions, self.pis)
            if not verification_result.success:
                raise RuntimeError(f"Verification failed: {verification_result.error}")

            # Convert verified positions to Position models
            self.verified_positions = self._convert_to_positions(
                verification_result.data["position_verifications"],
                verification_result.data["pi_verifications"],
            )
            results["stages"]["verification"] = {
                "success": True,
                "positions_verified": len(self.verified_positions),
            }

            # Stage 6: Ranking
            logger.info("stage_6_ranking")
            pi_dict = {p.id: p for p in self.pis}
            ranking_result = await self.ranking.run(self.verified_positions, pi_dict, fingerprint)
            if not ranking_result.success:
                raise RuntimeError(f"Ranking failed: {ranking_result.error}")

            self.ranked_positions = ranking_result.data["ranked_positions"]
            results["stages"]["ranking"] = {
                "success": True,
                "ranked_positions": len(self.ranked_positions),
                "high_priority": sum(1 for p in self.ranked_positions if p.contact_priority.value == "HIGH"),
                "medium_priority": sum(1 for p in self.ranked_positions if p.contact_priority.value == "MEDIUM"),
            }

            results["final_output"] = {
                "ranked_positions": self.ranked_positions,
                "pis": self.pis,
                "mapping": self.mapping,
            }

            logger.info("pipeline_complete", **results["stages"]["ranking"])

        except Exception as e:
            logger.exception("pipeline_failed", error=str(e))
            results["errors"].append(str(e))
            results["success"] = False

        results["completed_at"] = datetime.utcnow()
        return results

    def _convert_to_positions(
        self,
        position_verifications: List[tuple],
        pi_verifications: List[tuple],
    ) -> List[Position]:
        """Convert verified raw positions to Position models"""
        positions = []

        # Build PI verification lookup
        pi_verification_map = {pi.id: data for pi, data in pi_verifications}

        for raw_pos, pos_verification in position_verifications:
            # Find matching PI
            pi_id = None
            if hasattr(raw_pos, 'raw_data') and 'pi_id' in raw_pos.raw_data:
                try:
                    pi_id = UUID(raw_pos.raw_data['pi_id'])
                except (ValueError, KeyError):
                    pass

            pi_verification = pi_verification_map.get(pi_id) if pi_id else None
            pi = next((p for p in self.pis if p.id == pi_id), None) if pi_id else None

            position = Position(
                canonical_id=f"{raw_pos.source_name}_{self._generate_canonical_id(raw_pos)}",
                title=raw_pos.title,
                university=raw_pos.university,
                department=raw_pos.department,
                country=raw_pos.country,
                city=raw_pos.city,
                pi_id=pi_id,
                pi_name=raw_pos.pi_name,
                pi_email=pi_verification.get("evidence", [{}])[0].get("value") if pi_verification else raw_pos.pi_email,
                description=raw_pos.description,
                application_deadline=raw_pos.application_deadline,
                funding_amount=self._extract_funding_amount(raw_pos.funding_info),
                funding_details=raw_pos.funding_info,
                url=raw_pos.source_url,
                source_url=raw_pos.source_url,
                email_verified=pi_verification.get("verified", False) if pi_verification else False,
                verification_source=pi_verification.get("source_type") if pi_verification else None,
                email_confidence=pi_verification.get("confidence", 0.0) if pi_verification else 0.0,
                verification_level=pi_verification.get("verification_level", "NONE") if pi_verification else "NONE",
                research_domain=self._infer_research_domain(raw_pos),
                discovered_at=raw_pos.extracted_at,
                evidence=pos_verification.get("evidence", []) if isinstance(pos_verification, dict) else [],
            )
            positions.append(position)

        return positions

    def _generate_canonical_id(self, raw_pos: RawPosition) -> str:
        """Generate canonical ID for deduplication"""
        import hashlib
        content = f"{raw_pos.title}_{raw_pos.university}_{raw_pos.country}"
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def _extract_funding_amount(self, funding_info: Optional[str]) -> Optional[str]:
        if not funding_info:
            return None
        import re
        # Extract currency + amount
        patterns = [
            r'(€|\$|£|CAD|USD|EUR)\s*([\d,]+(?:\.\d{2})?)',
            r'([\d,]+(?:\.\d{2})?)\s*(€|\$|£|CAD|USD|EUR)',
        ]
        for pattern in patterns:
            match = re.search(pattern, funding_info)
            if match:
                return match.group(0)
        return None

    def _infer_research_domain(self, raw_pos: RawPosition) -> Optional[str]:
        text = f"{raw_pos.title} {raw_pos.description or ''}".lower()
        domains = {
            "biomechanics": ["biomechanics", "biomechanical"],
            "motor_control": ["motor control", "motor learning", "sensorimotor"],
            "gait_analysis": ["gait", "walking", "locomotion"],
            "injury_prevention": ["injury", "prevention", "rehabilitation"],
            "wearable_sensing": ["wearable", "imu", "sensor", "accelerometer"],
            "machine_learning": ["machine learning", "deep learning", "neural network", "lstm", "cnn"],
        }
        for domain, keywords in domains.items():
            if any(kw in text for kw in keywords):
                return domain
        return None


async def run_pipeline(
    fingerprint: ResearchFingerprint,
    min_fit_score: float = 70.0,
) -> Dict[str, Any]:
    """Convenience function to run full pipeline"""
    pipeline = PhDiscoverPipeline()
    return await pipeline.run_full_pipeline(fingerprint, min_fit_score)