"""
Reject a deadline the parser had to guess at.

A date written without a year is not a deadline, it is a fragment. The parser
resolves "December 1" against the current year and reports 2026-12-01 with
status "open" — which on a site that shows deadlines is a fabricated fact the
user reads as authoritative.

This is the same failure as the "Date Placed: 28 Sep" case that was fixed once
already, arriving through the other door: the negative-cue guard rejects a label
that is not a deadline, but nothing here rejects a value that parses cleanly
while carrying no year.

Two distinct things are asserted:

1. A yearless date is not reported as a deadline with a resolved year.
2. The existing behaviour still works — a dated deadline keeps its value, and a
   label that is not a deadline is still refused rather than silently dropped.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from phdiscover.deadlines import extract_deadline, is_rolling  # noqa: E402

results: list[bool] = []


def check(label: str, got, want) -> None:
    """Compare-and-report, matching test_deadlines.py.

    The first version took a boolean and a detail string, and the calls were
    written against got/want — so every check compared True against None and
    failed for a reason that had nothing to do with the code under test.
    """
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:56}{str(got)[:24]}")
    if not ok:
        print(f"        expected: {want}")


def main() -> int:
    # 1. A yearless date must not become a resolved deadline.
    for text, cue in [
        ("Applications close December 1", "close"),
        ("Deadline: 15 March", "deadline"),
        ("Apply by 3 June", "apply by"),
    ]:
        got = extract_deadline(text)
        check(f"yearless {cue!r} resolves to no date",
              got.value, None)

    # 2. Dated deadlines still work — this is the behaviour being protected.
    for text, want_year in [
        ("Deadline: 7 October 2026", 2026),
        ("Closing date 15/01/2027", 2027),
        ("Apply by 2026-12-01", 2026),
    ]:
        got = extract_deadline(text)
        check(f"dated {text[:28]!r} keeps its year",
              got.value.year if got.value else None, want_year)

    # 3. A label that is not a deadline is still refused.
    for text in ("Date placed: 28 September 2026",
                 "Start date 1 September 2026",
                 "Project period 2024-2027",
                 "Published 3 May 2026",
                 "Posted: 2026-09-28"):
        got = extract_deadline(text)
        check(f"non-deadline label {text[:26]!r} refused",
              got.value, None)

    # 4. Rolling postings still read as rolling. The patterns are about a
    # deadline being absent or the post staying open until filled — not about
    # being open "throughout the year", which is how the first version of this
    # test was written and which is why it failed against correct code.
    for text in ("Open until filled",
                 "There is no application deadline",
                 "Reviewed on a rolling basis"):
        check(f"rolling detected in {text[:30]!r}", is_rolling(text), True)
    check("a dated deadline is not rolling",
          is_rolling("Deadline: 7 October 2026"), False)

    passed = sum(results)
    print(f"\n{'=' * 66}\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())