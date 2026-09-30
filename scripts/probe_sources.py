"""
Reachability + extraction probe for every source in config/sources.yaml.

Two stages, because plain HTTP and a real browser fail on disjoint sets:
  1. HTTP probe  — fast, cheap, classifies DNS / 403 / 200.
  2. Browser probe — only for sources that need JS or failed HTTP.

Results are written to data/source_health.json and the YAML `status` fields are
left untouched — this script reports, it does not silently rewrite config.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path("F:/hermes/phdiscover-engine")
SOURCES_YAML = ROOT / "config" / "sources.yaml"
OUT_JSON = ROOT / "data" / "source_health.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class Probe:
    name: str
    label: str
    url: str
    declared_status: str
    needs_browser: bool
    tier: str
    source_type: str
    region: str
    http_status: int | None = None
    http_bytes: int = 0
    http_error: str = ""
    http_ms: int = 0
    browser_status: int | None = None
    browser_bytes: int = 0
    browser_error: str = ""
    candidate_links: int = 0
    verdict: str = ""
    verdict_reason: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def load_sources() -> list[dict[str, Any]]:
    with open(SOURCES_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def load_boards() -> list[dict[str, Any]]:
    """
    Only real vacancy boards are worth probing.

    The registry also carries funding schemes (DFG, SNF, NWO, NSERC) and
    institutional homepages. Probing them burns a browser session each run and
    always returns "not a board", which is a fact about the page, not about
    reachability. They are reported as skipped instead.
    """
    return [s for s in load_sources() if s.get("is_board")]


# Words that indicate a listing link, used to judge whether a 200 actually
# contains job postings rather than a cookie wall or an empty shell.
LISTING_HINTS = [
    "/job", "/jobs/", "/vacanc", "/position", "/opening", "/career",
    "/phd", "/doctoral", "/student-position", "/talent", "/werbung",
]


def classify(probe: Probe) -> None:
    """Turn raw observations into a verdict."""
    if probe.http_error and "resolve" in probe.http_error.lower():
        probe.verdict = "dns_blocked"
        probe.verdict_reason = "Domain does not resolve from this network"
        return

    if probe.http_status == 403:
        probe.verdict = "cloudflare_403"
        probe.verdict_reason = "Site healthy, bot blocked — needs Playwright"
        return

    if probe.http_status and probe.http_status >= 500:
        probe.verdict = "server_error"
        probe.verdict_reason = f"Upstream returned {probe.http_status}"
        return

    if probe.http_status == 200:
        if probe.candidate_links >= 3:
            probe.verdict = "crawlable"
            probe.verdict_reason = f"200 OK, {probe.candidate_links} candidate links"
        elif probe.http_bytes > 50_000:
            probe.verdict = "crawlable_spa"
            probe.verdict_reason = "200 OK with content, but no listing links in raw HTML (SPA)"
        else:
            probe.verdict = "thin"
            probe.verdict_reason = f"200 OK but only {probe.http_bytes} bytes — likely a redirect shell"
        return

    if probe.http_status:
        probe.verdict = "redirect_or_error"
        probe.verdict_reason = f"HTTP {probe.http_status}"
        return

    probe.verdict = "failed"
    probe.verdict_reason = probe.http_error or "no response"


async def http_probe(client: httpx.AsyncClient, src: dict[str, Any]) -> Probe:
    p = Probe(
        name=src["name"],
        label=src.get("label", src["name"]),
        url=src.get("search_url") or src["url"],
        declared_status=src.get("status", "unknown"),
        needs_browser=bool(src.get("needs_browser")),
        tier=src.get("tier", ""),
        source_type=src.get("source_type", ""),
        region=src.get("region", ""),
    )
    t0 = time.perf_counter()
    try:
        r = await client.get(p.url)
        p.http_status = r.status_code
        p.http_bytes = len(r.text)
        p.http_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code == 200:
            low = r.text.lower()
            p.candidate_links = sum(low.count(h) for h in LISTING_HINTS)
    except httpx.ConnectError as e:
        p.http_error = f"ConnectError: {e}"
    except httpx.ConnectTimeout:
        p.http_error = "ConnectTimeout"
    except Exception as e:  # noqa: BLE001 — probe must never abort the run
        p.http_error = f"{type(e).__name__}: {e}"
    return p


async def browser_probe(src: dict[str, Any], timeout: int = 30000) -> tuple[int | None, int, str, int]:
    """Open the page in headless Chromium. Returns (status, bytes, error, links)."""
    from playwright.async_api import async_playwright

    url = src.get("search_url") or src["url"]
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = await browser.new_context(user_agent=UA, locale="en-US")
                page = await ctx.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(3000)
                html = await page.content()
                links = await page.evaluate(
                    "() => document.querySelectorAll('a[href]').length"
                )
                status = resp.status if resp else None
                await ctx.close()
                return status, len(html), "", links
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        return None, 0, f"{type(e).__name__}: {e}", 0


async def main() -> None:
    all_sources = load_sources()
    sources = load_boards()
    skipped = [s for s in all_sources if not s.get("is_board")]
    print(f"Registry: {len(all_sources)}  |  probing {len(sources)} job boards  "
          f"|  skipping {len(skipped)} non-boards\n")

    limits = httpx.Limits(max_connections=6, max_keepalive_connections=6)
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, trust_env=False,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        limits=limits,
    ) as client:
        probes = await asyncio.gather(*(http_probe(client, s) for s in sources))

    # Browser stage for anything that failed HTTP but is declared browser-needing,
    # plus anything that returned 200 but looked like an SPA shell.
    needs_browser = [
        p for p in probes
        if p.needs_browser or p.verdict in {"cloudflare_403", "crawlable_spa", "failed"}
    ]
    print(f"HTTP stage done. Browser stage for {len(needs_browser)} sources...\n")

    for p in needs_browser:
        src = next(s for s in sources if s["name"] == p.name)
        status, size, err, links = await browser_probe(src)
        p.browser_status = status
        p.browser_bytes = size
        p.browser_error = err
        if links:
            p.candidate_links = max(p.candidate_links, links)

        if err:
            p.verdict = "browser_failed"
            p.verdict_reason = err[:120]
        elif status == 200 and links >= 5:
            p.verdict = "crawlable_browser"
            p.verdict_reason = f"Browser 200, {links} links"
        elif status == 200:
            p.verdict = "browser_thin"
            p.verdict_reason = f"Browser 200 but only {links} links"
        print(f"  {p.name:24} {p.verdict}")

    for p in probes:
        if p.verdict in {"crawlable", "crawlable_spa"}:
            continue  # browser stage may have upgraded it already
        classify(p)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(
            [asdict(p) for p in probes]
            + [{"name": s["name"], "label": s.get("label", s["name"]),
                "url": s.get("url", ""), "verdict": "not_a_board",
                "verdict_reason": s.get("notes", "")}
               for s in skipped],
            f, ensure_ascii=False, indent=2)

    # ── Report ────────────────────────────────────────────────────
    by_verdict: dict[str, list[Probe]] = {}
    for p in probes:
        by_verdict.setdefault(p.verdict, []).append(p)

    print("\n" + "=" * 78)
    print("SOURCE HEALTH REPORT")
    print("=" * 78)
    for verdict in sorted(by_verdict, key=lambda v: -len(by_verdict[v])):
        group = by_verdict[verdict]
        print(f"\n{verdict.upper()}  ({len(group)})")
        for p in group:
            extra = f"http={p.http_status or '-'} br={p.browser_status or '-'}"
            print(f"  {p.name:26} {extra:22} {p.verdict_reason[:44]}")

    crawlable = [p for p in probes if p.verdict.startswith("crawlable")]
    print("\n" + "=" * 78)
    print(f"CRAWLABLE: {len(crawlable)}/{len(probes)}")
    print(f"  HTTP only : {[p.name for p in crawlable if p.verdict == 'crawlable']}")
    print(f"  Browser   : {[p.name for p in crawlable if p.verdict == 'crawlable_browser']}")
    print(f"  SPA shell : {[p.name for p in crawlable if p.verdict == 'crawlable_spa']}")
    print("=" * 78)
    print(f"\nSaved -> {OUT_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
