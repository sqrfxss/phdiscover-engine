"""
Tests for the rescue stage's title decision.

The stage exists because a university board lists every vacancy it has, so most
cards are not biomechanics — and 155 of the 419 postings on the first
3,069-board crawl carried a title with no discipline in it ("PhD Researcher",
"Doctoral"). Those cannot be judged from the card, so their detail page is
fetched instead.

What matters is not catching every generic title but not misclassifying titled
ones. The first version of the pattern ended in `[\w\s]{0,40}?`, which accepts
any letters, so "Doctoral programme in Neuroscience" came back generic too:
every titled posting would have been sent for a fetch instead of being judged
on its card, and ~300 relevant rows would have been judged on a scraper
decision rather than on their text.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rescue_university_candidates import is_generic  # noqa: E402

# (title, expected, why)
CASES = [
    # --- generic: role only, no discipline -----------------------------------
    ("PhD", True, "bare role"),
    ("#PhD", True, "hashtag decoration"),
    ("PhD 2026", True, "ordinal decoration"),
    ("PhD (2 positions)", True, "count decoration"),
    ("Doctoral", True, "bare role"),
    ("PhD Researcher", True, "role and nothing else"),
    ("PhD Student", True, "role and nothing else"),
    ("PhD Student, in the area", True, "filler after the role"),
    ("PhD Candidates — open call", True, "filler after the role"),

    # --- not a vacancy page at all --------------------------------------------
    ("PhD defence of György Bodon", False, "a defence notice"),
    ("PhD Defense of Jane Roe", False, "a defence notice"),
    ("University Doctoral Regulations 2026", False, "policy page"),
    ("President's Vlog: Part-Time Doctoral Studies", False, "a vlog"),
    ("DBA vs. PhD: What's the Difference?", False, "comparison page"),
    ("Compare doctoral programmes", False, "comparison page"),
    ("PhD Alumni Network", False, "alumni page"),
    ("Doctoral Annual Report 2025", False, "report page"),
    ("How to apply for a PhD", False, "guidance page"),

    # --- not generic: the card names a discipline -----------------------------
    # These are the regression cases. Each one was generic before the pattern
    # stopped accepting arbitrary trailing words.
    ("Doctoral programme in Neuroscience", False, "names its field"),
    ("PhD in biomechanics and gait analysis", False, "names its field"),
    ("PhD Student Position in Motor Control", False, "names its field"),
    ("PhD candidate in neuroscience", False, "names its field"),
    ("PhD Researcher - PDTx", False, "names the project"),
    ("PhD Program Admissions", False, "names admissions"),
    ("Doctoral Colloquium in Accounting Research", False, "names its field"),
    ("Postdoc in Clinical Movement Analysis", False, "names its field"),

    # --- long titles are decided on the card ----------------------------------
    ("PhD position in sensorimotor control and gait biomechanics", False,
     "seven words, names its field"),
    ("Doctoral Researcher - Machine Learning for Human Movement", False,
     "seven words, names its field"),
]


def main() -> int:
    passed = 0
    fails: list[str] = []
    for title, want, why in CASES:
        got = is_generic(title)
        if got == want:
            passed += 1
            print(f"  PASS  {title[:46]:48} {why}")
        else:
            fails.append(f"{title!r}: expected generic={want}, got {got} ({why})")
            print(f"  FAIL  {title[:46]:48} expected={want} got={got}")

    # The property that actually matters, stated directly: a title naming a
    # discipline must never be treated as unjudgeable, whichever way round the
    # individual cases are written.
    named = ["Doctoral programme in Neuroscience", "PhD in biomechanics",
             "PhD candidate in neuroscience", "PhD Researcher - PDTx",
             "PhD Student Position in Motor Control"]
    leaks = [t for t in named if is_generic(t)]
    print(f"\n  {'PASS' if not leaks else 'FAIL'}  "
          f"no discipline-naming title is treated as generic")
    if leaks:
        fails.append(f"discipline named but judged generic: {leaks}")
    passed += 0 if leaks else 1

    print(f"\n{'=' * 64}")
    print(f"{passed}/{len(CASES) + 1} checks passed")
    if fails:
        print("\nfailures:")
        for f in fails:
            print(f"  - {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())