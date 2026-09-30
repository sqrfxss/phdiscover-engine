"""
PhDiscover Engine - Crawler Runner
Orchestrates all crawlers
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from phdiscover.config import get_settings
from phdiscover.crawlers import (
    EuraxessCrawler,
    FindAPhDCrawler,
    AcademicPositionsCrawler,
    OpenAlexCrawler,
)
from phdiscover.models.discovery import RawPosition
from phdiscover.recovery import with_recovery

logger = structlog.get_logger(__name__)


class CrawlerOrchestrator:
    """Orchestrates all crawlers with recovery"""

    def __init__(self):
        self.settings = get_settings()
        self.crawlers = {
            "euraxess": EuraxessCrawler(),
            "findaphd": FindAPhDCrawler(),
            "academicpositions": AcademicPositionsCrawler(),
            "openalex": OpenAlexCrawler(),
        }

        # Enable/disable crawlers from config
        enabled_sources = {s["name"] for s in self.settings.sources if s.get("parser") in self.crawlers}
        self.crawlers = {k: v for k, v in self.crawlers.items() if k in enabled_sources}

        logger.info("crawler_orchestrator_initialized", crawlers=list(self.crawlers.keys()))

    async def run_all_crawlers(
        self,
        source: Optional[str] = None,
        country: Optional[str] = None,
        dry_run: bool = False,
        max_items: int = 0,
    ) -> Dict[str, Any]:
        """Run all or specific crawlers"""
        results = {
            "started_at": datetime.utcnow(),
            "crawlers_run": [],
            "total_positions": 0,
            "errors": [],
        }

        crawlers_to_run = {source: self.crawlers[source]} if source else self.crawlers

        for name, crawler in crawlers_to_run.items():
            logger.info("running_crawler", crawler=name)

            try:
                if hasattr(crawler, 'crawl_by_concepts') and name == "openalex":
                    # OpenAlex uses different interface
                    positions = await self._run_openalex_crawler(crawler, country)
                else:
                    positions = await self._run_crawler_with_recovery(crawler)

                if max_items > 0:
                    positions = positions[:max_items]

                results["crawlers_run"].append({
                    "name": name,
                    "positions_found": len(positions),
                    "success": True,
                })
                results["total_positions"] += len(positions)

                # Save positions (in production, to database)
                await self._save_positions(name, positions)

            except Exception as e:
                logger.exception("crawler_failed", crawler=name, error=str(e))
                results["crawlers_run"].append({
                    "name": name,
                    "positions_found": 0,
                    "success": False,
                    "error": str(e),
                })
                results["errors"].append(f"{name}: {str(e)}")

        results["completed_at"] = datetime.utcnow()
        results["duration_seconds"] = (results["completed_at"] - results["started_at"]).total_seconds()

        logger.info(
            "all_crawlers_complete",
            total_positions=results["total_positions"],
            successful=sum(1 for c in results["crawlers_run"] if c["success"]),
            failed=sum(1 for c in results["crawlers_run"] if not c["success"]),
        )

        return results

    async def _run_crawler_with_recovery(self, crawler) -> List[RawPosition]:
        """Run crawler with recovery"""
        try:
            return await crawler.crawl()
        except Exception as e:
            logger.warning("crawler_initial_failed", crawler=crawler.source_name, error=str(e))
            # Try recovery
            try:
                return await with_recovery(crawler.source_name, crawler.base_url, crawler.crawl)
            except Exception as e2:
                logger.exception("crawler_recovery_failed", crawler=crawler.source_name, error=str(e2))
                raise

    async def _run_openalex_crawler(self, crawler: OpenAlexCrawler, country: Optional[str]) -> List[RawPosition]:
        """Run OpenAlex crawler with concept-based search"""
        # Get concept IDs from config
        concept_ids = [
            "C154945302",  # Biomechanics
            "C162324750",  # Motor control
            "C11413529",   # Gait analysis
            "C2775924381", # Sensorimotor
            "C159985018",  # Wearable tech
        ]

        countries = [country] if country else ["CA", "NL", "DE", "FI", "NZ"]

        return await crawler.crawl_by_concepts(concept_ids, countries)

    async def _save_positions(self, source: str, positions: List[RawPosition]):
        """Save positions to database (placeholder)"""
        logger.debug("saving_positions", source=source, count=len(positions))
        # In production: insert into database with deduplication
        pass


async def main(
    source: Optional[str] = None,
    country: Optional[str] = None,
    dry_run: bool = False,
    max_items: int = 0,
):
    """CLI entry point"""
    orchestrator = CrawlerOrchestrator()
    result = await orchestrator.run_all_crawlers(source, country, dry_run, max_items)

    print(f"\n{'='*50}")
    print(f"CRAWL COMPLETE")
    print(f"{'='*50}")
    print(f"Total Positions: {result['total_positions']}")
    print(f"Duration: {result['duration_seconds']:.1f}s")
    print(f"\nPer Crawler:")
    for c in result["crawlers_run"]:
        status = "✅" if c["success"] else "❌"
        print(f"  {status} {c['name']}: {c['positions_found']} positions")
        if not c["success"]:
            print(f"      Error: {c.get('error', 'Unknown')}")

    if result["errors"]:
        print(f"\nErrors:")
        for err in result["errors"]:
            print(f"  - {err}")


if __name__ == "__main__":
    asyncio.run(main())