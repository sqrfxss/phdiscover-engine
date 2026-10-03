"""
Rescue postings the filter cannot judge from their listing card.

A university's board lists every vacancy it has, so most cards are not about
biomechanics — and many do not name a discipline at all. Measured on the first
3,069-board crawl: 419 postings, of which 155 carried a generic title ("Doctoral
Colloquium", "PhD Program Admissions", "Research Fellowship") that the topic
gate has to drop, because a title with no discipline in it is not evidence of
either relevance or irrelevance.

Reading the detail page settles it. So this stage takes only the rows the first
filter set aside *for having no topic words*, fetches their pages, and re-runs
the topic gate on the full description.

Deliberately narrow. It does not rescue rows the role gate rejected (a postdoc
is a postdoc whatever its page says), and it does not re-open the ones the topic
gate rejected with a clear discipline in the title — "Doctorial Colloquium in
Accounting Research" stays out whatever the page says.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from crawl_v2 import UA  # noqa: E402
from enrich_positions import (  # noqa: E402
    FUNDING_RE, infer_country, pick_description,
)
from filter_and_merge import gate_deadline, gate_title, topic_gate  # noqa: E402

IN = ROOT / "data" / "university_crawl.json"
OUT = ROOT / "data" / "university_candidates.json"

CONCURRENCY = 12
TIMEOUT = 25
# A page has to actually say something. Below this the fetch taught us nothing.
MIN_DESCRIPTION = 180

# A title that is only a role, with no discipline named in it. The listing card
# cannot be judged on topic from one of these, so the detail page has to be read.
#
# Two alternatives, and nothing may follow the role words:
#
#   1. The title is nothing but a role and ordinal decoration
#      ("PhD", "PhD 2026", "#PhD", "PhD (2 positions)").
#   2. The role is followed by filler, not a discipline — the trailing clause
#      has to be empty of content words, which is what stops
#      "Doctoral programme in Neuroscience" from matching: "Neuroscience" is a
#      content word and the title names its field.
#
# The earlier version ended in `[\w\s]{0,40}?`, which accepts any letters at
# all. That made every titled posting look generic, and would have discarded
# ~300 relevant rows before their detail pages were ever fetched.
GENERIC_BARE = re.compile(
    r"^[\s#*\-\u2013\u2014:]*"
    r"(?:phd|ph\.d|doctoral|doctorate|dphil|doctorand|postgraduate|research)?"
    r"[\s#*\-\u2013\u2014:,]*"
    r"(?:studentships?|candidates?|researchers?|fellows?|colloquiums?|"
    r"fellowships?|scholarships?|programmes?|programs?|positions?|"
    r"vacancies|opportunities|grants?|calls?|jobs?|enrolments?|enrollments?|"
    r"student)?"
    r"[\s#*\-\u2013\u2014:,()]*"
    r"(?:[0-9ivx]+)?"
    r"[\s#*\-\u2013\u2014:,()]*$",
    re.IGNORECASE)

# Filler that can trail a role word without naming a discipline.
#
# Any number of filler tokens, in any order. Both properties were needed and the
# first version had neither:
#
# - Repeatable: "PhD Candidates \u2014 open call" has two filler words after the
#   role, and a pattern matching one word per call left it unjudgeable.
# - Order-free: "PhD (2 positions)" puts the count *before* the noun it
#   qualifies, so a repeating sequence had to accept 2-then-positions as well as
#   positions-then-2. Token-by-token is simpler than trying to order them.
FILLER_TOKEN = (
    r"(?:in|at|on|for|of|to|and|or|the|a|an)\b"
    r"|(?:area|field|topic|subject|domain|discipline|section|call|open|now"
    r"|is|are|available|deadline|closed)\b"
    r"|(?:positions?|vacanc(?:y|ies)|students?|researchers?|candidates?"
    r"|fellows?|jobs?|grants?|awards?)\b"
    r"|[0-9ivx]+"
)

_FILLER_ONE = re.compile(r"^(?:" + FILLER_TOKEN + r")$", re.IGNORECASE)
_SPLIT_ON = re.compile(r"[\s#*\-\u2013\u2014:,()&]+")


def _is_filler_run(text: str) -> bool:
    """Every token in text is filler. Empty counts: nothing named a field."""
    tokens = [t for t in _SPLIT_ON.split(text) if t]
    return all(_FILLER_ONE.match(tok) for tok in tokens)


# Pages that share a board with the vacancies but are not vacancies. Their
# titles look exactly like the generic ones above, so the title alone cannot
# tell "PhD Student" the posting from "PhD defence of György Bodon" the notice.
NOT_A_POSTING = re.compile(
    r"\b(defence|defense|vlog|webinar|blog|newsletter|podcast|regulation|"
    r"regulations|policy|policies|guide|guideline|faq|about us|history|"
    r"annual report|ranking|compare|vs\.?|versus|difference|differences|"
    r"how to apply|prospectus|brochure|testimonial|alumni|donate|"
    r"contact us|opening hours)\b",
    re.IGNORECASE)


def _role_prefix_len(t: str) -> int:
    """
    How many characters the leading role words of t occupy.

    Two things this has to get right, both caught by the tests:

    - Plural forms, so `candidates?` matches the whole word. Written as
      `candidate|candidates` the alternation stopped at the singular and left a
      stray "s" in the tail, which then failed the filler match and made every
      "PhD Candidates" title look named.
    - It has to match exactly the same role vocabulary as GENERIC_BARE, or the
      tail handed to GENERIC_FILLER starts mid-word.
    """
    m = re.match(
        r"^[\s#*\-\u2013\u2014:]*"
        r"(?:phd|ph\.d|doctoral|doctorate|dphil|doctorand|postgraduate|research)?"
        r"[\s#*\-\u2013\u2014:,]*"
        r"(?:studentships?|candidates?|researchers?|fellows?|colloquiums?|"
        r"fellowships?|scholarships?|programmes?|programs?|positions?|"
        r"vacancies|opportunities|grants?|calls?|jobs?|enrolments?|enrollments?|"
        r"student)?",
        t, re.IGNORECASE)
    return m.end() if m else 0


# A title that names the vacancy is decidable on the card, whatever else it
# begins with: "PhD Position in the Centre for Clinical Biomechanics" opens with
# an institution and is still a titled posting.
LISTING_TITLE = re.compile(
    r"\b(?:phd|ph\.d|doctoral|doctorate|dphil|postdoc(?:toral)?|post-doc|"
    r"research(?:er|ers)?|studentship|vacanc(?:y|ies)|position|opening|"
    r"fellow(?:ship)?|grant|award|job|employment|recruit(?:ment|ing)?|"
    r"call\s+for)\b",
    re.IGNORECASE)

# A title that names no vacancy and no role at all. These boards print the
# posting's card with the institution or the funding regulation as its heading:
# "The Centre for Research and Studies in Sociology (CIES-Iscte) …", "According
# to Regulation no. 815/2025 …", "Under the terms of the Regulation on Merit
# Scholarships …". None of them can be judged from the card, and none of them
# is what GENERIC_BARE matches either, since they are longer than a role.
#
# Measured on the 3,481-board crawl: 214 of 595 rows. GENERIC_BARE caught 16.
# Every one of the other 214 was thrown away by the topic gate for having no
# topic words in its title, when the page it points at states the discipline.
# A heading that names the posting's institution or its funding rule rather than
# the vacancy.
#
# The test is positional, not lexical. Both of these titles contain "research",
# so a word list cannot separate them:
#
#   "PhD Position in the Centre for Clinical Biomechanics"   <- a vacancy, role
#                                                              word first
#   "The Centre for Research and Studies in Sociology (CIES)" <- not decidable,
#                                                              institution first
#
# An earlier version listed institution words and refused any title matching
# one that also contained a vacancy word. "The Centre for Research…" contains
# both, so it satisfied neither branch and fell through to the word-count
# shortcut, which returned early on its 16 words.
UNJUDGEABLE_TITLE = re.compile(
    r"^\s*(?:"
    r"(?:the\s+)?(?:cent(?:er|re|ro)|institute|department|faculty|school|"
    r"academy|foundation|association|society|unit|laboratory|lab)\b"
    r"|according\s+to\b|under\s+the\s+terms\s+of\b|per\s+regulation\b"
    r"|regulation\s+(?:no\.?|number)?|terms\s+and\s+conditions\b"
    r"|general\s+call\b"
    r")",
    re.IGNORECASE)

# A role word in the leading position is what makes a heading a vacancy.
ROLE_FIRST = re.compile(
    r"^\s*[\s#*\-\u2013\u2014:,]*"
    r"(?:phd|ph\.d|doctoral|doctorate|dphil|postdoc(?:toral)?|post-doc|"
    r"research(?:er|ers)?|studentship|call|grant|award|vacanc(?:y|ies)|"
    r"position|opening|fellowship|job|recruit(?:ment)?|talent)",
    re.IGNORECASE)


def is_generic(title: str) -> bool:
    """
    Does this title give the topic gate nothing to work with?

    Three ways to fail, checked in order:

    1. It is not a vacancy page at all ("PhD defence of György Bodon",
       "University Doctoral Regulations 2026", a vlog) — no fetch needed, the
       title already settles it.
    2. It is nothing but a role ("PhD", "PhD 2026", "#PhD").
    3. The role is followed only by filler ("PhD Student, in the area").

    Anything naming a discipline ("Doctorial programme in Neuroscience") is not
    generic: the card says enough and the topic gate decides on it directly.
    """
    t = title.strip()
    if not t:
        return True
    if NOT_A_POSTING.search(t):
        return False
    # An institution or funding-regulation heading, however long. Checked before
    # the word-count shortcut below, which assumed a long title always names its
    # discipline — "The Centre for Research and Studies in Sociology (CIES-Iscte)
    # of Iscte – University Institute of Lisbon" is 16 words and names none.
    if UNJUDGEABLE_TITLE.match(t) and not ROLE_FIRST.match(t):
        return True
    # Seven or more words otherwise means a discipline is almost certainly named.
    if len(t.split()) >= 7:
        return False
    if GENERIC_BARE.match(t):
        return True
    # Filler only, after stripping the leading role words. Checked before
    # LISTING_TITLE because "PhD (2 positions)" and "PhD Student, in the area"
    # both contain a vacancy word and would otherwise be accepted as titled.
    if _is_filler_run(t[_role_prefix_len(t):].strip()):
        return True
    # A heading that is an institution or a funding regulation: no vacancy and
    # no role, so only the page can say what it is.
    # Anything else that names a vacancy or a role is decided on the card.
    if LISTING_TITLE.search(t):
        return False
    # No vacancy word, no role word, not an institution heading: still nothing
    # to judge from the title alone.
    return not LISTING_TITLE.search(t)


async def fetch(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                row: dict) -> dict:
    async with sem:
        try:
            r = await client.get(row["url"], timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return {**row, "fetch_error": f"{type(e).__name__}: {str(e)[:50]}"}

    out = dict(row)
    out["fetch_status"] = r.status_code
    if r.status_code != 200:
        return out

    soup = BeautifulSoup(r.text, "html.parser")
    for junk in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        junk.decompose()
    desc = pick_description(soup)
    out["description_full"] = desc

    blob = f"{row.get('title','')} {row.get('context','')} {desc}"
    out["has_funding_evidence"] = bool(FUNDING_RE.search(blob))
    out["country"] = infer_country(row["url"], blob) or row.get("country", "")
    return out


async def main() -> int:
    if not IN.exists():
        print(f"{IN} not found — run scripts/crawl_all_boards.py first", file=sys.stderr)
        return 1

    data = json.loads(IN.read_text(encoding="utf-8"))
    postings = [p for p in data.get("positions", [])
                if str(p.get("source", "")).startswith("uni_")]
    if not postings:
        print("no university postings in the crawl")
        return 0

    # Only the rows the listing card cannot answer for.
    candidates = []
    for p in postings:
        title = p.get("title", "")
        ok, _ = gate_title(title)
        if not ok:
            continue
        if not is_generic(title):
            continue
        candidates.append(p)

    print(f"=> {len(postings)} university postings, "
          f"{len(candidates)} with a title that names no discipline")

    if not candidates:
        OUT.write_text(json.dumps({"positions": []}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        return 0

    sem = asyncio.Semaphore(CONCURRENCY)
    done = rescued = 0
    resolved: list[dict] = []

    limits = httpx.Limits(max_connections=CONCURRENCY,
                          max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True,
                                 timeout=TIMEOUT, limits=limits,
                                 headers={"User-Agent": UA,
                                          "Accept-Language": "en,*;q=0.5"}) as client:

        async def worker(row: dict) -> None:
            nonlocal done, rescued
            got = await fetch(client, sem, row)
            done += 1

            title = got.get("title", "")
            desc = got.get("description_full", "")
            if len(desc) >= MIN_DESCRIPTION:
                # Re-judge on the full text, not the card.
                blob = f"{title} {got.get('context','')} {desc}"
                ok, fit = topic_gate(title, blob)
                if ok:
                    alive, _ = gate_deadline(got)
                    if alive:
                        got["fit"] = fit
                        got["rescued_by"] = "detail page"
                        resolved.append(got)
                        rescued += 1
            if done % 25 == 0 or done == len(candidates):
                print(f"  {done}/{len(candidates)}  rescued={rescued}", flush=True)

        await asyncio.gather(*(worker(c) for c in candidates))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates_examined": len(candidates),
        "rescued": len(resolved),
        "positions": resolved,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    print(f"\n  examined        : {len(candidates)}")
    print(f"  rescued         : {rescued}")
    print(f"  -> {OUT}")
    for r in sorted(resolved, key=lambda x: -x.get("fit", 0))[:10]:
        print(f"    [{r['fit']:3}] {r.get('university','?')[:28]:30} {r['title'][:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
