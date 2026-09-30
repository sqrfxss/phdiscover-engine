"""
Assert the published data is internally consistent before it ships.

This runs in CI and again after every crawl, because the failure it catches is
silent: an expired posting on the site, a record with no URL, or a config that
drifted from what the code expects. Nothing else would notice any of them.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config" / "sources.yaml"
SITE = ROOT / "web" / "data" / "ranked_opportunities.json"

# A token that mixes letters and digits with no vowels cannot be part of a
# title; boards append one and it reads as noise to a reader.
OPAQUE_TAIL = re.compile(r"(?<=\s)(?=[\w]*\d)(?=[\w]*[a-z])(?=[a-z0-9]{8,20}$)\S+$", re.I)

# Shapes that would mean a secret reached the published JSON.
SECRETISH = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bre_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        failures.append(msg)


def main() -> int:
    print("registry")
    cfg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    sources = cfg.get("sources", [])
    boards = [s for s in sources if s.get("is_board")]
    names = [s["name"] for s in sources]

    check(len(sources) > 0, f"{len(sources)} sources registered")
    check(len(names) == len(set(names)), "source names are unique")
    check(len(boards) > 0, f"{len(boards)} of them are job boards")

    missing = [s["name"] for s in boards
               if not (s.get("search_url") or s.get("url"))]
    check(not missing, f"every board has a URL (missing: {missing or 'none'})")

    # A board with max_pages but no stale_page_run would crawl past the
    # end of a stale listing, so the two must move together.
    mismatch = [s["name"] for s in boards
                if "max_pages" in s and "stale_page_run" not in s]
    check(not mismatch,
          f"boards with max_pages also set stale_page_run (offenders: {mismatch or 'none'})")

    print("\npublished data")
    site = json.loads(SITE.read_text(encoding="utf-8"))
    check(isinstance(site, list), "site data is a list")
    check(len(site) > 0, f"{len(site)} positions published")

    no_url = [p.get("id") for p in site if not p.get("url")]
    check(not no_url, f"every position has a URL (missing: {no_url or 'none'})")

    no_ev = [p.get("id") for p in site if not p.get("evidence")]
    check(not no_ev, f"every position carries evidence (missing: {no_ev or 'none'})")

    no_fit = [p.get("id") for p in site
              if not isinstance(p.get("research_fit_score"), (int, float))]
    check(not no_fit, f"every position has a fit score (missing: {no_fit or 'none'})")

    expired = [p["title"][:44] for p in site
               if p.get("deadline_status") == "expired"]
    check(not expired, f"no expired posting is published (leaked: {expired or 'none'})")

    urls = [p["url"].split("?")[0].rstrip("/") for p in site if p.get("url")]
    dupes = len(urls) - len(set(urls))
    check(dupes == 0, f"no duplicate URLs ({dupes} dupes)")

    # open status must carry a date, or the site's day counter cannot work.
    open_no_date = [p["id"] for p in site
                    if p.get("deadline_status") == "open" and not p.get("deadline_date")]
    check(not open_no_date,
          f"open positions carry a date (missing: {open_no_date or 'none'})")

    statuses = {p.get("deadline_status") for p in site}
    unknown = statuses - {"open", "rolling", "unknown"}
    check(not unknown, f"no unexpected deadline_status values ({unknown or 'none'})")

    print("\nno secrets in the published files")
    for f in (ROOT / "web" / "data").glob("*.json"):
        txt = f.read_text(encoding="utf-8", errors="replace")
        hits = [p.pattern for p in SECRETISH if p.search(txt)]
        check(not hits, f"{f.name} is clean (matched: {hits or 'none'})")

    print(f"\n{'=' * 62}")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
