"""
Filter + merge every crawl artefact into the site's position list.

Inputs (all optional, all merged):
  data/browser_positions.json   — the specialised EURAXESS browser crawl
  data/crawl_v2.json            — the unified HTTP+browser pass
  data/crawl_results.json       — the first generic pass

Five gates, each applied to the TITLE because that is the only field every one
of the 56 sources agrees on:
  1. role      — doctoral, not postdoc/faculty/teaching/engineering
  2. field     — not chemistry/materials/pharma/protein/etc.
  3. doctoral  — must actually say PhD/doctoral/studentship
  4. deadline  — not already past (kept when no date was readable)
  5. topic     — biomechanics / human movement / sensorimotor, and scored
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from phdiscover.deadlines import DeadlineStatus, extract_deadline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB_OUT = ROOT / "web" / "data" / "ranked_opportunities.json"
OUT = ROOT / "data" / "filtered_positions.json"

INPUTS = [
    ("data/university_crawl.json", "university_board", "University careers board"),
    ("data/browser_positions.json", "browser_crawler", "EURAXESS (EU official)"),
    ("data/crawl_v2.json", "crawl_v2", ""),
    ("data/crawl_results.json", "generic_crawler", ""),
]

DOCTORAL = ["phd", "doctoral", "doctorate", "doktorand", "dphil", "thesis",
            "studentship", "ph.d"]

REJECT_ROLE = [
    "postdoc", "postdoctoral", "post-doctoral", "junior postdoc",
    "professor", "professorship", "assistant professor", "associate professor",
    "lecturer", "teaching", "teacher", "research engineer", "engineer",
    "research assistant", "research scientist", "associate research",
    "tenure", "chair in", "group leader", "mta", "chief", "director",
    "head of", "coordinator", "manager", "intern", "visiting",
    "journal editor", "editor-in-chief", "peer reviewer",
]

REJECT_FIELD = [
    "chemistry", "chemical", "materials", "material science", "pharmac",
    "protein", "drug", "cancer", "oncolog", "immunolog", "virolog",
    "mineral", "mining", "petroleum", "civil engineering", "mechanical engineering",
    "electrical engineering", "pure mathematics", "astrophysics", "plant biology",
    "architecture", "food technology", "law", "history", "philosophy",
    "energy engineering", "telecommunication", "semiconductor", "catalysis",
    "polymer", "synthesis", "spectroscop", "crystallograph", "metallurg",
    "agriculture", "ecology", "botany", "geolog", "hydrolog", "climat",
    "social science", "psycholog", "education", "linguistic", "histor",
    "genetic", "genom", "proteom", "molecul", "membrane", "nanomaterial",
    "composite", "alloy", "concrete", "asphalt", "soil", "forest",
    "computer science", "software", "algorithm", "blockchain", "quantum comput",
    "civil", "structural", "aerospace", "automotive", "polytechnic",
]

BIOMECH_TITLE = [
    "biomechanic", "gait", "motor control", "sensorimotor", "kinesiolog",
    "human movement", "locomotion", "postural", "musculoskeletal",
    "ground reaction force", "clinical movement", "movement analysis",
    "behaviour analysis", "behavior analysis", "biomarker", "ergonom",
    "posture", "physical activity", "exercise", "athletic", "sport",
    "rehabilitation", "physiotherapy", "physiatr", "osteoarthritis",
    "cartilage", "stroke", "prosthe", "exoskeleton", "bionic", "kinetic",
    "kinematic", "human-robot", "dexterous",
]
# The user's own research is biomechanics + machine learning on human movement.
# A position that pairs either half with a biomedical or neural subject is a
# legitimate hit even when the title never says "biomechanics" — measured
# examples that a title-only gate threw away:
#   "PhD Candidate in AI-assisted medical imaging with MRI"   (ML + medical)
#   "Neuroscience PhD position (4-year funded)"                (neural)
#   "PhD in Probabilistic and Extremal Combinatorics"          (ML)
ADJACENT_TITLE = [
    "neuroscien", "cognit", "neuron", "neural", "brain", "motor",
    "human-robot", "human robot", "dexterous", "teleoperation",
    "medical imag", "clinical imag", "radiolog", "diagnos", "therap",
    "orthop", "patient", "biofeedback", "recover", "gait", "locomot",
    "machine learning", "deep learning", "artificial intelligence",
    "neural network", "computational", "data-driven", "modelling", "modeling",
    "movement", "posture", "balance", "coordination", "biomechanic",
]
BIOMECH_CONTEXT = BIOMECH_TITLE + [
    "motion capture", "wearable", "sensor", "marker-based", "inertial",
    "imu", "force plate", "emg", "electromyograph", "machine learning",
    "deep learning", "neural network", "artificial intelligence",
    "movement", "3d", "kinematics", "human", "patient",
]
ML_HINTS = ["machine learning", "deep learning", "neural network",
            "artificial intelligence", "ai-based", "predict", "modelling",
            "modeling", "quantification", "data-driven", "automated"]
SENSOR_HINTS = ["motion capture", "wearable", "sensor", "marker", "inertial",
                "imu", "force plate", "kinetic", "kinematic", "emg",
                "electromyograph", "3d", "pressure"]

OFFICIAL = {"euraxess", "msca", "jobbank", "eluta", "academicpositions",
            "academictransfer", "jobs_ac_uk", "phdjobs", "academicjobs"}


def gate_deadline(rec: dict) -> tuple[bool, str]:
    """
    Drop postings whose application deadline has already passed.

    Deliberately conservative. A posting is only removed when the crawl read a
    date it trusts and that date is in the past. Three cases are KEPT, because
    removing a live opportunity costs the user far more than showing a closed
    one they can see is closed:
      - no date was printed on the card at all
      - the date was unreadable or ambiguous (05/10 could be either)
      - the board states the post is rolling / open until filled
    """
    status = (rec.get("deadline_status") or "").lower()

    if status == "expired":
        return False, f"deadline passed ({rec.get('deadline_date') or rec.get('deadline','')})"
    if status in ("open", "rolling"):
        return True, "ok"
    # Crawl data from before the deadline fields existed, or a source that
    # never printed a date: fall back to re-reading the raw text.
    if status:
        return True, "ok"
    blob = f"{rec.get('title','')} {rec.get('context','')}"
    if not blob.strip():
        return True, "ok"
    d = extract_deadline(blob)
    if d.status is DeadlineStatus.EXPIRED:
        return False, f"deadline passed ({d.value.isoformat()})"
    return True, "ok"


def gate_title(title: str) -> tuple[bool, str]:
    low = title.lower()
    for p in REJECT_ROLE:
        if p in low:
            return False, f"role:{p}"
    for f in REJECT_FIELD:
        if f in low:
            return False, f"field:{f}"
    if not any(d in low for d in DOCTORAL):
        return False, "not-doctoral"
    return True, "ok"


def topic_gate(title: str, context: str) -> tuple[bool, int]:
    blob = f"{title} {context}".lower()
    tl = title.lower()

    title_hit = any(k in tl for k in BIOMECH_TITLE)
    adjacent_hit = any(k in tl for k in ADJACENT_TITLE)
    ctx_hits = [k for k in BIOMECH_CONTEXT if k in blob]

    # Core biomechanics: title says so, or the context is dense with it.
    if not title_hit and len(ctx_hits) < 2:
        # Adjacent field: the title itself must carry it, so a "PhD in
        # Combinatorics" with a passing mention of "movement" stays out.
        if not adjacent_hit:
            return False, 0
        score = 30
    else:
        score = 35
        if title_hit:
            score += 25
        elif len(ctx_hits) >= 4:
            score += 12

    if adjacent_hit:
        score += 8
    if any(k in blob for k in ML_HINTS):
        score += 18
    if any(k in blob for k in SENSOR_HINTS):
        score += 14
    if any(d in tl for d in DOCTORAL):
        score += 3
    return True, min(score, 100)


# Boards that render the whole teaser client-side, so the list page context is
# just a badge ("ORG Just Landed Canada . 2 days ago") and carries no topic
# words. Their cards cannot be judged until the detail page is fetched, so they
# are kept on title evidence alone and enriched afterwards.
THIN_CONTEXT_SOURCES = {"applykite", "academicjobs", "canadian_research"}
THIN_MIN_CTX = 120


def keep_despite_thin_context(row: dict) -> bool:
    if row.get("source") not in THIN_CONTEXT_SOURCES:
        return False
    t = row["title"].lower()
    if not any(d in t for d in DOCTORAL):
        return False
    # Still reject clearly non-biomechanics titles even on these boards.
    for f in REJECT_FIELD:
        if f in t:
            return False
    return len(row.get("context", "")) < THIN_MIN_CTX


def load_all() -> list[dict]:
    rows: list[dict] = []
    for rel, origin, default_label in INPUTS:
        path = ROOT / rel
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("positions", data) if isinstance(data, dict) else data
        for p in items:
            if not isinstance(p, dict) or "title" not in p:
                continue
            url = p.get("url") or p.get("application_url") or ""
            ctx = p.get("context") or p.get("description", "")
            rows.append({
                "title": strip_trailing_id(title_from_slug(
                    url, title_from_context(p["title"], ctx))),
                "url": url,
                "source": p.get("source", origin),
                "source_label": (p.get("source_label") or default_label
                                 or p.get("source", origin)),
                "context": ctx,
                "country": p.get("country", ""),
                "posted": p.get("posted", ""),
                "deadline": p.get("deadline", ""),
                "deadline_date": p.get("deadline_date", ""),
                "deadline_status": p.get("deadline_status", ""),
                                # A posting from a university's own board carries the
                                # institution with it, which is what makes it verifiable: a
                                # link on jobs.ethz.ch traces to ETH Zurich, a link on an
                                # aggregator traces to nothing.
                                "university": (p.get("university")
                                               or p.get("universities", "").split(",")[0].strip()),
                                "origin": p.get("method", origin),
            })
    return [r for r in rows if r["url"]]


# Boards that prefix every card with the employer name and a status badge, so
# the anchor text is "ORG Just Landed Canada . 2 days ago" rather than a title.
# Recover the real title from the tail of the anchor or from the URL slug.
TITLE_TAIL_RE = re.compile(r"(PhD|Doctoral|Postdoc|Postdoctoral|MSc|Master)\b.*$", re.IGNORECASE)
SLUG_RE = re.compile(r"/positions/([a-z0-9\-]+)/?$", re.IGNORECASE)
BADGE_WORDS = {"Just Landed", "Top university", "Posted", "days ago", "weeks ago",
               "months ago", "hours ago", "Closing soon", "New", "Hot"}

# Boards append an opaque id to the title, sometimes with no separator at all:
# "Phd opening in neuroscience photonics 5udgbc81w7". A 10+ character token that
# mixes letters and digits and carries no vowels cannot be part of a job title.
TRAILING_ID = re.compile(r"\s*[a-z0-9]{10,20}$", re.IGNORECASE)


def _looks_like_id(token: str) -> bool:
    """
    An opaque token: mixed letters AND digits.

    Deliberately narrow. Earlier this also matched vowel-less words like
    "Dexterous" and "Biomechanics", which silently truncated real titles — the
    cost of a false negative (a stray id in a title) is far below the cost of a
    false positive (a job title cut in half).
    """
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def strip_trailing_id(title: str) -> str:
    """Remove a board's internal id from the end of a title."""
    parts = title.rsplit(" ", 1)
    if len(parts) == 2 and _looks_like_id(parts[1]):
        return parts[0].strip(" .-–—") or title
    return title


def clean_title(raw: str) -> str:
    """Strip board chrome from an anchor's text."""
    t = raw.strip()
    for badge in BADGE_WORDS:
        t = t.replace(badge, " ")
    t = re.sub(r"Canada\s*\.|Posted\s+\w+\s+ago|\d+\s+\w+\s+ago", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .-–—")
    if TITLE_TAIL_RE.search(t):
        m = TITLE_TAIL_RE.search(t)
        t = t[m.start():]
    return t or raw.strip()


# Boards whose card heading is just the institution, with the real title as the
# first line of the teaser ("Queen's University" / "Senior Investigator We are
# seeking..."). Pull the title from the head of the context instead.
INSTITUTION_ONLY = re.compile(
    r"^[A-Z][\w'’\- ]{2,40}(University|College|Institute|Hospital|Agency|Centre|"
    r"Center|Laboratory|Labs|Technolog|Universit[eé]t|Universit\u00e4t)\s*(?:[-–—].*)?$",
    re.IGNORECASE)
JOB_TITLE_LEAD = re.compile(
    r"^((?:PhD|Doctoral|Postdoctoral|Postdoc|MSc|Masters?|Master of|"
    r"Research|Graduate|Doctor)[A-Za-z ,'’\-/()&]{8,140}?)"
    r"(?=\s+(?:We |The |This |A |An |In |At |with |using |for |to |and |—|-{2,}|\||$))",
    re.IGNORECASE)


def title_from_context(title: str, context: str) -> str:
    """
    Recover a real job title when the card heading is not one.

    Two patterns seen in the wild:
      - heading is only the institution  -> take the first clause of the teaser
      - heading is a badge              -> find the PhD/Doctoral run in the teaser
    """
    heading = clean_title(title)
    # A heading that already looks like a job is trusted.
    if JOB_TITLE_LEAD.match(heading) or any(
            d in heading.lower() for d in DOCTORAL):
        return heading
    if not INSTITUTION_ONLY.match(heading):
        return heading

    # The teaser usually starts with the role, then the sentence continues.
    m = JOB_TITLE_LEAD.match(context.strip())
    if m:
        return m.group(1).strip(" ,'-–—")
    # Otherwise the first comma-delimited chunk is the role.
    first = re.split(r"[,.]|\s+We\b|\s+The\b", context.strip(), maxsplit=1)[0]
    if 12 < len(first) < 160:
        return first.strip()
    return heading


def title_from_slug(url: str, fallback: str) -> str:
    """Boards like applykite bury the title in the URL and put chrome in the
    anchor text. The slug is the more reliable signal there."""
    m = SLUG_RE.search(url.split("?")[0])
    if not m:
        return fallback
    slug = m.group(1).replace("-", " ").strip()
    if len(slug) < 12:          # too short to be a title, e.g. "phd-in-x"
        return fallback
    return slug[0].upper() + slug[1:]


def guess_org(context: str, fallback: str, known: str = "") -> str:
    """
    Name the institution, preferring one we actually know over one we guessed.

    A posting harvested from a university's own board already carries the
    institution's name — it came from that site. Parsing the teaser text for
    "University of X" instead can produce a different or wrong name, so the
    registry's answer wins and the regex is only the fallback.
    """
    if known and len(known) > 3:
        return known[:90]
    m = re.search(
        r"(University of [A-Z][\w' -]+|Universit\u00e4t [A-Z\u00c4\u00d6\u00dc][\w' -]+|"
        r"Universit[e\u00e9] [A-Z][\w' -]+|[A-Z][\w' -]{2,30} University|"
        r"[A-Z][\w' -]{2,30} Institute|ETH ?Zurich|"
        r"Amsterdam UMC|Aalborg Universitet)",
        context)
    return m.group(1).strip()[:90] if m else fallback


def main() -> None:
    rows = load_all()
    stats = {"total_in": len(rows), "rejected_role": 0, "rejected_field": 0,
             "rejected_not_doctoral": 0, "rejected_deadline": 0,
             "rejected_offtopic": 0, "kept": 0}
    kept: list[dict] = []

    for r in rows:
        ok, reason = gate_title(r["title"])
        if not ok:
            if reason.startswith("role:"):
                stats["rejected_role"] += 1
            elif reason.startswith("field:"):
                stats["rejected_field"] += 1
            else:
                stats["rejected_not_doctoral"] += 1
            continue
        # Deadline, before the topic gate: an expired posting should not spend
        # a topic check, and must not be kept by the "thin context" escape
        # hatch below, which assumes the posting is a live one.
        ok, reason = gate_deadline(r)
        if not ok:
            stats["rejected_deadline"] += 1
            continue
        ok, fit = topic_gate(r["title"], r["context"])
        if not ok:
            # A thin-context board's teaser carries no topic words, so a miss
            # there is not evidence of irrelevance. Hold the card and let the
            # detail page decide.
            if not keep_despite_thin_context(r):
                stats["rejected_offtopic"] += 1
                continue
            stats["deferred_pending_enrichment"] = \
                stats.get("deferred_pending_enrichment", 0) + 1
            r["fit"] = 40          # provisional; rescored after enrichment
            kept.append(r)
            continue
        r["fit"] = fit
        kept.append(r)
    stats["kept"] = len(kept)

    best: dict[str, dict] = {}
    for r in kept:
        k = r["url"].split("?")[0].rstrip("/")
        if k not in best or len(r["context"]) > len(best[k]["context"]):
            best[k] = r
    final = sorted(best.values(), key=lambda x: -x["fit"])

    payload = [
        {
            "id": f"pd-{i}",
            "title": f["title"],
            "university": guess_org(f["context"], f["source_label"], f.get("university", "")),
            "country": f["country"] or "N/A",
            "department": "N/A",
            "pi_name": "See official posting",
            "pi_email": "",
            "funding": "PhD position \u2014 see official posting",
            "application_deadline": (f.get("deadline") or "").strip()
                                    or "See official posting",
            "deadline_date": (f.get("deadline_date") or "").strip(),
            "deadline_status": (f.get("deadline_status") or "").strip() or "unknown",
            "opportunity_type": "FUNDED_PHD",
            "research_domain": "Biomechanics / Human Movement",
            "research_fit_score": f["fit"],
            "contact_priority": "HIGH" if f["fit"] >= 75 else "MEDIUM" if f["fit"] >= 55 else "LOW",
            "url": f["url"],
            "source_url": f["url"],
            "posted": f["posted"],
            "description": f["context"][:400],
            "source": f["source_label"],
            "evidence": [{
                "claim": f"Position listed on {f['source_label']}",
                "value": f["title"],
                "source_url": f["url"],
                "source_type": "official" if f["source"] in OFFICIAL else "aggregator",
                "confidence": 0.9 if "browser" in f["origin"] else 0.65,
            }],
        }
        for i, f in enumerate(final, 1)
    ]

    WEB_OUT.parent.mkdir(parents=True, exist_ok=True)
    WEB_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT.write_text(json.dumps({"stats": stats, "positions": final},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 70)
    print("FILTER REPORT")
    print("=" * 70)
    for k, v in stats.items():
        print(f"  {k:24} {v}")
    srcs: dict[str, int] = {}
    for f in final:
        srcs[f["source_label"]] = srcs.get(f["source_label"], 0) + 1
    print(f"\nSources contributing ({len(srcs)}):")
    for s, n in sorted(srcs.items(), key=lambda kv: -kv[1]):
        print(f"  {s:34} {n}")
    print(f"\nTop 20:")
    for f in final[:20]:
        print(f"  [{f['fit']:3}] {f['title'][:50]:52} {f['source_label'][:22]}")
    print("=" * 70)


if __name__ == "__main__":
    main()
