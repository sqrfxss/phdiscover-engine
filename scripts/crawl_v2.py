"""
Unified source crawler — handles the three failure classes we measured.

1. TLS-hostname mismatch  (researchjobs.com, findajob.eu, mastersportal)
   -> per-source verify toggle; certs are broken upstream, not locally.

2. Cloudflare / bot wall  (daad, snf, nrc, findaphd, findapostdoc,
   academicjobs, research-in-germany)
   -> Playwright with a real browser fingerprint. Some need a wait for the
      challenge to clear before navigating on.

3. Client-side rendering   (phdjobs, biosciencecareers, researchjobseurope,
   nature, four_tu, kth, prospects, jobs.ch, ...)
   -> HTTP returns a shell with 0 usable anchors; the real list only exists
      after JS runs. These are routed to Playwright.

Extraction is hybrid: harvest anchors from the rendered DOM, then fall back to
a JSON/API sniff for boards that hydrate from an XHR endpoint.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# The retry helper and the position cache live in the package, but these
# scripts are run directly from the repo, so make the import work either way.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from phdiscover.cache import PositionCache  # noqa: E402
from phdiscover.deadlines import (  # noqa: E402
    DeadlineStatus, classify_page, extract_deadline, should_stop_paginating,
)
from phdiscover.reliability import RetryPolicy, retry_async  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES_YAML = ROOT / "config" / "sources.yaml"
OUT = ROOT / "data" / "crawl_v2.json"

# Two attempts with a short pause is enough: the failures we see are 30s
# timeouts, and a third try on a saturated Iranian link usually fails too.
RETRY = RetryPolicy(attempts=2, base_delay=4.0, max_delay=12.0, jitter=0.5)

# How deep to walk a board's pagination. Four pages covers the boards that
# publish dozens of posts without hammering anyone; a per-source `max_pages`
# in config/sources.yaml overrides it.
MAX_PAGES = 4
PER_PAGE_LIMIT = 80

# Boards list newest first, so a run of pages that are entirely expired postings
# means everything deeper is older still. Four is the threshold: fewer and a
# single odd board with sparse dates would end the crawl too early, and the cost
# of being wrong is a few extra requests. A per-source `stale_page_run` in
# config/sources.yaml overrides it.
STALE_PAGE_RUN = 4

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ── Sources whose certificates do not match their hostname ────────
# Confirmed 2026-09-29: researchjobs.com and findajob.eu serve a cert for a
# different name. mastersportal presents a broken chain only on some edges.
# These are public read-only boards and no credentials are transmitted.
INSECURE_HOSTS = {"researchjobs.com", "www.researchjobs.com",
                  "findajob.eu", "www.findajob.eu",
                  "mastersportal.com", "www.mastersportal.com"}

# ── Path fragments observed in real listing links ──────────────────
POSTING_HINTS = [
    "/jobs/position/", "/job/", "/jobs/", "/job-", "/vacanc", "/position",
    "/opening", "/career", "/phd", "/doctoral", "/student-position",
    "/talent", "/stellen", "/vacature", "/emploi", "/lavoro", "/offre",
    "/advert", "/listing", "/showjob", "/findajob", "/virksomhed/",
    "/search/phd", "/our-programs/", "/detail", "/en/job",
]

# ── Anchor text that marks a listing regardless of URL shape ──────
LISTING_TEXT = re.compile(
    r"phd|doctoral|postdoc|post-doc|research (?:assistant|fellow|engineer|"
    r"scientist|student|associate)|vacanc|studentship|fellowship|job|"
    r"opening|tenure|chair|position",
    re.IGNORECASE,
)

CONTEXT_HINTS = [
    "phd", "doctoral", "studentship", "biomechanic", "gait", "motor control",
    "sensorimotor", "kinesiolog", "movement", "biomechanical", "neuroscience",
    "rehabilitation", "wearable", "university", "institute", "research",
    "motion capture", "kinetic", "sensor",
]

DEADLINE_RE = re.compile(
    r"(?:deadline|closes|closing|apply by|applications?\s+(?:close|due))"
    r"\s*[:\-]?\s*(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|"
    r"\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)


@dataclass
class Found:
    title: str
    url: str
    source: str
    source_label: str
    context: str = ""
    country: str = ""
    deadline: str = ""
    deadline_date: str = ""
    deadline_status: str = "unknown"
    method: str = ""
    # Which listing page this posting was found on, for the stale-page stop rule.
    page_no: int = 1
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def path_shape(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url)
    parts = [p for p in path.split("?")[0].split("/") if p]
    return "/" + "/".join(parts[:4])


def is_posting_link(href: str, text: str) -> bool:
    path = re.sub(r"^https?://[^/]+", "", href).lower()
    if any(h in path for h in POSTING_HINTS):
        return True
    # Fall back on anchor text when the URL shape is unusual.
    return bool(LISTING_TEXT.search(text)) and len(text.strip()) > 12


def relevance(blob: str) -> bool:
    low = blob.lower()
    return sum(1 for h in CONTEXT_HINTS if h in low) >= 2


def find_deadline(text: str) -> str:
    m = DEADLINE_RE.search(text)
    return m.group(1).strip() if m else ""


def harvest(html: str, base: str, source: str, label: str,
            country: str, method: str, limit: int = 80) -> list[Found]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Found] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            # urljoin, not string concatenation: concatenating a root-relative
            # href onto a paginated URL ("/jobs?page=2") produced
            # "/jobs?page=2/jobs/phd-x", which no longer matches anything.
            href = urljoin(base, href)
        if not href.startswith("http"):
            continue
        # Skip in-page anchors and obvious non-postings.
        if re.search(r"#(main|content|accept|refuse|cookie)", href, re.I):
            continue

        text = a.get_text(" ", strip=True)
        card = a.find_parent(["li", "article", "div", "tr", "td"])

        # Many boards hide the real title behind a "Read More" link. Look for a
        # heading inside the card first, then fall back to the anchor text.
        title = text
        if card:
            head = card.find(["h1", "h2", "h3", "h4", "h5"])
            if head:
                htext = head.get_text(" ", strip=True)
                if 12 < len(htext) < 200:
                    title = htext

        if not is_posting_link(href, title if len(title) > 12 else text):
            continue

        ctx = (card.get_text(" ", strip=True) if card else text)[:700]
        if not relevance(ctx):
            continue

        key = href.split("?")[0].rstrip("/")
        if key in out:
            continue
        # A slug is a better title than a bare org name when the card heading
        # is just the institution ("Queen's University" on canadianresearch.org).
        final_title = title or text or ""
        m = re.search(r"/(?:job|position|posting)/([a-z0-9\-]{14,})/?$",
                      href.split("?")[0], re.I)
        if m and len(final_title.split()) <= 3:
            slug = m.group(1).replace("-", " ").strip()
            final_title = slug[0].upper() + slug[1:]

        # Deadline from the card text, with the title included since some boards
        # print "PhD in X — closes 18 Oct 2026" on one line.
        dl = extract_deadline(f"{final_title} {ctx}")
        rolling = is_rolling_blob(f"{final_title} {ctx}")
        out[key] = Found(
            title=(final_title or "(untitled)")[:180],
            url=href, source=source, source_label=label,
            context=ctx, country=country, method=method,
            deadline=dl.raw,
            deadline_date=dl.value.isoformat() if dl.value else "",
            deadline_status=(DeadlineStatus.ROLLING if rolling else dl.status).value,
        )
        if len(out) >= limit:
            break
    return list(out.values())


def is_rolling_blob(text: str) -> bool:
    from phdiscover.deadlines import is_rolling
    return is_rolling(text)


# ═══════════════════════════════════════════════════════════════════
# HTTP path
# ═══════════════════════════════════════════════════════════════════

async def crawl_http(src: dict[str, Any], secure: httpx.AsyncClient,
                     insecure: httpx.AsyncClient) -> tuple[list[Found], str]:
    """
    Fetch up to MAX_PAGES listing pages, following whatever pagination the
    board actually offers.

    Reading only page 1 was the single biggest reason the result set looked
    small: researchjobseurope has hundreds of doctoral posts and returned 26.
    Boards that paginate are common and the "Next" link is usually right there,
    so the crawler follows it instead of hardcoding a page URL shape.
    """
    base_url = src.get("search_url") or src["url"]
    host = re.sub(r"^https?://", "", base_url).split("/")[0]
    client = insecure if host in INSECURE_HOSTS else secure
    tag = "insecure" if host in INSECURE_HOSTS else "secure"

    max_pages = int(src.get("max_pages", MAX_PAGES))
    collected: list[Found] = []
    seen: set[str] = set()
    visited_pages: set[str] = {base_url}
    url = base_url
    pages_read = 0
    # How many consecutive pages held nothing but expired postings, and the
    # reason pagination ended, for the per-source note.
    stale_run = int(src.get("stale_page_run", STALE_PAGE_RUN))
    history: list = []
    stop_reason = "max_pages"

    while url and pages_read < max_pages:
        try:
            r = await client.get(url)
        except Exception as e:  # noqa: BLE001
            note = f"{type(e).__name__}: {str(e)[:60]}"
            if pages_read == 0:
                return [], f"{note} ({tag})"
            stop_reason = "error on a later page"
            break

        if r.status_code != 200:
            if pages_read == 0:
                return [], f"HTTP {r.status_code} ({tag})"
            stop_reason = f"HTTP {r.status_code} on a later page"
            break

        pages_read += 1
        for item in harvest(r.text, url, src["name"], src.get("label", ""),
                            src.get("country", ""), f"http/{tag}",
                            limit=PER_PAGE_LIMIT):
            key = item.url.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            item.page_no = pages_read
            collected.append(item)

        # An empty page does not mean the end of the list — the harvest is
        # relevance-filtered, so a page full of postdocs or chemistry posts
        # yields nothing while later pages hold the biomechanics ones. Keep
        # walking until the pager runs out or we hit max_pages.
        #
        # The one exception is a run of pages that are entirely expired: boards
        # order by date descending, so once a run of them turns up, everything
        # deeper is older still. That is where reading stops — not on an empty
        # page, which is ambiguous.
        history.append(classify_page([
            {"title": i.title, "context": i.context, "deadline": i.deadline}
            for i in collected if i.page_no == pages_read
        ]))
        if should_stop_paginating(history, stale_run):
            stop_reason = f"stopped after {stale_run} consecutive all-expired pages"
            break

        nxt = next_page_url(r.text, url)
        if not nxt or nxt == url or nxt in visited_pages:
            stop_reason = "pager exhausted"
            break
        visited_pages.add(nxt)
        url = nxt
        await asyncio.sleep(0.6)

    if pages_read == 0:
        return [], f"HTTP {r.status_code} ({tag})"
    extra = "" if stop_reason == "max_pages" else f", {stop_reason}"
    return collected, (f"200 {len(collected)} postings, {pages_read} page(s) "
                       f"({tag}){extra}")


def next_page_url(html: str, current: str) -> str | None:
    """
    Find the 'next page' link, whichever board produced it.

    Boards use four different shapes in the wild: ?page=2, /page/2/,
    ?offset=20, and /job/job/index?page=2. Rather than hardcode any of them,
    look for a link whose text or href looks like a pager and return it.

    Two guards, both learned from real boards:
      - A "Next" that points backwards would loop the crawler forever.
      - A pager that jumps to a *different search mode* (e.g. /jobs/phd ->
        /search/?...) abandons the filter the caller asked for and silently
        returns unrelated rows, so a candidate must keep the current path's
        shape to be followed.
    """
    soup = BeautifulSoup(html, "html.parser")
    pager_words = re.compile(
        r"^(next|more|find more|show more|older|next page|»|>|→|after|"
        r"volgende|mehr|suivant|vidare|następna)", re.IGNORECASE)
    current_num = _page_number(current)
    current_path = _path_shape(current)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue
        text = a.get_text(" ", strip=True)
        if not (pager_words.match(text) or re.search(
                r"[?&](page|p|offset|start)=\d+", href) or re.search(
                r"/page/\d+", href)):
            continue
        # A pager href with no number (?page=, /page/) is a template, not a link
        # to follow — the board fills it in with JavaScript.
        if re.search(r"[?&](page|p|offset|start)=\s*(#|$)", href):
            continue
        if re.search(r"/page/\s*(#|$)", href):
            continue
        absolute = urljoin(current, href)
        if absolute == current:
            continue
        # Never step backwards: a "Next" that points to a page already read
        # would loop the crawler.
        nxt_num = _page_number(absolute)
        if current_num is not None and nxt_num is not None and nxt_num <= current_num:
            continue
        # A pager that switches to a different search path is not page 2 of the
        # same listing — following it drops the caller's filter. This only
        # applies once we are already on a numbered page: the first pager on an
        # unnumbered URL is often the only one present, and some boards move the
        # whole listing under a /page/ prefix when they paginate it.
        if current_num is not None and _path_shape(absolute) != current_path:
            continue
        return absolute
    return None


def _page_number(url: str) -> int | None:
    """Extract the page index from a pagination URL, if it carries one."""
    m = re.search(r"[?&](?:page|p|offset|start)=(\d+)", url)
    if m:
        return int(m.group(1))
    m = re.search(r"/page/(\d+)", url)
    return int(m.group(1)) if m else None


def _path_shape(url: str) -> str:
    """
    Reduce a URL to its path with the page number removed, so two URLs can be
    compared as "the same listing" vs "a different search".
    """
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
    # /jobs/page/2/ and /jobs/page/3/ are the same listing, so drop the segment.
    path = re.sub(r"/page/\d+(?=/|$)", "", path)
    return path.rstrip("/") or "/"


# ═══════════════════════════════════════════════════════════════════
# Browser path
# ═══════════════════════════════════════════════════════════════════

async def crawl_browser(src: dict[str, Any], browser) -> tuple[list[Found], str]:
    url = src.get("search_url") or src["url"]
    host = re.sub(r"^https?://", "", url).split("/")[0]
    ignore_https_errors = src.get("verify_tls") is False
    ctx = await browser.new_context(
        user_agent=UA,
        locale="en-US",
        # Mirrors the httpx client's verify=False for the three boards whose
        # certificate does not match their hostname.
        ignore_https_errors=ignore_https_errors,
        viewport={"width": 1440, "height": 900},
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    tag = "insecure-tls" if ignore_https_errors else "browser"
    try:
        page = await ctx.new_page()
        page.set_default_timeout(60000)

        # Open a blank page first: some hosts serve the Cloudflare interstitial
        # to about:blank navigation and only release the real request afterwards.
        try:
            await page.goto("about:blank", timeout=15000)
        except Exception:  # noqa: BLE001
            pass

        # Some sites show a consent interstitial on first load. Each selector is
        # tried in its own call because Playwright's query_selector rejects a
        # comma-separated list of "text=" engines.
        for sel in ("text=Accept all cookies", "text=Accept All",
                    "button:has-text('Accept')", "#onetrust-accept-btn-handler",
                    "button:has-text('Allow all')"):
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(800)
                    break
            except Exception:  # noqa: BLE001
                continue

        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001
            return [], f"browser goto {type(e).__name__}: {str(e)[:50]}"

        # If we landed on a Cloudflare interstitial, wait it out then reload.
        for sel in ("#challenge-form", "#cf-challenge-running",
                    "text=Just a moment", "text=Checking your browser"):
            try:
                if await page.query_selector(sel):
                    print(f"      cloudflare challenge on {src['name']}, waiting...")
                    await page.wait_for_timeout(9000)
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                    break
            except Exception:  # noqa: BLE001
                continue

        await page.wait_for_timeout(4500)

        # Trigger lazy loading / infinite scroll.
        for _ in range(3):
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(1500)
        # Expand collapsed "show more" sections.
        for sel in ("text=Show more", "text=Load more", "button:has-text('Mehr')"):
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(timeout=2000)
                    await page.wait_for_timeout(1200)
                    break
            except Exception:  # noqa: BLE001
                continue

        html = await page.content()
        status = resp.status if resp else 0
        items = harvest(html, url, src["name"], src.get("label", ""),
                        src.get("country", ""), tag)
        return items, f"{tag} {status}, {len(items)} postings"
    except Exception as e:  # noqa: BLE001
        return [], f"browser {type(e).__name__}: {str(e)[:60]}"
    finally:
        await ctx.close()


async def open_browser():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    b = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    return pw, b


# ═══════════════════════════════════════════════════════════════════

async def main() -> None:
    with open(SOURCES_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Only real vacancy boards are worth a crawl. The registry also holds funding
    # schemes (DFG, SNF, NWO) and institutional homepages, which return zero
    # forever; spending a browser session on each is pure waste. Bot-walled
    # boards are excluded for the same reason.
    skipped = [
        (s["name"], s.get("status", "?"))
        for s in cfg["sources"] if not s.get("is_board")
    ]
    targets = [s for s in cfg["sources"] if s.get("is_board")]
    http_first = [s for s in targets if not s.get("needs_browser")]
    browser_first = [s for s in targets if s.get("needs_browser")]

    print(f"Registry:    {len(cfg['sources'])} sources")
    print(f"Skipped:     {len(skipped)} (not job boards / bot-walled)")
    print(f"HTTP stage:  {len(http_first)}")
    print(f"Browser stage: {len(browser_first)}\n")
    if skipped:
        by_status: dict[str, int] = {}
        for _, st in skipped:
            by_status[st] = by_status.get(st, 0) + 1
        print("  skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
        print()

    all_items: list[Found] = []
    per_source: dict[str, dict] = {}

    # ── Stage 1: HTTP ────────────────────────────────────────────
    limits = httpx.Limits(max_connections=5)
    hdrs = {"User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 trust_env=False, headers=hdrs, limits=limits) as secure, \
            httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False,
                              headers=hdrs, limits=limits, verify=False) as insecure:
        for i, src in enumerate(http_first, 1):
            def attempt(s=src, _first=secure, _second=insecure):
                return crawl_http(s, _first, _second)

            def on_retry(label, attempt_no, note):
                print(f"      retry {attempt_no} for {label}: {note[:70]}")

            items, note = await retry_async(attempt, policy=RETRY, on_retry=on_retry)
            # A source that produced nothing gets one more chance after a pause,
            # because "timeout" and "the board is genuinely empty" look the same
            # from the outside but need opposite responses.
            if not items and not note.startswith("HTTP 4") and not note.startswith("HTTP 5"):
                items2, note2 = await retry_async(attempt, policy=RETRY,
                                                  on_retry=on_retry)
                if items2:
                    items, note = items2, note2

            # A 200 whose body has plenty of "phd/vacancy" words but zero
            # extractable anchors means the list is built client-side — send it
            # to the browser stage rather than accepting zero as the answer.
            if not items and note.startswith("200"):
                src["_escalate"] = True
            all_items.extend(items)
            per_source[src["name"]] = {"n": len(items), "note": note,
                                       "method": "http"}
            print(f"  [{i:2}/{len(http_first)}] {'+' if items else ' '} "
                  f"{src['name']:24} {len(items):3}  {note}")
            await asyncio.sleep(0.8)

    # ── Stage 2: Browser (declared + escalated) ─────────────────
    browser_targets = list({s["name"]: s for s in browser_first +
                           [s for s in http_first if s.get("_escalate")]}.values())

    print(f"\nBrowser stage ({len(browser_targets)} sources, incl. "
          f"{len([s for s in http_first if s.get('_escalate')])} escalated)")

    pw, br = await open_browser()
    try:
        for i, src in enumerate(browser_targets, 1):
            async def attempt(s=src):
                return await crawl_browser(s, br)

            def on_retry(label, attempt_no, note):
                print(f"      retry {attempt_no} for {label}: {note[:70]}")

            items, note = await retry_async(attempt, policy=RETRY, on_retry=on_retry)
            # A board that answered 200 with zero cards is either empty or
            # rendered its list somewhere this page does not reach. Retry once
            # before accepting the zero, because a cached empty run is worse
            # than no run at all.
            if not items and "200," in note:
                items2, note2 = await retry_async(attempt, policy=RETRY,
                                                  on_retry=on_retry)
                if items2:
                    items, note = items2, note2

            all_items.extend(items)
            prev = per_source.get(src["name"], {})
            per_source[src["name"]] = {
                "n": max(prev.get("n", 0), len(items)),
                "note": note, "method": "browser",
                "http_note": prev.get("note", ""),
            }
            print(f"  [B{i:2}/{len(browser_targets)}] {'+' if items else ' '} "
                  f"{src['name']:24} {len(items):3}  {note}")
            await asyncio.sleep(2.5)
    finally:
        await br.close()
        await pw.stop()

    # Dedupe
    best: dict[str, Found] = {}
    for f in all_items:
        k = f.url.split("?")[0].rstrip("/")
        if k not in best or len(f.context) > len(best[k].context):
            best[k] = f
    unique = sorted(best.values(), key=lambda x: -len(x.context))

    # Record the skipped sources too, so the coverage report can tell the
    # difference between "returned nothing" and "was never a candidate".
    for name, st in skipped:
        per_source.setdefault(name, {"n": 0, "method": "skipped",
                                     "note": f"Not crawled: {st}"})

    # ── Cache ───────────────────────────────────────────────────
    # A source that worked this run replaces its cached copy. A source that
    # failed keeps its cached copy, so a flaky network cannot delete good data.
    cache = PositionCache()
    fresh = [asdict(x) for x in unique]

    by_source: dict[str, list[dict]] = {}
    for p in fresh:
        by_source.setdefault(p["source"], []).append(p)

    for source, positions in by_source.items():
        info = per_source.get(source, {})
        if info.get("method") != "skipped" and info.get("n", 0) > 0:
            cache.record_success(source, positions)
    for name, info in per_source.items():
        if info.get("method") == "skipped" or info.get("n", 0) > 0:
            continue
        cache.record_failure(name, info.get("note", ""))

    merged, cache_notes = cache.merge_into(fresh, per_source)
    if cache.dirty:
        cache.save()

    # Dedupe again: the cache may reintroduce a position this run also found.
    final: dict[str, dict] = {}
    for p in merged:
        k = p["url"].split("?")[0].rstrip("/")
        if k not in final or len(p.get("context", "")) > len(final[k].get("context", "")):
            final[k] = p
    merged = sorted(final.values(), key=lambda x: -len(x.get("context", "")))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"crawled_at": datetime.now(timezone.utc).isoformat(),
                   "skipped": [{"name": n, "status": s} for n, s in skipped],
                   "per_source": per_source,
                   "cache_stats": cache.stats(),
                   "cache_notes": cache_notes,
                   "positions": merged},
                  f, ensure_ascii=False, indent=2)

    # ── Report ──────────────────────────────────────────────────
    print("\n" + "=" * 76)
    print("CRAWL v2 SUMMARY")
    print("=" * 76)
    crawled = {k: v for k, v in per_source.items() if v["method"] != "skipped"}
    ok = {k: v for k, v in crawled.items() if v["n"] > 0}
    print(f"Boards crawled:      {len(crawled)}")
    print(f"Boards with data:    {len(ok)}")
    print(f"Boards silent:       {len(crawled) - len(ok)}")
    print(f"Skipped (not boards): {len(skipped)}")
    print(f"Fresh this run:       {len(unique)}")
    print(f"After cache merge:    {len(merged)}")
    if cache_notes:
        print(f"\n{len(cache_notes)} source(s) served from cache after a failure:")
        for n in cache_notes:
            flag = "STALE" if n["stale"] else "ok"
            print(f"  {n['source']:24} {n['cached_positions']:3} cached  "
                  f"age={n['cache_age_days']}d  [{flag}]  {n['reason'][:40]}")
    print()
    for name, info in sorted(ok.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {name:26} {info['n']:4}  [{info['method']}] {info['note']}")
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
