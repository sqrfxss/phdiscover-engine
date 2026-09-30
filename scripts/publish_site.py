"""
Publish the enriched dataset to the static site.

The site reads web/data/*.json directly, so publishing is a shape conversion:
internal field names -> the field names index.html expects, with the evidence
record the site renders under each card.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("F:/hermes/phdiscover-engine")
ENRICHED = ROOT / "data" / "enriched_positions.json"
WEB = ROOT / "web" / "data"
HEALTH_IN = ROOT / "data" / "source_health.json"

OFFICIAL_SOURCES = {"euraxess", "msca", "academicpositions", "academictransfer",
                    "jobs_ac_uk", "academicjobs", "jobbank", "researchjobs_com",
                    "canadian_research", "applykite", "biosciencecareers",
                    "nature_careers", "mastersportal"}


def confidence_of(row: dict) -> float:
    """Higher when the record came from an official board AND the detail page
    confirmed it with a real description."""
    c = 0.5
    if row.get("source") in OFFICIAL_SOURCES:
        c += 0.2
    if row.get("enriched"):
        c += 0.2
    if row.get("deadline"):
        c += 0.05
    if row.get("has_funding_evidence"):
        c += 0.05
    return round(min(c, 0.98), 2)


def main() -> None:
    data = json.loads(ENRICHED.read_text(encoding="utf-8"))
    rows = data["positions"]

    out = []
    for i, r in enumerate(rows, 1):
        desc = (r.get("description_full") or r.get("context") or "")[:420]
        evidence = [{
            "claim": f"Listed as a doctoral opening on {r.get('source_label', r.get('source',''))}",
            "value": r.get("title", ""),
            "source_url": r.get("url", ""),
            "source_type": "official" if r.get("source") in OFFICIAL_SOURCES else "aggregator",
            "confidence": confidence_of(r),
        }]
        if r.get("enriched"):
            evidence.append({
                "claim": "Detail page fetched and parsed",
                "value": f"{len(r.get('description_full',''))} chars of description"
                          + (f", country={r['country']}" if r.get("country") else ""),
                "source_url": r.get("url", ""),
                "source_type": "official",
                "confidence": 0.9,
            })
        if r.get("deadline"):
            evidence.append({
                "claim": "Application deadline found on the page",
                "value": r["deadline"],
                "source_url": r.get("url", ""),
                "source_type": "official",
                "confidence": 0.75,
            })
        if r.get("has_funding_evidence"):
            evidence.append({
                "claim": "Funding or salary language present",
                "value": "Yes",
                "source_url": r.get("url", ""),
                "source_type": "official",
                "confidence": 0.6,
            })

        out.append({
            "id": f"pd-{i:03d}",
            "title": r.get("title", ""),
            "university": (r.get("org_hint") or r.get("guess_org")
                           or r.get("university") or r.get("source_label", "")),
            "country": r.get("country") or "N/A",
            "department": "N/A",
            "pi_name": "See official posting",
            "pi_email": "",
            "funding": ("Funding evidence on page" if r.get("has_funding_evidence")
                        else "See official posting"),
            "application_deadline": (r.get("deadline") or "").strip()
                                    or "See official posting",
            # `or`, not `.get(..., default)`: the crawler leaves these as empty
            # strings when a board prints no date, and an empty status would
            # reach the site as missing rather than explicitly "not stated".
            "deadline_date": (r.get("deadline_date") or "").strip(),
            "deadline_status": (r.get("deadline_status") or "").strip() or "unknown",
            "opportunity_type": "FUNDED_PHD",
            "research_domain": "Biomechanics / Human Movement",
            "research_fit_score": r.get("fit", 0),
            "contact_priority": ("HIGH" if r.get("fit", 0) >= 75
                                else "MEDIUM" if r.get("fit", 0) >= 55 else "LOW"),
            "url": r.get("url", ""),
            "source_url": r.get("url", ""),
            "posted": r.get("posted", ""),
            "description": desc,
            "source": r.get("source_label", r.get("source", "")),
            "evidence": evidence,
        })

    WEB.mkdir(parents=True, exist_ok=True)
    (WEB / "ranked_opportunities.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "position_count": len(out),
        "sources_contributing": sorted({r.get("source_label", r.get("source", ""))
                                        for r in rows}),
        "with_deadline": sum(1 for r in rows if r.get("deadline")),
        "with_country": sum(1 for r in rows if r.get("country")),
        "enriched": sum(1 for r in rows if r.get("enriched")),
    }
    (WEB / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    if HEALTH_IN.exists():
        (WEB / "source_health.json").write_text(
            HEALTH_IN.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Published {len(out)} positions -> {WEB}")
    print(f"  sources contributing : {len(meta['sources_contributing'])}")
    print(f"  country resolved     : {meta['with_country']}")
    print(f"  deadline found       : {meta['with_deadline']}")
    print(f"  detail pages fetched : {meta['enriched']}")


if __name__ == "__main__":
    main()
