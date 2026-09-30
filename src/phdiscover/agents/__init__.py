"""
PhDiscover Engine - Agents Package
"""

from __future__ import annotations

from phdiscover.agents.base import AgentResult, BaseAgent
from phdiscover.agents.research_mapping import ResearchMappingAgent
from phdiscover.agents.pi_discovery import PIDiscoveryAgent
from phdiscover.agents.research_fit import ResearchFitAgent
from phdiscover.agents.opportunity_search import OpportunitySearchAgent
from phdiscover.agents.verification import VerificationAgent
from phdiscover.agents.ranking import RankingAgent

__all__ = [
    "AgentResult",
    "BaseAgent",
    "ResearchMappingAgent",
    "PIDiscoveryAgent",
    "ResearchFitAgent",
    "OpportunitySearchAgent",
    "VerificationAgent",
    "RankingAgent",
]