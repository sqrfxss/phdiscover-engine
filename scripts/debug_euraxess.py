"""Debug: inspect what EURAXESS actually returns for a biomechanics search."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from phdiscover.crawlers.browser_crawler import BrowserCrawler


async def main():
    async with BrowserCrawler(headless=True) as c:
        ctx = await c._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()
        page.set_default_timeout(45000)
        await page.goto("https://euraxess.ec.europa.eu/jobs/search", wait_until="networkidle")

        for sel in ["text=Accept all cookies", "button:has-text('Accept')"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                continue

        box = await page.query_selector('input[placeholder*="Field, subject"]')
        await box.click()
        # PrimeNG/Angular inputs ignore fill() — type() fires real key events
        # so the framework's autocomplete + ngModel binding actually updates.
        await page.keyboard.type("biomechanics", delay=80)
        await page.wait_for_timeout(1500)  # let autocomplete dropdown settle
        # Press Enter to submit
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(6000)

        # Dump result text
        result_text = await page.evaluate("""
        () => {
            const arts = Array.from(document.querySelectorAll('article'));
            return {
                url: location.href,
                articleCount: arts.length,
                totalText: document.body.innerText.match(/\\d+[\\s,]*(results|jobs|offers)/i)?.[0] || 'n/a',
                firstThree: arts.slice(0,3).map(a => a.innerText.split('\\n').slice(0,4).join(' | ')),
            };
        }
        """)
        print("URL after search:", result_text["url"])
        print("Articles:", result_text["articleCount"])
        print("Total marker:", result_text["totalText"])
        print()
        for i, t in enumerate(result_text["firstThree"], 1):
            print(f"[{i}] {t[:200]}")
            print()

        await ctx.close()


asyncio.run(main())
