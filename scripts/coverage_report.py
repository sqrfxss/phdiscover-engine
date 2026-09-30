"""
Write the source-coverage report the project needs to be honest about itself.

Reports, per source: what kind of page it is, whether it is crawled, how it is
reached (plain HTTP, TLS-exception, or a real browser), and what it last
returned. Also separates job boards from funding portals, because a funding
portal returning zero positions is a fact about the world, not a bug.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "config" / "sources.yaml"
CRAWL = ROOT / "data" / "crawl_v2.json"
HEALTH = ROOT / "data" / "source_health.json"
OUT = ROOT / "data" / "coverage_report.json"
MD = ROOT / "data" / "COVERAGE.md"

REASON = {
    "verified_200": "Crawlable",
    "cloudflare_403": "Cloudflare bot wall (server-side, unbreakable without an account)",
    "blocked_bot_wall": "Cloudflare bot wall (403 even after challenge, zero anchors)",
    "unreachable_network": "Host unreachable from this network (DNS or TCP timeout)",
    "unknown": "Not yet probed",
    "not_a_board": "Not a job board — funding scheme or institutional portal",
    "excluded_bot_wall": "Bot-walled; excluded from the crawl set",
    "retired": "Programme sunset; no longer accepts applications",
    "thin": "Loads but returns no listings",
}
REACH = {
    True: "Playwright (Cloudflare / client-rendered)",
    False: "Plain HTTP",
}


def main() -> None:
    cfg = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    sources = cfg["sources"]

    crawl = json.loads(CRAWL.read_text(encoding="utf-8")) if CRAWL.exists() else {}
    per_source = crawl.get("per_source", {})

    # source_health.json is a list of per-source records, keyed here by name.
    health: dict[str, dict] = {}
    if HEALTH.exists():
        raw = json.loads(HEALTH.read_text(encoding="utf-8"))
        for rec in (raw if isinstance(raw, list) else raw.get("sources", [])):
            if isinstance(rec, dict) and "name" in rec:
                health[rec["name"]] = rec

    rows = []
    for s in sources:
        name = s["name"]
        st = s.get("status", "unknown")
        hit = per_source.get(name, {})
        rows.append({
            "name": name,
            "label": s.get("label", name),
            "kind": "job_board" if s.get("is_board") else "not_a_board",
            "status": st,
            "status_reason": REASON.get(st, st),
            "reach": REACH.get(bool(s.get("needs_browser")),
                               "Plain HTTP" + (" (TLS exception)" if s.get("verify_tls") is False else "")),
            "tls_verify": s.get("verify_tls", True),
            "last_returned": hit.get("n", 0),
            "last_note": hit.get("note", ""),
            "http_status": health.get(name, {}).get("http_status"),
            "bytes": health.get(name, {}).get("http_bytes"),
            "url": s.get("search_url") or s.get("url", ""),
        })

    boards = [r for r in rows if r["kind"] == "job_board"]
    not_boards = [r for r in rows if r["kind"] == "not_a_board"]
    # "Silent" means the board was actually crawled and returned nothing.
    # A skipped source never had a chance, so it is not silent.
    live = [r for r in boards if r["last_returned"] > 0]
    broken = [r for r in boards if r["last_returned"] == 0
              and r["last_note"] != "skipped"]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "sources_in_registry": len(rows),
            "job_boards": len(boards),
            "not_job_boards": len(not_boards),
            "boards_returning_data": len(live),
            "boards_returning_nothing": len(broken),
            "distinct_postings_found": len(crawl.get("positions", [])),
        },
        "boards_with_data": sorted(live, key=lambda r: -r["last_returned"]),
        "boards_without_data": broken,
        "not_job_boards": sorted(not_boards, key=lambda r: r["name"]),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Markdown summary ───────────────────────────────────────────
    t = report["totals"]
    md = [
        "# PhDiscover — Source Coverage",
        "",
        f"_Generated {report['generated_at']}_",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Sources in registry | {t['sources_in_registry']} |",
        f"| Real job boards | {t['job_boards']} |",
        f"| Boards returning data | {t['boards_returning_data']} |",
        f"| Boards returning nothing | {t['boards_returning_nothing']} |",
        f"| Not job boards (funding schemes, portals) | {t['not_job_boards']} |",
        f"| Distinct postings harvested | {t['distinct_postings_found']} |",
        "",
        "## Boards returning data",
        "",
        "| Source | Postings | Reached via |",
        "|---|---|---|",
    ]
    for r in report["boards_with_data"]:
        md.append(f"| {r['label']} | {r['last_returned']} | {r['reach']} |")

    md += ["", "## Boards that return nothing", "",
           "| Source | Status | Why |", "|---|---|---|"]
    for r in broken:
        md.append(f"| {r['label']} | `{r['status']}` | {r['status_reason']} |")

    md += ["", "## Not job boards", "",
           "These are funding programmes and institutional portals. They were in the",
           "original list but do not list individual vacancies, so they are excluded",
           "from the crawl rather than counted as failures.", "",
           "| Source | Why |", "|---|---|"]
    for r in report["not_job_boards"]:
        md.append(f"| {r['label']} | {r['status_reason']} |")

    MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"Boards: {t['job_boards']}  |  returning data: {t['boards_returning_data']}  |  "
          f"silent: {t['boards_returning_nothing']}")
    print(f"Postings harvested: {t['distinct_postings_found']}")
    print(f"\nSaved -> {OUT}")
    print(f"Saved -> {MD}")


if __name__ == "__main__":
    main()
