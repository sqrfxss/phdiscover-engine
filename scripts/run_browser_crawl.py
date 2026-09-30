"""Run the browser crawler against EURAXESS with real data extraction."""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from phdiscover.crawlers.browser_crawler import BrowserCrawler

KEYWORDS = [
    "biomechanics", "motor control", "gait analysis", "human movement", "kinesiology",
    "human movement biomechanics", "sensorimotor", "ground reaction force",
    "wearable sensor movement", "neural network biomechanics", "postural control",
    "musculoskeletal modelling", "clinical movement analysis",
]


async def main():
    print("=" * 60)
    print("PhDiscover — Browser Crawler (EURAXESS)")
    print("=" * 60)

    async with BrowserCrawler(headless=True) as crawler:
        positions = await crawler.crawl_euraxess(
            keywords=KEYWORDS,
            max_results=50,
        )

    print()
    print("=" * 60)
    print(f"RESULT: {len(positions)} relevant positions")
    print("=" * 60)

    for i, p in enumerate(positions, 1):
        print()
        print(f"[{i}] {p.title[:70]}")
        print(f"    Org: {p.organization}")
        print(f"    Location: {p.city}, {p.country}")
        print(f"    Posted: {p.posted}")
        print(f"    Field: {p.research_field[:50]}")
        print(f"    URL: {p.url}")

    # Save
    out = str(ROOT / "data" / "browser_positions.json")
    crawler = BrowserCrawler()
    crawler.save(positions, out)

    # Update web data with real fit scores
    crawler2 = BrowserCrawler()
    web_data = []
    for p in positions:
        fit = crawler2._score_fit(p)
        priority = "HIGH" if fit >= 75 else "MEDIUM" if fit >= 55 else "LOW"
        web_data.append({
            "id": f"euraxess-{abs(hash(p.url)) % 100000}",
            "title": p.title,
            "university": p.organization,
            "country": p.country or "N/A",
            "department": p.department or "N/A",
            "pi_name": "See official posting",
            "pi_email": "",
            "funding": "PhD position — see official posting",
            "application_deadline": p.deadline or "See official posting",
            "opportunity_type": p.opportunity_type,
            "research_domain": p.research_field or "Biomechanics / Human Movement",
            "research_fit_score": fit,
            "contact_priority": priority,
            "url": p.url,
            "source_url": p.url,
            "posted": p.posted,
            "description": p.description,
            "evidence": [{
                "claim": "Position exists on EURAXESS (official EU source)",
                "value": p.title,
                "source_url": p.url,
                "source_type": "euraxess",
                "confidence": 0.75,
            }],
        })
    web_data.sort(key=lambda x: -x["research_fit_score"])
    with open(str(ROOT / "web" / "data" / "ranked_opportunities.json"), "w", encoding="utf-8") as f:
        json.dump(web_data, f, ensure_ascii=False, indent=2)
    print(f"Updated web data with {len(web_data)} positions (sorted by fit score)")


if __name__ == "__main__":
    asyncio.run(main())
