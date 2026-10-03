"""
Why do 93% of the discovered boards return nothing?

The 3,481-board crawl returned postings from 239 boards. 3,026 of the silent
ones answered HTTP 200 — so the reachability probe, which only asks "does this
URL answer", cannot tell a working board from an empty one. This samples the
silent ones and reports what their HTML actually contains, because the
difference between "no vacancies right now" and "this page was never a list" is
the whole question.

Read the output before changing any selector: a board that serves 200 with no
vacancy words in the HTML is either empty or JavaScript-rendered, and those
need different fixes.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402

from crawl_v2 import UA  # noqa: E402

CRAWL = ROOT / "data" / "university_crawl.json"
SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 40

# Words that only appear on a page carrying vacancies.
VACANCY_WORDS = ("phd", "doctoral", "postdoc", "vacanc", "position",
                 "opening", "career", "jobdetail", "research position")


async def probe(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                url: str) -> dict:
    async with sem:
        try:
            r = await client.get(url, timeout=25)
        except Exception as e:  # noqa: BLE001
            return {"url": url, "error": type(e).__name__}
    body = r.text.lower()
    return {
        "url": url,
        "status": r.status_code,
        "bytes": len(r.text),
        "anchors": r.text.count("<a "),
        "vacancy_words": sum(body.count(w) for w in VACANCY_WORDS),
        "distinct_words": sum(1 for w in VACANCY_WORDS if w in body),
        "looks_rendered": "<div id=\"root\"" in body
                          or "__next" in body or "ng-app" in body,
        "error": "",
    }


async def main() -> int:
    data = json.loads(CRAWL.read_text(encoding="utf-8"))
    silent = [r for r in data["per_board"]
              if r["found"] == 0 and not r.get("note")]
    if not silent:
        print("no silent boards")
        return 0

    # Largest research output first: if these are empty, the sample is
    # representative of what matters.
    silent.sort(key=lambda r: -r.get("research_size", 0))
    picks = silent[:SAMPLE]
    print(f"{len(silent)} silent boards, sampling the {len(picks)} largest")

    sem = asyncio.Semaphore(10)
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True,
                                 timeout=25, limits=limits,
                                 headers={"User-Agent": UA}) as client:
        rows = await asyncio.gather(*(probe(client, sem, r["board_url"])
                                      for r in picks))

    ok = [r for r in rows if not r["error"]]
    with_words = [r for r in ok if r["vacancy_words"] > 0]
    js = [r for r in ok if r["looks_rendered"]]

    print(f"\n  answered            : {len(ok)}/{len(rows)}")
    print(f"  carry vacancy words : {len(with_words)}/{len(ok)}")
    print(f"  look JS-rendered    : {len(js)}/{len(ok)}")

    print("\n=== boards that DO carry vacancy words but returned none ===")
    shown = 0
    for r in with_words:
        if r["vacancy_words"] < 3:
            continue
        shown += 1
        if shown <= 15:
            print(f"  {r['distinct_words']}w {r['vacancy_words']:4}x  "
                  f"{r['bytes']:>8,}b  {r['url'][:56]}")
    print(f"  ({shown} such boards)")

    print("\n=== boards with no vacancy words at all ===")
    empty = [r for r in ok if r["vacancy_words"] == 0]
    print(f"  {len(empty)}/{len(ok)}")
    for r in empty[:10]:
        tag = " [JS]" if r["looks_rendered"] else ""
        print(f"  {r['bytes']:>8,}b  {r['anchors']:>4}a{tag}  {r['url'][:56]}")

    out = ROOT / "data" / "silent_boards.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n  -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))