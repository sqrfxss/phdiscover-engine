"""
PhDiscover Engine - Parser Test Script
Tests crawlers against golden dataset
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import structlog

from phdiscover.crawlers import (
    EuraxessCrawler,
    FindAPhDCrawler,
    AcademicPositionsCrawler,
    OpenAlexCrawler,
)

logger = structlog.get_logger(__name__)

# Golden dataset fixtures - in production these would be real saved HTML pages
GOLDEN_FIXTURES = {
    "euraxess": {
        "listing": "tests/golden/euraxess/listing_page.html",
        "detail": "tests/golden/euraxess/detail_page.html",
    },
    "findaphd": {
        "listing": "tests/golden/findaphd/listing_page.html",
        "detail": "tests/golden/findaphd/detail_page.html",
    },
    "academicpositions": {
        "listing": "tests/golden/academicpositions/listing_page.html",
        "detail": "tests/golden/academicpositions/detail_page.html",
    },
}


async def test_parser(
    crawler,
    source_name: str,
    golden_dir: Path,
    golden: bool = True,
) -> Dict[str, Any]:
    """Test a single parser against golden dataset"""
    results = {
        "source": source_name,
        "tests": [],
        "passed": 0,
        "failed": 0,
        "errors": [],
    }

    if not golden:
        logger.info("skipping_golden_tests", source=source_name)
        return results

    # Test listing page parsing
    listing_file = golden_dir / "listing_page.html"
    if listing_file.exists():
        html = listing_file.read_text(encoding="utf-8")
        try:
            start = datetime.utcnow()
            positions = await crawler.parse_listing_page(html, "https://test.example.com/listing")
            duration = (datetime.utcnow() - start).total_seconds() * 1000

            results["tests"].append({
                "test": "parse_listing_page",
                "passed": True,
                "positions_found": len(positions),
                "duration_ms": duration,
            })
            results["passed"] += 1
            logger.info("listing_parse_ok", source=source_name, count=len(positions))
        except Exception as e:
            results["tests"].append({
                "test": "parse_listing_page",
                "passed": False,
                "error": str(e),
            })
            results["failed"] += 1
            results["errors"].append(f"listing: {str(e)}")
            logger.error("listing_parse_failed", source=source_name, error=str(e))
    else:
        results["tests"].append({
            "test": "parse_listing_page",
            "passed": False,
            "error": "Golden file not found",
        })
        results["failed"] += 1

    # Test detail page parsing
    detail_file = golden_dir / "detail_page.html"
    if detail_file.exists():
        html = detail_file.read_text(encoding="utf-8")
        try:
            start = datetime.utcnow()
            position = await crawler.parse_detail_page(html, "https://test.example.com/detail/123")
            duration = (datetime.utcnow() - start).total_seconds() * 1000

            results["tests"].append({
                "test": "parse_detail_page",
                "passed": position is not None,
                "position_parsed": position is not None,
                "duration_ms": duration,
            })
            if position:
                results["passed"] += 1
            else:
                results["failed"] += 1
            logger.info("detail_parse_ok", source=source_name, parsed=position is not None)
        except Exception as e:
            results["tests"].append({
                "test": "parse_detail_page",
                "passed": False,
                "error": str(e),
            })
            results["failed"] += 1
            results["errors"].append(f"detail: {str(e)}")
            logger.error("detail_parse_failed", source=source_name, error=str(e))
    else:
        results["tests"].append({
            "test": "parse_detail_page",
            "passed": False,
            "error": "Golden file not found",
        })
        results["failed"] += 1

    return results


async def run_all_parser_tests(source: Optional[str] = None, golden: bool = True) -> Dict[str, Any]:
    """Run parser tests for all or specific source"""
    crawlers = {
        "euraxess": EuraxessCrawler(),
        "findaphd": FindAPhDCrawler(),
        "academicpositions": AcademicPositionsCrawler(),
        "openalex": OpenAlexCrawler(),
    }

    if source:
        crawlers = {source: crawlers[source]}

    golden_base = Path("tests/golden")
    all_results = {
        "started_at": datetime.utcnow().isoformat(),
        "sources": {},
        "total_passed": 0,
        "total_failed": 0,
    }

    for name, crawler in crawlers.items():
        golden_dir = golden_base / name
        result = await test_parser(crawler, name, golden_dir, golden)
        all_results["sources"][name] = result
        all_results["total_passed"] += result["passed"]
        all_results["total_failed"] += result["failed"]

    all_results["completed_at"] = datetime.utcnow().isoformat()

    # Print summary
    print(f"\n{'='*60}")
    print(f"PARSER TEST RESULTS")
    print(f"{'='*60}")
    print(f"Total Passed: {all_results['total_passed']}")
    print(f"Total Failed: {all_results['total_failed']}")
    print(f"\nPer Source:")
    for name, result in all_results["sources"].items():
        status = "✅" if result["failed"] == 0 else "❌"
        print(f"  {status} {name}: {result['passed']} passed, {result['failed']} failed")
        for test in result["tests"]:
            test_status = "✓" if test["passed"] else "✗"
            print(f"    {test_status} {test['test']}")
            if not test["passed"]:
                print(f"      Error: {test.get('error', 'Unknown')}")

    return all_results


async def main(source: Optional[str] = None, golden: bool = True):
    """CLI entry point"""
    await run_all_parser_tests(source, golden)


if __name__ == "__main__":
    import sys
    source = sys.argv[1] if len(sys.argv) > 1 else None
    golden = "--no-golden" not in sys.argv
    asyncio.run(main(source, golden))