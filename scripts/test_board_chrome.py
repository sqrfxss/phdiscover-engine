"""
Guard the board-chrome filter against the titles a crawl actually produced.

A university's board is a whole page, not a list of vacancies. Everything on it
that is anchor-linkable — the nav bar, "Return", "Find out more:", pagination —
looks exactly like a posting to a scraper that reads URLs and anchor text.

Measured on the 3,481-board crawl: of 680 "postings", the largest title groups
were "home" (19), "Return" (10), "Careers" (8), "Find out more:" (8), "current
students &" (8), plus 10 rows titled "(untitled)". None is a vacancy. They were
being caught downstream by the topic gate, which meant every intermediate count
— raw postings, pages with data, boards with postings — was inflated.

The property that matters is one-sided. A missed piece of chrome is caught by
the filter downstream and costs nothing but a row. A real vacancy mistaken for
chrome is deleted before any page is read, and never comes back. So the tests
below are built around titles that must survive.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from crawl_all_boards import is_board_chrome  # noqa: E402

# (title, expected_chrome, why)
CASES = [
    # ── chrome: page furniture that is anchor-linkable ─────────────────────
    ("home", True, "nav item"),
    ("Home", True, "nav item, capitalised"),
    ("Return", True, "back link"),
    ("Back", True, "back link"),
    ("Menu", True, "nav toggle"),
    ("Search", True, "search button"),
    ("Careers", True, "nav item"),
    ("Find out more:", True, "call to action"),
    ("Read more", True, "truncated teaser link"),
    ("More information", True, "call to action"),
    ("Show more", True, "lazy-load button"),
    ("View all", True, "listing link"),
    ("See all", True, "listing link"),
    ("Apply now", True, "button"),
    ("Next", True, "pagination"),
    ("Previous", True, "pagination"),
    ("Page 2", True, "pagination"),
    ("Skip to main content", True, "accessibility link"),
    ("Main navigation", True, "landmark label"),
    ("Privacy policy", True, "footer"),
    ("Cookie settings", True, "footer"),
    ("Imprint", True, "footer"),
    ("(untitled)", True, "no title resolved"),
    ("untitled", True, "no title resolved"),
    ("", True, "empty"),
    ("   ", True, "whitespace only"),
    ("!!!", True, "punctuation only"),
    ("2026", True, "a year, not a title"),

    # ── real postings: must never be treated as chrome ─────────────────────
    # Each of these is a title form that appears in the crawl data. Deleting one
    # is unrecoverable, so they are the cases worth testing.
    ("Postdoctoral Fellow in Gait Biomechanics", False, "field named"),
    ("PhD in Sensorimotor Control", False, "field named"),
    ("PhD Researcher", False, "role, no field"),
    ("PhD Position in Clinical Movement Analysis", False, "field named"),
    ("Doctoral Candidate, Biomechanics of Human Movement", False, "field named"),
    ("PhD student in motion capture for rehabilitation", False, "field named"),
    ("Postdoc: Neural Control of Locomotion", False, "field named"),
    ("Home Economics PhD position", False, "'Home' is a field here"),
    ("Careers in Research", False, "'Careers' opens the title"),
    ("Careers and Employability PhD Programme", False, "'Careers' opens it"),
    ("PhD in Machine Learning for Movement Science", False, "field named"),
    ("Research Fellow in Sports Biomechanics", False, "field named"),
    ("Next-generation joint replacement PhD", False, "'Next' opens the title"),
    ("Page and Locomotion Studies PhD", False, "'Page' opens the title"),
]


def main() -> int:
    fails: list[str] = []
    for title, want, why in CASES:
        got = is_board_chrome(title)
        if got == want:
            print(f"  PASS  {title[:44]:46} {why}")
        else:
            fails.append(f"{title!r}: expected chrome={want}, got {got} ({why})")
            print(f"  FAIL  {title[:44]:46} expected={want} got={got}")

    # The asymmetry, stated directly: no real vacancy may be deleted.
    real = [t for t, want, _ in CASES if not want]
    killed = [t for t in real if is_board_chrome(t)]
    ok = not killed
    print(f"\n  {'PASS' if ok else 'FAIL'}  no real posting is treated as chrome")
    if killed:
        fails.append(f"real postings deleted as chrome: {killed}")

    print(f"\n{'=' * 66}")
    # Derived, never accumulated: an earlier version kept `passed` in step by
    # hand and printed 42/43 while every check was passing.
    total = len(CASES) + 1
    passed = total - len(fails)
    print(f"{passed}/{total} checks passed")
    if fails:
        print("\nfailures:")
        for f in fails:
            print(f"  - {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())