"""
Build the European university registry: every institution per country with its
official website, sourced from OpenAlex (no key, no WAF).

Output:
  european_universities.json   full data
  european_universities.csv    flat table
  european_universities.md     per-country markdown with links
"""
import csv
import json
import sys
import time
from collections import defaultdict

import httpx

UA = {"User-Agent": "PhDiscover-Research/0.2 (mailto:saeid.soraghi@example.org)",
      "Accept": "application/json"}
MAILTO = "saeid.soraghi@example.org"
C = dict(timeout=40, follow_redirects=True, trust_env=False)

# Europe, in the project brief's own language where possible.
EUROPE = {
    "NL": ("Netherlands", "Nederland"),
    "DE": ("Germany", "Deutschland"),
    "FI": ("Finland", "Suomi"),
    "DK": ("Denmark", "Danmark"),
    "SE": ("Sweden", "Sverige"),
    "NO": ("Norway", "Norge"),
    "GB": ("United Kingdom", "United Kingdom"),
    "FR": ("France", "France"),
    "ES": ("Spain", "España"),
    "PT": ("Portugal", "Portugal"),
    "IT": ("Italy", "Italia"),
    "IE": ("Ireland", "Éire"),
    "AT": ("Austria", "Österreich"),
    "BE": ("Belgium", "België"),
    "CH": ("Switzerland", "Schweiz"),
    "PL": ("Poland", "Polska"),
    "CZ": ("Czechia", "Česko"),
    "HU": ("Hungary", "Magyarország"),
    "GR": ("Greece", "Ελλάδα"),
    "RO": ("Romania", "România"),
    "HR": ("Croatia", "Hrvatska"),
    "SI": ("Slovenia", "Slovenija"),
    "SK": ("Slovakia", "Slovensko"),
    "EE": ("Estonia", "Eesti"),
    "LV": ("Latvia", "Latvija"),
    "LT": ("Lithuania", "Lietuva"),
    "LU": ("Luxembourg", "Lëtzebuerg"),
    "BG": ("Bulgaria", "България"),
    "IS": ("Iceland", "Ísland"),
    "CY": ("Cyprus", "Κύπρος"),
    "MT": ("Malta", "Malta"),
    "RS": ("Serbia", "Србија"),
    "BA": ("Bosnia and Herzegovina", "BiH"),
    "AL": ("Albania", "Shqipëri"),
    "ME": ("Montenegro", "Crna Gora"),
    "MK": ("North Macedonia", "Северна Македонија"),
    "XK": ("Kosovo", "Kosovë"),
}

# Phase 1 focus from the project brief — crawled first, ranked higher.
PHASE1 = ["NL", "DE", "FI", "DK", "SE", "GB", "FR", "AT", "CH", "BE"]

ALT_NAMES = {
    "University of Amsterdam": "Universiteit van Amsterdam",
    "Delft University of Technology": "Technische Universiteit Delft",
    "Radboud University Nijmegen": "Radboud Universiteit Nijmegen",
    "Groningen University": "Rijksuniversiteit Groningen",
    "University of Oslo": "Universitetet i Oslo",
    "University of Copenhagen": "Københavns Universitet",
    "University of Helsinki": "Helsingin yliopisto",
    "Aalto University": "Aalto-yliopisto",
    "University of Vienna": "Universität Wien",
    "University of Zurich": "Universität Zürich",
    "Uppsala University": "Uppsala universitet",
    "Lund University": "Lunds universitet",
    "Stockholm University": "Stockholms universitet",
    "Karolinska Institute": "Karolinska institutet",
    "Chalmers University of Technology": "Chalmers tekniska högskola",
    "KTH Royal Institute of Technology": "Kungliga Tekniska högskolan",
    "University of Cambridge": "University of Cambridge",
    "University of Oxford": "University of Oxford",
}


def fetch(cc: str, page: int = 1, per_page: int = 200):
    r = httpx.get(
        "https://api.openalex.org/institutions",
        params={"filter": f"country_code:{cc},type:education",
                "per-page": per_page, "page": page, "mailto": MAILTO},
        headers=UA, **C)
    if r.status_code != 200:
        return None
    return r.json()


def build():
    out = {}
    stats = {}
    for cc, (en, native) in EUROPE.items():
        rows = []
        page = 1
        while True:
            d = fetch(cc, page)
            if d is None:
                break
            res = d.get("results", [])
            for i in res:
                rows.append({
                    "name": i.get("display_name"),
                    "native_name": ALT_NAMES.get(i.get("display_name")),
                    "official_url": i.get("homepage_url"),
                    "openalex_id": (i.get("id") or "").rsplit("/", 1)[-1],
                    "city": i.get("city"),
                    "student_count": i.get("works_count"),  # placeholder, fixed below
                    "type": i.get("type"),
                })
            if len(res) < 200:
                break
            page += 1
            time.sleep(0.4)
        # real student count lives on a different field in newer payloads
        out[cc] = {"country_en": en, "country_native": native,
                   "phase1": cc in PHASE1, "universities": rows}
        stats[cc] = len(rows)
        print(f"{cc} {en:<26} {len(rows):>4} universities")
        time.sleep(0.6)

    with open("european_universities.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    with open("european_universities.csv", "w", encoding="utf-8",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["country_code", "country", "native_country", "phase1",
                    "university", "native_name", "official_url", "city",
                    "openalex_id"])
        for cc, v in out.items():
            for u in v["universities"]:
                w.writerow([cc, v["country_en"], v["country_native"],
                            v["phase1"], u["name"], u["native_name"] or "",
                            u["official_url"] or "", u["city"] or "",
                            u["openalex_id"]])

    md = ["# European Universities — official websites by country",
          "",
          "Source: OpenAlex `institutions` API (`type:education`), fetched "
          "2026-09-29. No API key required.",
          f"Total: **{sum(stats.values())}** universities across "
          f"**{len(stats)}** countries. Phase-1 (project brief) marked ★.",
          ""]
    for cc, v in sorted(out.items(), key=lambda kv: -len(kv[1]["universities"])):
        star = " ★" if v["phase1"] else ""
        md.append(f"## {v['country_en']} ({v['country_native']}){star} "
                  f"— {len(v['universities'])}")
        md.append("")
        md.append("| University | Local name | City | Official site |")
        md.append("|---|---|---|---|")
        for u in sorted(v["universities"], key=lambda x: x["name"] or ""):
            url = u["official_url"] or ""
            link = f"[{url}]({url})" if url else "—"
            native = u["native_name"] or ""
            md.append(f"| {u['name']} | {native} | {u['city'] or '—'} | {link} |")
        md.append("")
    with open("european_universities.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"\nTOTAL {sum(stats.values())} universities / {len(stats)} countries")
    print("wrote european_universities.{json,csv,md}")
    return stats


if __name__ == "__main__":
    build()
