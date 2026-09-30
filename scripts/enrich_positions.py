"""
Enrich surviving positions by fetching each detail page.

The list pages give a title and a one-line teaser. The detail page carries the
facts the site actually displays: institution, country, deadline, funding type,
and a description long enough to score against a research profile. Without this
step the fit score is guessing from a headline.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from phdiscover.deadlines import (  # noqa: E402
    DeadlineStatus, extract_deadline, is_rolling,
)

ROOT = Path("F:/hermes/phdiscover-engine")
INP = ROOT / "data" / "filtered_positions.json"
OUT = ROOT / "data" / "enriched_positions.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

INSECURE = {"academicpositions.com", "researchjobs.com", "findajob.eu",
            "mastersportal.com"}
# These hosts serve a certificate that does not match their hostname (measured
# 2026-09-29). verify=False is the only way to read them, so the second client
# below is used for them alone. Read-only public pages, no credentials sent.

COUNTRY_BY_TLD = {
    ".de": "Germany", ".nl": "Netherlands", ".at": "Austria", ".ch": "Switzerland",
    ".dk": "Denmark", ".se": "Sweden", ".no": "Norway", ".fi": "Finland",
    ".be": "Belgium", ".fr": "France", ".uk": "United Kingdom",
    ".ca": "Canada", ".us": "United States", ".au": "Australia", ".nz": "New Zealand",
    ".ie": "Ireland", ".pt": "Portugal", ".es": "Spain", ".it": "Italy",
    ".pl": "Poland", ".cz": "Czechia", ".gr": "Greece", ".jp": "Japan",
    ".cn": "China", ".in": "India", ".br": "Brazil",
}
COUNTRY_HINTS = [
    ("Amsterdam UMC", "Netherlands"), ("TU Delft", "Netherlands"),
    ("Radboud", "Netherlands"), ("Aalborg", "Denmark"), ("DTU", "Denmark"),
    ("Technical University of Denmark", "Denmark"), ("KU Leuven", "Belgium"),
    ("ETH Zurich", "Switzerland"), ("EPFL", "Switzerland"),
    ("University of Basel", "Switzerland"), ("NTNU", "Norway"),
    ("KTH", "Sweden"), ("Utrecht", "Netherlands"), ("Maastricht", "Netherlands"),
    ("KU Leuven", "Belgium"), ("Aarhus", "Denmark"), ("Copenhagen", "Denmark"),
    ("Oslo", "Norway"), ("Uppsala", "Sweden"), ("Karolinska", "Sweden"),
]

DEADLINE_RE = re.compile(
    r"(?:deadline|application close|closing date|apply before|applications? (?:are )?"
    r"(?:due|close)|submit by)\s*[:\-]?\s*"
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})", re.IGNORECASE)

FUNDING_RE = re.compile(
    r"(fully[- ]funded|funded position|salary|€\s?[\d,]{5,}|EUR\s?[\d,]{5,}"
    r"|gross salary|monthly|per month|4[- ]year|three[- ]year|48 months|36 months)",
    re.IGNORECASE)


def infer_country(url: str, blob: str) -> str:
    host = urlparse(url).netloc.lower()
    for tld, name in COUNTRY_BY_TLD.items():
        if host.endswith(tld):
            return name
    for hint, name in COUNTRY_HINTS:
        if hint.lower() in blob.lower():
            return name
    return ""


def pick_description(soup: BeautifulSoup) -> str:
    """Find the longest meaningful prose block, skipping nav and scripts."""
    best = ""
    for tag in soup.find_all(["p", "div", "section"]):
        if tag.find(["p", "div", "section"]):
            continue
        text = tag.get_text(" ", strip=True)
        if not (200 < len(text) < 4000):
            continue
        if re.search(r"(cookie|privacy|terms of use|all rights reserved|"
                     r"subscribe|newsletter|follow us|©)", text, re.I):
            continue
        if len(text) > len(best):
            best = text
    return best[:2500]


async def enrich_one(client: httpx.AsyncClient, item: dict) -> dict:
    url = item["url"]
    out = dict(item)
    try:
        r = await client.get(url)
    except Exception as e:  # noqa: BLE001
        out["enrich_error"] = f"{type(e).__name__}: {str(e)[:60]}"
        return out

    if r.status_code != 200:
        out["enrich_error"] = f"HTTP {r.status_code}"
        return out

    soup = BeautifulSoup(r.text, "html.parser")
    for bad in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        bad.decompose()

    desc = pick_description(soup)
    blob = f"{item.get('title','')} {item.get('context','')} {desc}"

    # Reuse the crawler's deadline parser: the detail page is the authoritative
    # source for a date, and a second regex here would only disagree with it.
    dl = extract_deadline(blob)
    rolling = is_rolling(blob)
    if dl.raw:
        out["deadline"] = dl.raw
        out["deadline_date"] = dl.value.isoformat() if dl.value else ""
        out["deadline_status"] = (DeadlineStatus.ROLLING if rolling
                                  else dl.status).value
    else:
        # Nothing here: keep whatever the listing card already established.
        out["deadline"] = item.get("deadline", "")
        out["deadline_date"] = item.get("deadline_date", "")
        out["deadline_status"] = (item.get("deadline_status")
                                  or "").strip() or "unknown"
    out["has_funding_evidence"] = bool(FUNDING_RE.search(blob))
    out["country"] = infer_country(url, blob) or item.get("country", "")
    out["description_full"] = desc
    out["enriched"] = True

    # University from the detail page's own metadata, else the <title>.
    for prop in ("og:site_name", "application-name"):
        el = soup.find("meta", attrs={"property": prop}) or \
             soup.find("meta", attrs={"name": prop})
        if el and el.get("content"):
            out["org_hint"] = el["content"][:90]
            break
    h1 = soup.find("h1")
    if h1 and len(h1.get_text(strip=True)) > 5:
        out.setdefault("page_heading", h1.get_text(" ", strip=True)[:140])
    return out


async def main() -> None:
    data = json.loads(INP.read_text(encoding="utf-8"))
    items = data["positions"]
    print(f"Enriching {len(items)} positions\n")

    hdrs = {"User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 trust_env=False, headers=hdrs) as secure, \
            httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False,
                              headers=hdrs, verify=False) as insecure:
        out = []
        for i, item in enumerate(items, 1):
            host = urlparse(item["url"]).netloc.lower()
            client = insecure if any(host.endswith(h) for h in INSECURE) else secure
            res = await enrich_one(client, item)
            flag = "OK " if res.get("enriched") else "ERR"
            print(f"  [{i:2}/{len(items)}] {flag} {res.get('country','?'):14} "
                  f"{res.get('title','')[:52]}")
            out.append(res)
            await asyncio.sleep(0.7)

    OUT.write_text(json.dumps({"positions": out, "stats": data.get("stats", {})},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Second pass ──────────────────────────────────────────────
    # Cards that were held back for a thin teaser can now be judged on real
    # description text, and every fit score can be recomputed against it.
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from filter_and_merge import gate_deadline, topic_gate  # noqa: PLC0415

    rescored, dropped_topic, dropped_deadline = [], 0, 0
    for r in out:
        blob = f"{r.get('title','')} {r.get('description_full','') or r.get('context','')}"
        # The detail page is where most deadlines actually appear, so the
        # deadline gate has to run again here — a posting kept by the list-page
        # filter because it printed no date can turn out to be closed on its
        # own page.
        alive, _why = gate_deadline(r)
        if not alive:
            dropped_deadline += 1
            continue
        ok, fit = topic_gate(r.get("title", ""), blob)
        if not ok:
            dropped_topic += 1
            continue
        r["fit"] = fit
        rescored.append(r)

    rescored.sort(key=lambda x: -x["fit"])
    OUT.write_text(json.dumps({"positions": rescored, "stats": data.get("stats", {})},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in out if r.get("enriched"))
    with_country = sum(1 for r in out if r.get("country"))
    with_deadline = sum(1 for r in out if r.get("deadline"))
    with_funding = sum(1 for r in out if r.get("has_funding_evidence"))
    print(f"\nEnriched:        {ok}/{len(out)}")
    print(f"Country known:   {with_country}/{len(out)}")
    print(f"Deadline found:  {with_deadline}/{len(out)}")
    print(f"Funding flagged: {with_funding}/{len(out)}")
    print(f"\nRescored on full text: {len(rescored)} kept, "
          f"{dropped_topic} off-topic, {dropped_deadline} expired dropped")
    for r in rescored:
        print(f"  [{r['fit']:3}] {r.get('title','')[:58]:60} {r.get('country','?')}")
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
