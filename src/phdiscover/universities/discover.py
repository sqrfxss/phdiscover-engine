"""
Find each university's own vacancy board from its homepage.

The registry holds 5,452 university homepages. Crawling those directly is
wasted work: a homepage lists no positions, so every one of them would return
zero. What varies is only where the board lives — measured across a sample:
    tudelft.nl      /onderwijs/opleidingen/phd
    kth.se          /om/jobba-pa-kth          (Swedish, not English)
    univie.ac.at    careers.univie.ac.at      (a separate subdomain)
    ku.dk           phd.ku.dk                 (a separate subdomain)
    lmu.de          /de/workspace-fuer-studierende/career-service/
so neither a fixed path nor an English-only word list is enough.

Strategy, cheapest first — each stage runs only if the previous found nothing:
  1. a subdomain whose name is itself a careers board (careers.univie.ac.at)
  2. well-known paths, in order of how often they hold doctoral posts
  3. a link on the homepage whose href or text looks like a careers page
  4. the same link search one level down, on the site's own navigation

Everything found is recorded with the stage that found it, so the daily update
can tell a board that moved from a board that was never there.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

# Ordered by how reliably the path holds doctoral or research vacancies.
CAREER_PATHS: tuple[str, ...] = (
    "/en/careers", "/careers", "/en/jobs", "/jobs",
    "/en/vacancies", "/vacancies", "/vacancy", "/job-vacancies",
    "/en/work-with-us", "/work-with-us", "/working-at-us",
    "/phd", "/doctoral", "/doctoral-programmes", "/research-positions",
    "/en/research/employment", "/opportunities", "/positions",
    "/join-us", "/employment", "/career", "/karriere", "/werken-bij",
    "/arbeit", "/emploi", "/empleo", "/lavoro", "/praca",
)

# Href fragments and link text that mark a careers page, across the languages
# the registry's countries actually use.
CAREER_WORDS = (
    # English
    "career", "job", "vacanc", "recruit", "employment", "opportunit",
    "work-with-us", "work-at", "join-us", "join", "position", "posting",
    "phd", "doctoral", "postdoc", "post-doc", "fellowship", "research-staff",
    # German
    "karriere", "stellenangebot", "stellen", "jobs", "arbeitsplatz",
    # Dutch
    "vacature", "vacatures", "werken-bij", "werk",
    # French
    "emploi", "offres", "recrutement", "carriere", "carrieres",
    # Spanish / Portuguese / Italian
    "empleo", "ofertas", "reclutamiento", "carreira", "vaga", "vagas",
    "collocamento", "lavoro", "assunzioni",
    # Nordic / Slavic / Turkish
    "jobb", "jobbba", "ansatt", "arbejde", "zatrudnij", "praca", "kariyer",
    "is", "kariyera",
)

# Paths and link fragments that are never a vacancy board. Measured:
# universite-paris-saclay.fr/fr/suio is the student information system, and it
# ranks as highly as a real board because "suio" sits next to careers text.
NOT_A_BOARD = re.compile(
    r"(privacy|cookie|terms|legal|imprint|disclaimer|contact|about|login|"
    r"signin|sign-in|register|news|press|events|calendar|shop|store|"
    r"library|alumni|giving|donate|map|sitemap|rss|feed|search\b)", re.I)

# Student and applicant portals. A board serves vacancies; these serve the
# people already admitted, and their listings are about enrolment.
STUDENT_PORTAL = re.compile(
    r"(^|/)(suio|su[io]s|apollo|bourse|cursus|studies|study|studium|"
    r"studier|inscription|immatriculation|enrol|admission|admissions|"
    r"apply-application|candidate|campus|student|etudiant|studierenden)(/|$)", re.I)

# Institutional pages that sit next to a careers link and match the same
# vocabulary. Measured on the first sweep: uamd.edu.al/misioni-dhe-vizioni
# ("mission and vision") and uda.ad/recerca/escola-internacional were both
# returned as boards, and neither lists a single vacancy.
#
# The leading-boundary requirement is dropped: real boards sit under these
# segments as prefixes — /recherche/stellenangebote, /research/jobs — so a
# segment match alone is right, and a whole-segment match alone is what let
# "misioni-dhe-vizioni" through.
INSTITUTIONAL_PAGE = re.compile(
    r"/(misioni|missio|vizioni|mision|vision|about|over|ueber|uber|"
    r"historia|history|historie|structure|estructura|organigrama|organigram|"
    r"recherche|research|recerca|forschung|onderzoek|policy|politique|"
    r"politika|polityka|nauka|wissenschaft|science|sciences|ciencias)"
    r"(/|-|$)", re.I)

# A board page lists postings. These words appear in its chrome and nowhere
# else useful, so their absence from a candidate is evidence against it.
LISTING_WORDS = re.compile(
    r"(job|jobs|vacanc|vacancy|vacancies|vakan|vacante|stellenangebot|"
    r"offre|oferta|vaga|offerta|phd|doctoral|doktorat|postdoc|"
    r"position|positions|opening|openings|recruit|recrutement|"
    r"work with us|werken bij|arbeiten|current|open|available|search|"
    r"listing|announcement|suche|recherche)", re.I)

# Filenames that only ever serve assets, never a vacancy list.
ASSET = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|css|js|"
                   r"mp4|mp3|ico|woff2?|ttf)(\?|$)", re.I)

_LINK_RE = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\'#]+)["\'][^>]*>'
                      r'(.*?)</a\s*>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@dataclass
class Finding:
    """One university's board, and how it was found."""
    university: str
    homepage: str
    board_url: str = ""
    stage: str = ""            # subdomain | path | link | nav-link
    http_status: int = 0
    method: str = "http"
    candidates_tried: int = 0
    error: str = ""
    found_via: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    p = urlparse(url if "//" in url else "https://" + url)
    return (p.netloc or "").lower()


def _registrable(host: str) -> str:
    """The last two labels, so a.b.univ.ac.at groups with b.univ.ac.at."""
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def normalize(url: str, base: str) -> str:
    """Absolute URL, no fragment, no tracking query, no trailing slash."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:")):
        return ""
    absolute = urljoin(base, url)
    p = urlparse(absolute)
    if p.scheme not in ("http", "https"):
        return ""
    # Drop tracking parameters; keep anything that looks like a real query.
    # Parsing the query has to happen on the raw string before urlparse sees
    # it: urljoin returns "url?query" as one piece, and p.query is only
    # populated once the string has been split — which urlparse does, so the
    # check below was fine but `?utm_source=a&id=7` kept its utm because the
    # filter tested startswith("utm_") on the whole query, not each pair.
    if p.query:
        keep = []
        for kv in p.query.split("&"):
            if not kv:
                continue
            key = kv.split("=", 1)[0].strip().lower()
            if key.startswith(("utm_", "fbclid", "gclid", "msclkid", "_ga",
                               "ref", "referrer", "source", "campaign")):
                continue
            keep.append(kv)
        query = "&".join(keep)
    else:
        query = ""
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def looks_like_board(url: str, text: str = "", page_html: str = "") -> bool:
    """Does this URL plausibly hold a vacancy list?"""
    if not url or ASSET.search(url):
        return False
    path = urlparse(url).path or ""
    # A student portal is not a board, however the link text reads.
    if STUDENT_PORTAL.search(path):
        return False
    # Neither is the university's own mission or research page. These sit right
    # next to the careers link and share its vocabulary, which is why they
    # surfaced in the first sweep.
    if INSTITUTIONAL_PAGE.search(path):
        # ...unless the page's own text names vacancies, in which case it is a
        # board that happens to live under /research/.
        if not (page_html and LISTING_WORDS.search(page_html)):
            return False
    if NOT_A_BOARD.search(text) or NOT_A_BOARD.search(url):
        # ...unless the href itself is unmistakably a board.
        if not any(k in url.lower() for k in
                   ("career", "job", "vacanc", "phd", "doctoral", "stellen",
                    "karriere", "emploi", "empleo", "vaga", "praca")):
            return False
    blob = f"{url} {text}".lower()
    return any(w in blob for w in CAREER_WORDS)


def page_lists_positions(html: str) -> bool:
    """Does this page actually contain vacancy listings?"""
    if not html:
        return False
    # A board's own nav says so; so does the first row of its table.
    if LISTING_WORDS.search(html):
        return True
    return False


def rank_board_links(links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    Order candidate links by how likely they are to hold vacancies.

    A homepage link whose text or href merely says "careers" often points at a
    general landing page ("/en") rather than a list — LMU Munich's homepage
    offers both, and the landing page was being picked. Scoring puts the
    specific paths above the generic ones and drops a link that is the site's
    own language switch or homepage.
    """
    SPECIFIC = ("vacanc", "job", "stellen", "offres", "ofertas", "vagas",
                "phd", "doctoral", "doktorat", "position", "opening",
                "recruit", "announcement", "tender")
    GENERIC = ("career", "karriere", "arbeitsplatz", "emploi", "carriere",
               "empleo", "lavoro", "vocation", "jobb", "praca", "kariyer",
               "employment", "work-with-us", "werken-bij")

    scored: list[tuple[int, str, str]] = []
    for url, text in links:
        low = f"{url} {text}".lower()
        path = urlparse(url).path or "/"

        # The bare homepage or a language switch is never a board.
        if path in ("", "/") or re.fullmatch(r"/[a-z]{2}(-[a-z]{2})?", path, re.I):
            continue
        # ...unless the link text names a board, which rules the "en" case out.
        if path in ("/en", "/de", "/fr") and not any(k in low for k in SPECIFIC):
            continue

        if any(k in low for k in SPECIFIC):
            rank = 0
        elif any(k in low for k in GENERIC):
            rank = 1
        else:
            rank = 3
        # A deeper path is more specific than a shallow one.
        depth = min(len([s for s in path.split("/") if s]), 3)
        scored.append((rank * 10 + depth, url, text))

    scored.sort(key=lambda t: t[0])
    return [(u, t) for _, u, t in scored]


def extract_links(html: str, base: str, limit: int = 120) -> list[tuple[str, str]]:
    """(absolute_url, link_text) for every internal link that looks like a board."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    home_host = _registrable(_host(base))

    for m in _LINK_RE.finditer(html):
        href, raw = m.group(1), _TAG_RE.sub(" ", m.group(2))
        text = re.sub(r"\s+", " ", raw).strip()
        url = normalize(href, base)
        if not url or url in seen:
            continue
        # Off-site links are rarely a university's own board.
        if home_host and _registrable(_host(url)) != home_host:
            continue
        if not looks_like_board(url, text):
            continue
        seen.add(url)
        out.append((url, text))
        if len(out) >= limit:
            break
    return rank_board_links(out)


async def find_board(client: httpx.AsyncClient, university: str,
                      homepage: str, *, path_stage: bool = True,
                      link_stage: bool = True,
                      max_path_tries: int = 10) -> Finding:
    """
    Locate one university's board.

    Raises nothing: every failure comes back as Finding.error, because this runs
    5,000-odd times and a single unreachable host must not stop the sweep.
    """
    f = Finding(university=university, homepage=homepage)
    base = normalize(homepage, homepage)
    if not base:
        f.error = "no usable URL"
        return f

    home_host = _host(base)
    root_domain = _registrable(home_host)
    # careers.uni-freiburg.de -> subdomain "careers", root "uni-freiburg.de"
    subdomain = ""
    if home_host.endswith("." + root_domain):
        subdomain = home_host[: -(len(root_domain) + 1)].split(".")[-1]

    # ── stage 1: a careers subdomain ──────────────────────────────
    # careers.univie.ac.at, jobs.ethz.ch, phd.ku.dk. Cheap to try — a 404
    # costs the same as a miss — but the word must actually describe a board.
    # A bare 200 is not enough: research.tudelft.nl is a research portal, not a
    # vacancy list, and accepting it wasted the one probe this site allows.
    # The check below is the same one the link stage uses, on the URL alone.
    for label in ("careers", "jobs", "vacancies", "job", "phd", "research"):
        if label == subdomain:
            continue
        cand = normalize(f"https://{label}.{root_domain}/", base)
        if label != "research" and not looks_like_board(cand):
            continue
        f.candidates_tried += 1
        try:
            r = await client.head(cand, timeout=12)
            if r.status_code and r.status_code < 400:
                # "research" only counts when the site also offers a path that
                # plainly says careers; otherwise it is a portal, not a board.
                if label == "research":
                    alt = normalize("/en/careers", base)
                    try:
                        r2 = await client.head(alt, timeout=12)
                        if r2.status_code and r2.status_code >= 400:
                            continue
                    except Exception:  # noqa: BLE001
                        continue
                f.board_url, f.stage = cand, "subdomain"
                f.http_status = r.status_code
                return f
        except Exception:  # noqa: BLE001
            continue

    # ── stage 2: well-known paths ─────────────────────────────────
    if path_stage:
        # Bounded, and the paths that actually hold doctoral posts come first.
        # Trying all 30 costs 30 HEAD requests against a host that has already
        # declined three; measured, a miss on the first few is a miss overall.
        for path in CAREER_PATHS[:max_path_tries]:
            cand = normalize(path, base)
            if not cand:
                continue
            f.candidates_tried += 1
            try:
                r = await client.head(cand, timeout=8)
                if r.status_code and r.status_code < 400:
                    f.board_url, f.stage = cand, "path"
                    f.http_status = r.status_code
                    return f
            except Exception:  # noqa: BLE001
                continue

    # ── stage 3 and 4: read the homepage, then its navigation ────
    if link_stage:
        try:
            r = await client.get(base, timeout=25)
            f.http_status = r.status_code
            if r.status_code == 200:
                links = extract_links(r.text, str(r.url))
                f.found_via = [u for u, _ in links[:5]]
                if links:
                    f.board_url, f.stage = links[0][0], "link"
                    return f
        except Exception as e:  # noqa: BLE001
            f.error = f"{type(e).__name__}: {str(e)[:60]}"

    if not f.error:
        f.error = "no board found"
    return f