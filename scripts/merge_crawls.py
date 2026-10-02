"""
Merge crawl artefacts from different runs into one dataset.

Two runs see different things. The GitHub runner reaches boards that time out
from Iran; a local run from Iran walks more pages on the boards it can reach.
Measured on two consecutive runs of the same code: 415 postings from GitHub
against 368 from Iran, with 8 positions — including five EURAXESS ones — found
only by the local run. Merging keeps both.

Merge policy, per source:
  - union the postings, keyed on the canonical URL
  - prefer the copy with the longer context: a card from one run may carry a
    deadline the other missed
  - carry the newer `discovered_at`

Usage:
    python scripts/merge_crawls.py                     # data/ + a sibling file
    python scripts/merge_crawls.py a.json b.json -o out.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Fields that describe one posting. Anything else in the file (per_source,
# cache_stats, ...) is rebuilt rather than merged.
POSTING_KEYS = ("url", "title", "context", "country", "deadline",
                "deadline_date", "deadline_status", "method", "posted",
                "page_no", "discovered_at", "source", "source_label")


def canonical(url: str) -> str:
    """Key a posting by its URL, ignoring query strings and trailing slashes."""
    return (url or "").split("?")[0].split("#")[0].rstrip("/")


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("positions", data) if isinstance(data, dict) else data
    return {"path": path, "positions": [p for p in rows if isinstance(p, dict)
                                        and p.get("url")],
            "per_source": (data.get("per_source", {}) if isinstance(data, dict)
                           else {})}


def merge_postings(runs: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for run in runs:
        for p in run["positions"]:
            key = canonical(p["url"])
            if not key:
                continue
            cur = best.get(key)
            if cur is None:
                best[key] = {k: p.get(k, "") for k in POSTING_KEYS}
                continue
            # Keep whichever copy carries more information. A deadline found by
            # one run and missed by the other must not be lost.
            for field in POSTING_KEYS:
                new = p.get(field, "")
                if len(str(new or "")) > len(str(cur.get(field) or "")):
                    cur[field] = new
    return list(best.values())


def merge_per_source(runs: list[dict], merged: list[dict]) -> dict:
    """
    Rebuild the per-source summary so it matches the merged postings.

    Taking the max count from each run would overstate coverage: it assumes two
    runs saw disjoint sets, and they mostly do not. Counting the merged rows
    gives a number that matches what the next stage will actually read.
    """
    by_source: dict[str, list[dict]] = {}
    for p in merged:
        by_source.setdefault(p.get("source") or "unknown", []).append(p)

    out: dict[str, dict] = {}
    for run in runs:
        for name, info in (run["per_source"] or {}).items():
            cur = out.setdefault(name, dict(info))
            # Keep the method and note from whichever run actually saw rows;
            # a failure note from the other run would mislabel a live source.
            if by_source.get(name):
                other = next((r["per_source"].get(name, {}) for r in runs
                              if r["per_source"].get(name, {}).get("n", 0) > 0), {})
                for k in ("method", "note"):
                    if other.get(k):
                        cur[k] = other[k]
                cur["n"] = len(by_source[name])
                cur.setdefault("status", info.get("status", ""))
                cur["merged_from"] = sorted({
                    r["path"].name for r in runs
                    if r["per_source"].get(name, {}).get("n", 0) > 0
                })
    return out


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    out_path = None
    for i, a in enumerate(argv):
        if a == "-o":
            out_path = argv[i + 1]

    paths = ([Path(a) for a in args] if args else
             sorted(ROOT.glob("data/crawl_*.json")))
    paths = [p for p in paths if p.exists()]
    if len(paths) < 1:
        print("no crawl artefacts found", file=sys.stderr)
        return 1

    runs = [load(p) for p in paths]
    for r in runs:
        print(f"  {r['path'].name:28} {len(r['positions']):5} postings")

    merged = merge_postings(runs)
    per_source = merge_per_source(runs, merged)

    payload = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "merged_from": [str(p.relative_to(ROOT)) if ROOT in p.parents else str(p)
                        for p in paths],
        "per_source": per_source,
        "positions": merged,
    }
    dest = Path(out_path) if out_path else ROOT / "data" / "crawl_merged.json"
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")

    boards = sum(1 for v in per_source.values() if v.get("n", 0) > 0)
    print(f"\n  merged: {len(merged)} postings from {boards} boards")
    print(f"  -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
