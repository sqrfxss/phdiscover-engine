"""
Crawl the university boards discovered by crawl_universities.py.

Each board is its own site with its own markup, so this reuses the generic
extractor rather than a per-university parser: measure first, write parsers only
for the boards that turn out to need one. Everything found is tagged with the
university it came from, which is what makes a posting verifiable later — a link
on jobs.ethz.ch can be traced to ETH Zurich, a link on a random aggregator
cannot.

Ordering is by research output, taken from the registry's openalex_papers
column: the largest universities publish the most positions, so a truncated run
still finds something worth publishing.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from crawl_v2 import (  # noqa: E402
    PER_PAGE_LIMIT, RETRY, UA, _page_number, harvest, next_page_url,
)

BOARDS = ROOT / "config" / "university_boards.json"
OUT = ROOT / "data" / "university_crawl.json"

# At ~4,000 boards the earlier settings (6 at a time, 0.5s per board, 3 pages)
# would take well over a day. 16 at a time with a 0.15s delay and 2 pages walks
# the registry in about an hour while staying a crawl rather than a load test:
# one request per host per page, sequential per board, 16 boards in flight.
CONCURRENCY = int(os.environ.get("BOARD_CONCURRENCY", "16"))
PER_BOARD_DELAY = float(os.environ.get("BOARD_DELAY", "0.15"))
MAX_PAGES_PER_BOARD = int(os.environ.get("BOARD_PAGES", "2"))
TIMEOUT = 20


def _research_size() -> dict[str, int]:
    """homepage -> openalex_papers, so boards can be ordered by how much they
    are likely to publish. The column is in the registry CSV."""
    import csv
    p = ROOT / "config" / "universities.csv"
    if not p.exists():
        return {}
    out: dict[str, int] = {}
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                url = (r.get("official_url") or "").strip().split("?")[0].rstrip("/")
                if url:
                    out[url] = int(r.get("openalex_papers") or 0)
    except (OSError, ValueError):
        pass
    return out


def load_boards(country: str = "", limit: int = 0) -> list[dict]:
    """
    Distinct boards to crawl.

    Universities that share a board are collapsed onto it: the UK registry points
    ox.ac.uk, cam.ac.uk and imperial.ac.uk all at jobs.ac.uk, and crawling it
    three times would triple the load on someone else's site for no new data.
    """
    if not BOARDS.exists():
        raise SystemExit(f"{BOARDS} not found — run scripts/crawl_universities.py first")
    reg = json.loads(BOARDS.read_text(encoding="utf-8"))
    by_url: dict[str, dict] = {}
    for entry in reg.get("boards", {}).values():
        url = (entry.get("board_url") or "").strip()
        if not url or not entry.get("still_ok", True):
            continue
        rec = by_url.setdefault(url, {
            "board_url": url,
            # Keyed by homepage, which is what the registry CSV indexes; the
            # board URL is a different path and never matches.
            "homepage": (entry.get("homepage") or "").rstrip("/"),
            "country": entry.get("country", ""),
            "stage": entry.get("stage", ""),
            "universities": [],
        })
        rec["universities"].append(entry.get("university") or "")
    size = _research_size()
    for rec in by_url.values():
        rec["_papers"] = size.get(rec.get("homepage") or "", 0)
    # Largest research output first: those universities publish the most
    # positions, so a truncated run still finds something worth publishing.
    out = sorted(by_url.values(), key=lambda b: -b["_papers"])
    if country:
        needle = country.lower()
        out = [b for b in out if needle in (b.get("country") or "").lower()]
    if limit:
        out = out[:limit]
    return out


async def crawl_one(client: httpx.AsyncClient, board: dict) -> dict:
    """Walk up to MAX_PAGES_PER_BOARD listing pages of one board."""
    name = board["universities"][0] if board["universities"] else board["board_url"]
    found: list[dict] = []
    seen: set[str] = set()
    url = board["board_url"]
    visited: set[str] = {url}
    pages = 0
    note = ""

    while url and pages < MAX_PAGES_PER_BOARD:
        try:
            r = await client.get(url)
        except Exception as e:  # noqa: BLE001
            note = f"{type(e).__name__}: {str(e)[:50]}"
            break

        if r.status_code != 200:
            note = f"HTTP {r.status_code}"
            break

        pages += 1
        items = harvest(r.text, url, f"uni_{_slug(name)}", name,
                        board.get("country", ""), "http")
        for it in items:
            key = it.url.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            # The university is what makes this verifiable later, so it travels
            # with the record rather than living only in the registry.
            #
            # Set as a plain dict key rather than an attribute: asdict() only
            # serialises declared dataclass fields, so assigning
            # it.university put the value in memory and dropped it on write.
            item = asdict(it)
            item["university"] = name
            item["universities"] = ", ".join(board["universities"][:4])
            found.append(item)

        nxt = next_page_url(r.text, url)
        if not nxt or nxt in visited:
            break
        visited.add(nxt)
        url = nxt
        await asyncio.sleep(PER_BOARD_DELAY)

    return {
        "board_url": board["board_url"],
        "universities": board["universities"],
        "country": board.get("country", ""),
        "stage": board.get("stage", ""),
        "research_size": board.get("_papers", 0),
        "pages": pages,
        "found": len(found),
        "note": note,
        "positions": found,
    }


async def run(boards: list[dict]) -> dict:
    limits = httpx.Limits(max_connections=CONCURRENCY,
                          max_keepalive_connections=CONCURRENCY)
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []
    done = 0
    total_found = 0

    headers = {"User-Agent": UA,
               "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "en,*;q=0.5"}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True,
                                 timeout=TIMEOUT, limits=limits,
                                 headers=headers) as client:

        async def worker(board: dict) -> None:
            nonlocal done, total_found
            async with sem:
                res = await crawl_one(client, board)
            results.append(res)
            total_found += res["found"]
            done += 1
            if done % 25 == 0 or done == len(boards):
                print(f"  {done}/{len(boards)}  postings={total_found}", flush=True)
                # Checkpoint: an hour of crawling that dies at the end leaves
                # nothing, the same failure the discovery sweep had.
                write_partial(results, total_found)

        await asyncio.gather(*(worker(b) for b in boards))

    results.sort(key=lambda r: -r["found"])
    payload = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "boards_crawled": len(boards),
        "boards_with_data": sum(1 for r in results if r["found"]),
        "postings": total_found,
        "per_board": results,
        "positions": [p for r in results for p in r["positions"]],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # tmp-then-rename, so an interrupted write cannot leave a truncated file
    # that the filter then reads as real data.
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(OUT)
    return payload


def write_partial(results: list[dict], total: int) -> None:
    """Save progress so an interrupted crawl is not lost."""
    payload = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "in_progress": True,
        "boards_crawled": len(results),
        "boards_with_data": sum(1 for r in results if r["found"]),
        "postings": total,
        "per_board": results,
        "positions": [p for r in results for p in r["positions"]],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "unknown").lower()).strip("_")
    return s[:40] or "unknown"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", default="",
                    help="only boards in this country")
    ap.add_argument("--limit", type=int, default=0,
                    help="only crawl the first N boards")
    args = ap.parse_args()

    boards = load_boards(country=args.country, limit=args.limit)
    print(f"=> {len(boards)} distinct boards from the university registry")
    if not boards:
        print("nothing to crawl")
        return 1

    payload = asyncio.run(run(boards))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    print(f"\n  boards crawled : {payload['boards_crawled']}")
    print(f"  with postings  : {payload['boards_with_data']}")
    print(f"  postings found : {payload['postings']}")
    print(f"\n  top boards:")
    for r in payload["per_board"][:10]:
        if r["found"]:
            print(f"    {r['found']:4}  {r['universities'][0][:34]:36} {r['board_url'][:50]}")
    print(f"\n  -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())