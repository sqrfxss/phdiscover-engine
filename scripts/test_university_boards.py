"""
Board detection, tested against URLs measured from real universities.

Every expectation here came from a run: the false positives are the pages the
first sweep returned as boards and should not have (uamd.edu.al/misioni-dhe-vizioni,
uda.ad/recerca/escola-internacional, fr.suio.fr), and the true positives are the
boards it correctly found. No network, no fixtures to maintain.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from phdiscover.universities.discover import (  # noqa: E402
    extract_links, looks_like_board, normalize, rank_board_links,
)

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:34} {str(got)[:40]}")
    if not ok:
        print(f"        expected: {want}")


BOARDS = [
    # (url, why)
    ("https://jobs.ethz.ch/", "ETH Zurich subdomain board"),
    ("https://phd.ku.dk/", "Copenhagen doctoral board"),
    ("https://careers.utoronto.ca/", "Toronto careers subdomain"),
    ("https://jobs.unibe.ch/", "Bern jobs subdomain"),
    ("https://www.lmu.de/de/die-lmu/arbeiten-an-der-lmu/stellenportal",
     "Munich, nested German path"),
    ("https://www.kth.se/om/jobba-pa-kth", "KTH, Swedish path"),
    ("https://www.univie.ac.at/karriere", "Vienna, German path"),
    ("https://www.lu.se/vacancies", "Lund vacancies"),
    ("https://www.helsinki.fi/phd", "Helsinki doctoral"),
    ("https://tudelft.nl/onderwijs/opleidingen/phd", "TU Delft, Dutch path"),
    ("https://www.uni-freiburg.de/en/jobs", "Freiburg jobs"),
    ("https://www.uzh.ch/careers", "Zurich careers"),
]

NOT_BOARDS = [
    ("https://uamd.edu.al/misioni-dhe-vizioni",
     "mission and vision - first sweep hit"),
    ("https://www.uda.ad/recerca/escola-internacional",
     "research school - first sweep hit"),
    ("https://fr.suio.fr/", "student information system"),
    ("https://www.uzh.ch/en/research.html", "research overview"),
    ("https://www.uu.nl/en/organisation/about", "about page"),
    ("https://example.edu/privacy-policy", "privacy policy"),
    ("https://example.edu/media/brochure.pdf", "a PDF"),
]

HTML = """
<html><body>
  <a href="/en">English</a>
  <a href="/">Home</a>
  <a href="/de/die-lmu/arbeiten-an-der-lmu/stellenportal">Stellenportal</a>
  <a href="/de/fakultaeten">Fakult&auml;ten</a>
  <a href="/careers">Careers</a>
  <a href="https://www.other-university.de/jobs">External board</a>
  <a href="/privacy">Privacy</a>
  <a href="/news/2026/01/something">News item</a>
</body></html>
"""


def main():
    print("real boards are recognised")
    for url, why in BOARDS:
        check(why, looks_like_board(url), True)

    print("\npages that merely sound like boards are rejected")
    for url, why in NOT_BOARDS:
        check(why, looks_like_board(url), False)

    print("\nurl normalisation")
    check("query string dropped",
          normalize("https://x.edu/jobs?utm_source=a&id=7", "https://x.edu"),
          "https://x.edu/jobs?id=7")
    check("tracking param dropped",
          normalize("https://x.edu/jobs?utm_source=a", "https://x.edu"),
          "https://x.edu/jobs")
    check("trailing slash dropped",
          normalize("https://x.edu/jobs/", "https://x.edu"), "https://x.edu/jobs")
    check("host lowercased",
          normalize("https://X.EDU/Jobs", "https://x.edu"), "https://x.edu/Jobs")
    check("mailto rejected", normalize("mailto:a@b.c", "https://x.edu"), "")
    check("javascript rejected", normalize("javascript:void(0)", "https://x.edu"), "")

    print("\nranking puts the specific path first")
    links = extract_links(HTML, "https://www.lmu.de")
    ranked = [u for u, _ in links]
    check("a link was found at all", bool(ranked), True)
    check("the language switch is not a board",
          any(u.rstrip("/").endswith("/en") for u in ranked), False)
    check("the nested stellenportal wins",
          ranked[0] if ranked else "", "https://www.lmu.de/de/die-lmu/arbeiten-an-der-lmu/stellenportal")
    check("the off-site board is dropped",
          any("other-university" in u for u in ranked), False)
    check("the news item is dropped",
          any("/news/" in u for u in ranked), False)

    print("\nexternal and asset links never survive extraction")
    check("pdf dropped", [u for u, _ in extract_links(
        '<a href="/media/x.pdf">jobs brochure</a>', "https://x.edu")], [])

    passed = sum(results)
    print(f"\n{'=' * 62}\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())