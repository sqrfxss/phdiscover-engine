"""
Browser-based crawler using Playwright for JS-heavy sites (EURAXESS, AcademicPositions).

This is the JS_REQUIRED recovery path from the failure taxonomy.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class BrowserPosition:
    """Position extracted via headless browser."""
    title: str
    organization: str
    country: str
    city: str
    posted: str
    url: str
    research_field: str = ""
    department: str = ""
    description: str = ""
    opportunity_type: str = "FUNDED_PHD"
    source: str = ""
    deadline: str = ""
    funding: str = ""
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# Only doctoral-level opportunities pass. Postdocs, faculty, teaching and
# engineering roles are out of scope for a PhD position board.
PHD_TYPE_PATTERNS = [
    "phd", "doctoral", "doctorate", "doktorand", "thesis", "studentship",
    "PhD Candidate", "PhD Researcher", "PhD position",
]

# Hard rejection on the TITLE — an excluded field in the title is definitive.
REJECT_TITLE_PATTERNS = [
    "professor", "professorship", "assistant professor", "associate professor",
    "lecturer", "teaching", "teacher", "postdoc", "postdoctoral", "junior postdoc",
    "research engineer", "research assistant", "researcher on", "engineer",
    "tenure", "chair in", "group leader", "mta", "tenure track",
    "chief", "director", "head of", "coordinator", "manager",
]

# Also exclude whole disciplines when the title says so.
REJECT_FIELDS = {
    "chemistry", "chemical", "materials", "civil engineering", "mechanical engineering",
    "electrical engineering", "pure mathematics", "astrophysics", "plant biology",
    "mining", "petroleum", "architecture", "food technology", "law", "history",
    "philosophy", "social science", "energy engineering", "telecommunication",
}

BIOMECH_KEYWORDS = [
    "biomechanic", "gait", "motor control", "sensorimotor", "kinesiolog", "kinetic",
    "kinematic", "human movement", "locomotion", "postural", "musculoskeletal",
    "joint loading", "ground reaction force", "motion capture", "motion-tracking",
    "wearable", "rehabilitation", "biomarker", "ergonom", "posture", "EMG",
    "electromyograph", "neuroscience", "sensor", "signal processing", "machine learning",
    "deep learning", "neural network", "artificial intelligence", "physiotherapy",
    "clinical movement", "osteoarthritis", "cartilage", "stroke", "movement analysis",
    "behaviour analysis", "behavior analysis", "robotic", "robotics", "3d printed",
    "biomechanical", "soft robotics", "exoskeleton", "prosthe",
]


class BrowserCrawler:
    """Headless browser crawler for JS-heavy job boards."""

    def __init__(self, headless: bool = True, timeout: int = 45000):
        self.headless = headless
        self.timeout = timeout
        self._playwright = None
        self._browser = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        return self

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def crawl_euraxess(
        self, keywords: list[str], countries: list[str] | None = None,
        max_results: int = 40, max_pages: int = 4,
    ) -> list[BrowserPosition]:
        """Crawl EURAXESS job search (Angular SPA, requires JS)."""
        results: list[BrowserPosition] = []
        context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = await context.new_page()
        page.set_default_timeout(self.timeout)

        for kw in keywords:
            url = "https://euraxess.ec.europa.eu/jobs/search"
            try:
                await page.goto(url, wait_until="networkidle", timeout=self.timeout)
                # Dismiss cookie banner
                for sel in ["text=Accept all cookies", "button:has-text('Accept')"]:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.click()
                            await page.wait_for_timeout(800)
                            break
                    except Exception:
                        continue

                # EURAXESS is an SPA: URL params are ignored. Must use the search box.
                # PrimeNG/Angular inputs ignore fill() — keyboard.type() fires real key
                # events so ngModel + autocomplete actually bind, then Enter submits.
                filled = False
                for sel in [
                    'input[placeholder*="Field, subject"]',
                    'input[type="search"]',
                    'input[name="keyword"]',
                ]:
                    try:
                        box = await page.query_selector(sel)
                        if not box:
                            continue
                        await box.click()
                        await page.keyboard.type(kw, delay=70)
                        await page.wait_for_timeout(1200)
                        await page.keyboard.press("Enter")
                        filled = True
                        break
                    except Exception:
                        continue

                if not filled:
                    # Fall back to URL param
                    await page.goto(
                        f"{url}?keyword={kw.replace(' ', '%20')}",
                        wait_until="networkidle", timeout=self.timeout,
                    )

                # Wait for results to render
                try:
                    await page.wait_for_selector("article", timeout=25000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)

                # EURAXESS paginates at 10/page — walk the pager to widen coverage.
                for page_no in range(max_pages):
                    positions = await self._parse_euraxess_page(page)
                    relevant = [p for p in positions if self._is_relevant(p)]
                    for p in relevant:
                        p.source = "euraxess"
                    results.extend(relevant)

                    next_btn = await page.query_selector(
                        'button[aria-label*="Next"], a[aria-label*="Next"], '
                        '[class*="pagination"] button:last-child'
                    )
                    if not next_btn or page_no == max_pages - 1:
                        break
                    try:
                        disabled = await next_btn.get_attribute("disabled")
                        cls = (await next_btn.get_attribute("class") or "")
                        if disabled is not None or "disabled" in cls:
                            break
                        await next_btn.click()
                        await page.wait_for_timeout(2500)
                    except Exception:
                        break

                print(f"  [{kw}] -> filled={filled}, "
                      f"{len([p for p in results if p.source == 'euraxess'])} relevant so far")
            except Exception as e:
                print(f"  [{kw}] -> ERROR: {type(e).__name__}: {e}")
            await page.wait_for_timeout(3000)  # rate limiting

        await context.close()
        return self._deduplicate(results)[:max_results]

    async def _parse_euraxess_page(self, page) -> list[BrowserPosition]:
        """Extract positions from EURAXESS results page DOM."""
        raw = await page.evaluate("""
        () => {
            const out = [];
            document.querySelectorAll('article').forEach(a => {
                const link = a.querySelector('a[href*="/jobs/"]');
                if (!link || link.href.includes('/search')) return;
                const lines = (a.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
                out.push({ url: link.href, lines: lines });
            });
            return out;
        }
        """)

        positions = []
        for item in raw:
            lines = item["lines"]
            org_idx = next((i for i, l in enumerate(lines) if "Posted on:" in l), -1)
            if org_idx < 0:
                continue
            org_date = lines[org_idx]
            org = re.sub(r"Posted on:.*$", "", org_date).strip()
            posted = (re.search(r"Posted on:\s*(.+)", org_date) or [None, ""])[1].strip()
            title = lines[org_idx + 1] if len(lines) > org_idx + 1 else ""
            desc_parts = lines[org_idx + 2: org_idx + 5]

            full_text = "\n".join(lines)
            loc = re.search(r"Number of offers:\s*\d+,\s*([^,]+),\s*([^,\n]+)", full_text)
            country = loc.group(1).strip() if loc else ""
            city = loc.group(2).strip() if loc else ""

            dept = ""
            field = ""
            for l in lines:
                if l.startswith("Department:"):
                    dept = l.replace("Department:", "").strip()
                if l.startswith("Research Field:"):
                    field = l.replace("Research Field:", "").strip()

            positions.append(BrowserPosition(
                title=title,
                organization=org,
                country=country,
                city=city,
                posted=posted,
                url=item["url"],
                research_field=field,
                department=dept,
                description=" ".join(desc_parts)[:400],
            ))
        return positions

    def _is_relevant(self, pos: BrowserPosition) -> bool:
        """Apply hard rejection + PhD-type + relevance rules."""
        title_low = pos.title.lower()
        context = f"{pos.title} {pos.research_field} {pos.department} {pos.description}".lower()

        # 1. Role-type gate: must be doctoral, not postdoc/faculty/teaching/engineering.
        if any(p in title_low for p in REJECT_TITLE_PATTERNS):
            return False

        # 2. Must explicitly be a PhD/doctoral opportunity.
        if not any(p in title_low for p in PHD_TYPE_PATTERNS):
            return False

        # 3. Discipline gate.
        for bad in REJECT_FIELDS:
            if bad in title_low:
                return False

        # 4. Topic relevance: keyword in the title, or >=2 matches in context.
        if any(kw in title_low for kw in BIOMECH_KEYWORDS):
            return True
        return sum(1 for kw in BIOMECH_KEYWORDS if kw in context) >= 2

    def _score_fit(self, pos: BrowserPosition) -> int:
        """0-100 fit against the founder's fingerprint (biomechanics + ML)."""
        title_low = pos.title.lower()
        context = f"{pos.title} {pos.research_field} {pos.description}".lower()
        score = 40
        if any(kw in title_low for kw in BIOMECH_KEYWORDS):
            score += 25
        ml_kw = ["machine learning", "deep learning", "neural network", "artificial intelligence",
                 "deep learning", "ai-based", "predict", "modelling", "modeling", "quantification"]
        if any(kw in context for kw in ml_kw):
            score += 20
        sensor_kw = ["motion capture", "wearable", "sensor", "marker", "inertial", "imu",
                     "force plate", "kinetic", "kinematic", "biomarker"]
        if any(kw in context for kw in sensor_kw):
            score += 15
        return min(score, 100)

    @staticmethod
    def _deduplicate(positions: list[BrowserPosition]) -> list[BrowserPosition]:
        seen = set()
        unique = []
        for p in positions:
            key = p.url or f"{p.title}_{p.organization}"
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    def save(self, positions: list[BrowserPosition], path: str) -> None:
        """Save extracted positions to JSON."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(p) for p in positions]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(data)} positions -> {path}")
