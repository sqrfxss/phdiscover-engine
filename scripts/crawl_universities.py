"""
Locate the vacancy board for every university in the registry.

The registry has 5,452 university homepages across 49 countries. Crawling those
directly is wasted work — a homepage lists no positions — so this discovers
where each board actually is and writes the result back, which the daily crawl
then reads instead of re-deriving it.

Results persist between runs: a university whose board was found is not
searched again, and the board is re-checked rather than re-found. That keeps a
one-off 5,000-host sweep from becoming a daily one, while still noticing a board
that moved.

    python scripts/crawl_universities.py --limit 50          # try 50
    python scripts/crawl_universities.py --country Germany   # one country
    python scripts/crawl_universities.py --refresh           # re-find all
    python scripts/crawl_universities.py --stats             # report only
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from crawl_v2 import harvest  # noqa: E402
from phdiscover.universities.discover import (  # noqa: E402
    UA, Finding, find_board,
)

DEFAULT_CSV = Path.home() / "Desktop" / "universities_final.csv"
REGISTRY = ROOT / "config" / "university_boards.json"
REPORT = ROOT / "data" / "university_discovery.json"

# Concurrency and politeness. 6,000 hosts is a lot of traffic for someone else's
# infrastructure, so this stays a crawl rather than a load test. 16 at a time
# with a 0.25s per-host delay finished the sweep in well under two hours; at 8
# and 0.4s it took over five, which is long enough that a run is likely to be
# interrupted before it finishes.
CONCURRENCY = int(os.environ.get("UNIV_CONCURRENCY", "16"))
PER_HOST_DELAY = float(os.environ.get("UNIV_DELAY", "0.25"))


def load_registry_csv(path: Path) -> list[dict]:
    """Read the university CSV, keeping only rows with a usable URL."""
    if not path.exists():
        raise SystemExit(f"registry not found: {path}")
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            url = (r.get("official_url") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                continue
            rows.append({
                "university": (r.get("university") or "").strip(),
                "country": (r.get("country") or "").strip(),
                "country_code": (r.get("country_code") or "").strip(),
                "homepage": url,
                "phase1": (r.get("phase1") or "").strip() == "True",
                "openalex_papers": int(r.get("openalex_papers") or 0),
            })
    return rows


def load_registry() -> dict:
    """Boards found on earlier runs, keyed by homepage."""
    if not REGISTRY.exists():
        return {"version": 1, "boards": {}, "updated_at": ""}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "boards": {}, "updated_at": ""}


def save_registry(reg: dict, checkpoint: bool = False) -> None:
    """
    Write the registry to disk.

    Called every 10 universities during a sweep as well as at the end. The
    sweep runs for hours over 6,000 hosts; writing only at the end means an
    interrupted run — a dropped connection, a restart — leaves nothing behind
    and the whole thing has to start over.
    """
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    reg["boards"] = dedupe_shared_boards(reg.get("boards", {}))
    reg["count"] = len(reg["boards"])
    if checkpoint:
        reg["in_progress"] = True
    else:
        reg["in_progress"] = False
    # Write to a sibling then replace, so a kill mid-write cannot truncate the
    # file and lose the checkpoint it was in the middle of saving.
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(REGISTRY)


def dedupe_shared_boards(boards: dict[str, dict]) -> dict[str, dict]:
    """
    Drop universities whose discovered board is shared with an earlier one.

    The UK registry is full of separate institutions on one domain — ox.ac.uk,
    cam.ac.uk, imperial.ac.uk — and they all resolve to the same national board
    at jobs.ac.uk. Keeping all five means crawling it five times a day. The
    first entry keeps the board and the rest are recorded against it, so the
    count on the site stays accurate while the crawl does the work once.
    """
    by_url: dict[str, str] = {}
    for key, entry in boards.items():
        url = (entry.get("board_url") or "").lower()
        if not url:
            continue
        if url in by_url:
            entry["shared_with"] = boards[by_url[url]]["university"]
            entry["board_url"] = ""
            entry["shared_board"] = url
        else:
            by_url[url] = key
    return boards


def select(rows: list[dict], registry: dict, *, country: str = "",
           limit: int | None, refresh: bool, only_missing: bool) -> list[dict]:
    """Which universities this run will look at."""
    boards = registry.get("boards", {})
    out = rows
    if country:
        needle = country.lower()
        out = [r for r in out
               if needle in r["country"].lower() or needle == r["country_code"].lower()]
    if only_missing and not refresh:
        out = [r for r in out
               if r["homepage"].split("?")[0].rstrip("/") not in boards]
    if limit:
        # Biggest research output first: those universities publish the most
        # positions, so a partial run still finds something useful.
        out = sorted(out, key=lambda r: -r["openalex_papers"])[:limit]
    return out


async def probe_one(client: httpx.AsyncClient, row: dict) -> Finding:
    f = await find_board(client, row["university"], row["homepage"],
                         max_path_tries=PATH_TRIES)
    await asyncio.sleep(PER_HOST_DELAY)
    return f


# How many candidate paths to try before falling through to reading the homepage.
PATH_TRIES = int(os.environ.get("UNIV_PATH_TRIES", "10"))


async def run(targets: list[dict], registry: dict, *, refresh: bool) -> dict:
    boards = registry.setdefault("boards", {})
    limits = httpx.Limits(max_connections=CONCURRENCY,
                          max_keepalive_connections=CONCURRENCY)
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    found = 0

    async with httpx.AsyncClient(trust_env=False, follow_redirects=True,
                                 timeout=25, limits=limits,
                                 headers={"User-Agent": UA,
                                          "Accept": "text/html,application/xhtml+xml",
                                          "Accept-Language": "en,*;q=0.5"}) as client:

        async def worker(row: dict) -> None:
            nonlocal done, found
            key = row["homepage"].split("?")[0].rstrip("/")
            prior = boards.get(key)

            # A board already on file is re-checked, not re-searched: one HEAD
            # against the known URL is far cheaper than 30 candidates.
            if prior and prior.get("board_url") and not refresh:
                try:
                    r = await client.head(prior["board_url"], timeout=15)
                    if r.status_code and r.status_code < 400:
                        prior["http_status"] = r.status_code
                        prior["last_checked"] = datetime.now(timezone.utc).isoformat()
                        prior["still_ok"] = True
                        done += 1
                        return
                except Exception:  # noqa: BLE001
                    pass
                prior["still_ok"] = False
                # fall through and search again

            async with sem:
                f = await probe_one(client, row)
            done += 1
            if f.board_url:
                found += 1
                boards[key] = {
                    "university": row["university"],
                    "country": row["country"],
                    "country_code": row["country_code"],
                    "homepage": row["homepage"],
                    "board_url": f.board_url,
                    "stage": f.stage,
                    "http_status": f.http_status,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "still_ok": True,
                }
            else:
                boards[key] = {
                    "university": row["university"],
                    "country": row["country"],
                    "country_code": row["country_code"],
                    "homepage": row["homepage"],
                    "board_url": "",
                    "stage": "",
                    "error": f.error,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "still_ok": False,
                }
            if done % 10 == 0 or done == len(targets):
                pct = done / len(targets) * 100
                print(f"  {done}/{len(targets)} ({pct:4.1f}%)  found={found}",
                      flush=True)
                # Checkpoint every 10 universities. The full sweep takes hours,
                # and without this an interrupted run leaves nothing on disk —
                # which is exactly what happened the first time it was launched.
                save_registry(registry, checkpoint=True)

        # gather has to live INSIDE the `async with`. Placed after it, every
        # worker raised "Cannot send a request, as the client has been closed"
        # and the run finished with zero boards and a clean exit code.
        await asyncio.gather(*(worker(row) for row in targets))


    return registry


async def verify_boards(boards: list[dict], *, sample: int = 0) -> None:
    """
    Fetch each board and confirm it actually lists vacancies.

    Discovery only proves a URL exists and looks like a careers page. It does
    not prove the page lists anything: measured, uamd.edu.al/misioni-dhe-vizioni
    ("mission and vision") and uda.ad/recerca/escola-internacional both returned
    200 and both list zero positions. Reading the page is the only way to tell a
    board from a page that merely sounds like one.

    Only the sample is fetched by default — verifying all of them costs a second
    full sweep of 6,000 hosts.
    """
    from phdiscover.universities.discover import page_lists_positions

    targets = boards if not sample else boards[:sample]
    checked = listed = 0

    async with httpx.AsyncClient(trust_env=False, follow_redirects=True,
                                 timeout=25,
                                 headers={"User-Agent": UA}) as client:
        for b in targets:
            url = b.get("board_url") or ""
            if not url:
                continue
            checked += 1
            try:
                r = await client.get(url)
            except Exception as e:  # noqa: BLE001
                b["verify_error"] = f"{type(e).__name__}: {str(e)[:50]}"
                continue
            b["verify_status"] = r.status_code
            if r.status_code == 200:
                items = harvest(r.text, str(r.url), "verify", "", "", "http")
                b["verify_postings"] = len(items)
                if items or page_lists_positions(r.text):
                    b["verified"] = True
                    listed += 1
                else:
                    b["verified"] = False
            if checked % 25 == 0 or checked == len(targets):
                print(f"  verified {checked}/{len(targets)}  "
                      f"lists postings: {listed}", flush=True)


def write_report(reg: dict, rows: list[dict]) -> None:
    boards = reg.get("boards", {})
    by_stage: dict[str, int] = {}
    by_country: dict[str, dict] = {}
    shared = 0
    for b in boards.values():
        c = b.get("country", "?")
        slot = by_country.setdefault(c, {"boards": 0, "universities": 0})
        slot["universities"] += 1
        if b.get("shared_board"):
            # Counted under the university that owns the board, so the totals
            # describe distinct boards rather than distinct universities.
            shared += 1
            continue
        if b.get("board_url"):
            by_stage[b.get("stage", "?")] = by_stage.get(b.get("stage", "?"), 0) + 1
            slot["boards"] += 1
    for r in rows:
        by_country.setdefault(r["country"], {"boards": 0, "universities": 0})
        by_country[r["country"]]["universities"] = sum(
            1 for b in boards.values() if b.get("country") == r["country"])

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universities_in_registry": len(rows),
        "boards_found": sum(1 for b in boards.values() if b.get("board_url")),
        "boards_shared": shared,
        "boards_missing": sum(1 for b in boards.values()
                              if not b.get("board_url") and not b.get("shared_board")),
        "by_stage": by_stage,
        "by_country": dict(sorted(by_country.items(),
                                  key=lambda kv: -kv[1]["boards"])),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--country", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true",
                    help="re-search boards that were already found")
    ap.add_argument("--missing-only", action="store_true",
                    help="skip universities whose board is already on file")
    ap.add_argument("--verify", type=int, default=0, metavar="N",
                    help="fetch N boards found so far and confirm they list "
                         "vacancies (0 = skip; costs one fetch per board)")
    ap.add_argument("--stats", action="store_true",
                    help="report what is on file and exit")
    args = ap.parse_args()

    rows = load_registry_csv(args.csv)
    registry = load_registry()

    if args.stats:
        write_report(registry, rows)
        print(f"universities in registry : {len(rows)}")
        print(f"boards on file           : "
              f"{sum(1 for b in registry['boards'].values() if b.get('board_url'))}")
        print(f"still failing            : "
              f"{sum(1 for b in registry['boards'].values() if not b.get('still_ok'))}")
        print(f"report -> {REPORT}")
        return 0

    if args.verify:
        found = [b for b in registry.get("boards", {}).values()
                 if b.get("board_url")]
        print(f"=> verifying {min(args.verify, len(found))} boards")
        asyncio.run(verify_boards(found, sample=args.verify))
        save_registry(registry)
        ok = sum(1 for b in found if b.get("verified"))
        print(f"\n  confirmed listing vacancies: {ok}/{len(found)}")
        return 0

    targets = select(rows, registry, country=args.country,
                     limit=args.limit or None, refresh=args.refresh,
                     only_missing=args.missing_only)
    if not targets:
        print("nothing to do")
        return 0

    print(f"=> {len(targets)} universities, {CONCURRENCY} at a time")
    asyncio.run(run(targets, registry, refresh=args.refresh))
    save_registry(registry)
    write_report(registry, rows)

    ok = sum(1 for b in registry["boards"].values() if b.get("board_url"))
    tried = len(targets)
    print(f"\n  boards on file: {ok}")
    print(f"  registry      -> {REGISTRY}")
    print(f"  report        -> {REPORT}")
    if tried:
        print(f"  hit rate this run: {ok}/{len(registry['boards'])} "
              f"({ok / max(len(registry['boards']), 1) * 100:.1f}% of all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
