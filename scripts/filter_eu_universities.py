"""
Filter the European university registry to institutions that plausibly grant
doctorates and run real research.

Two facts learned from the raw pull (2026-09-29):

1. OpenAlex `type` is `education` for all 4476 rows -- it carries no signal.
   Filtering on it is a no-op.
2. The `works_count` field I first stored under `student_count` is actually the
   institution's *paper count* in OpenAlex. A Kunsthochschule or police academy
   has single-digit papers; a doctoral university has thousands. That is the
   signal that actually separates them, and it is objective rather than a
   name guess.

So: keep if (name looks doctoral OR paper volume is high) AND it is not
explicitly a non-research school. Report the cutoff and everything excluded.
"""
import csv
import json
import re
from collections import Counter

SRC = "european_universities.json"

# Institutions with a doctoral/research mission: keep regardless of papers.
DOCTORAL_HINT = re.compile(
    r"(\buniversit|\buniver|\buniv\b|hochschule|université|universidad|"
    r"universiteit|università|universitate|univerzita|vyso[ck]á|"
    r"visokė|wyższ|univerz|teknillinen korkeakoulu|"
    r"københavns|uppsala|lund|køln|hamburg|berlijn|"
    r"kulta|helsingin|chalmers|kth|karolinska|"
    r"wageningen|embl|max.planck|polytechnique|"
    r"college of science|faculty of|school of)", re.I)

# Never a research university, whatever the paper count suggests.
DROP = re.compile(
    r"(akademie der polizei|akademie mode|akademie für (mode|design|medien)|"
    r"kunsthochschule|kunstakademie|musikhochschule|musikakademie|"
    r"filmhochschule|hochschule für kunst|"
    r"akademie für (bildende|darstellende|theater|design|medien)|"
    r"theologische hochschule|kirchliche hochschule|christliche hochschule|"
    r"private hochschule|hochschule für öffentliche|"
    r"fachhochschule|berufshochschule|berufsakademie|"
    r"akademie der landesüberwachung|"
    r"police academy|military|defence college|war college|"
    r"volkshochschule|gemeinschaftsakademie|"
    r"akademie für weiterbildung|institut für weiterbildung|"
    r"sprachakademie|heilpraktikerschule|"
    r"universitätsklinik|university hospital|krankenhaus|clinic)",
    re.I)

# Applied-sciences / arts universities: real, but not biomechanics-PhD venues.
APPLIED = re.compile(r"(university of applied sciences|hogeschool|"
                     r"université des sciences appliqu|"
                     r"hochschule für öffentliche verwaltung|"
                     r"university of the arts|arts university)", re.I)

MIN_PAPERS = 40  # a research university clears this easily; an academy does not


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    out, excluded, reasons = {}, {}, Counter()

    for cc, v in data.items():
        kept, gone = [], []
        for u in v["universities"]:
            name = u.get("name") or ""
            papers = u.get("student_count") or 0  # misnamed field: it is works_count
            if not u.get("official_url"):
                r = "no_official_url"
            elif DROP.search(name):
                r = "non_research_school"
            elif APPLIED.search(name):
                r = "applied_sciences_or_arts"
            elif DOCTORAL_HINT.search(name) and papers >= MIN_PAPERS:
                r = None
            elif papers >= 400:
                r = None  # high paper volume, clearly research-active
            else:
                r = "low_research_activity"
            if r is None:
                kept.append(u)
            else:
                gone.append({"name": name, "url": u.get("official_url"),
                             "papers": papers, "reason": r})
                reasons[r] += 1
        out[cc] = {**v, "universities": kept}
        if gone:
            excluded[cc] = gone

    total_kept = sum(len(v["universities"]) for v in out.values())
    total_all = sum(len(v["universities"]) for v in data.values())

    for path, obj in [("european_universities_filtered.json", out),
                      ("european_universities_excluded.json", excluded)]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)

    with open("european_universities.csv", "w", encoding="utf-8",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["country_code", "country", "native_country", "phase1",
                    "university", "native_name", "official_url",
                    "openalex_papers", "openalex_id"])
        for cc, v in out.items():
            for u in sorted(v["universities"], key=lambda x: x["name"] or ""):
                w.writerow([cc, v["country_en"], v["country_native"],
                            v["phase1"], u["name"], u["native_name"] or "",
                            u["official_url"] or "",
                            u.get("student_count") or 0, u["openalex_id"]])

    md = ["# European Universities — official websites by country", "",
          "Source: OpenAlex `institutions` API "
          "(`filter=country_code:XX,type:education`), fetched 2026-09-29. "
          "No API key required.", "",
          f"**{total_kept}** research universities across **{len(out)}** "
          f"countries. Filtered from {total_all} raw rows by OpenAlex paper "
          f"volume (>=40 papers, or >=400 regardless of name) minus explicit "
          f"non-research schools. Every exclusion is listed with a reason in "
          f"`european_universities_excluded.json`.", "",
          "★ = Phase-1 focus country (NL, DE, FI, DK, SE, GB, FR, AT, CH, BE).",
          ""]
    for cc, v in sorted(out.items(), key=lambda kv: -len(kv[1]["universities"])):
        if not v["universities"]:
            continue
        star = " ★" if v["phase1"] else ""
        md += [f"## {v['country_en']} ({v['country_native']}){star} "
               f"— {len(v['universities'])}", "",
               "| University | Local name | Official site | Papers |",
               "|---|---|---|---|"]
        for u in sorted(v["universities"], key=lambda x: -(x.get("student_count") or 0)):
            url = u["official_url"] or ""
            link = f"[{url.rstrip('/')}]({url})" if url else "—"
            md.append(f"| {u['name']} | {u['native_name'] or ''} | {link} "
                      f"| {u.get('student_count') or 0:,} |")
        md.append("")
    with open("european_universities.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"kept {total_kept} / {total_all}")
    print("exclusion reasons:", dict(reasons))
    print()
    for cc, v in sorted(out.items(), key=lambda kv: -len(kv[1]["universities"])):
        n = len(excluded.get(cc, []))
        if not v["universities"] and not n:
            continue
        print(f"  {cc} {v['country_en']:<24} {len(v['universities']):>4} (-{n})"
              + ("  [Phase1]" if v["phase1"] else ""))


if __name__ == "__main__":
    main()
