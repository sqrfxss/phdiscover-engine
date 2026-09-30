"""Deadline parsing and the stop-paginating rule, tested against real formats."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from phdiscover.deadlines import (
    DeadlineStatus, PageFreshness, classify_page, extract_deadline,
    is_rolling, should_stop_paginating,
)

TODAY = date(2026, 9, 29)          # fixed so the test never rots
FUTURE = (TODAY + timedelta(days=40)).strftime("%d %b %Y")
PAST = (TODAY - timedelta(days=40)).strftime("%d %b %Y")

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:56} {str(got)[:26]}")
    if not ok:
        print(f"        expected: {want}")


def main():
    print("=== format parsing ===")
    cases = [
        (f"Application deadline: {FUTURE}", DeadlineStatus.OPEN),
        (f"Deadline: {PAST}", DeadlineStatus.EXPIRED),
        (f"Closes {FUTURE}", DeadlineStatus.OPEN),
        (f"Apply by {FUTURE}", DeadlineStatus.OPEN),
        (f"applications are due {FUTURE}", DeadlineStatus.OPEN),
        (f"Submit before {FUTURE}", DeadlineStatus.OPEN),
        (f"2026-12-31 application closes", DeadlineStatus.OPEN),
        (f"Deadline {PAST} — do not apply", DeadlineStatus.EXPIRED),
        (f"Last date: {PAST}", DeadlineStatus.EXPIRED),
        # Two unlabeled dates after a posting-date label: the guard is applied
        # to both, so nothing is claimed. A second date on the card is not
        # trusted as a deadline just because the first one was labelled — the
        # card's own label says its dates are posting dates.
        (f"Posted 01 Oct 2026. {FUTURE}", DeadlineStatus.UNKNOWN),
        (f"{PAST}. University of X", DeadlineStatus.EXPIRED),
        # A date about the project, not the application deadline.
        ("The project runs 2024-01-15 to 2027-01-15", DeadlineStatus.UNKNOWN),
        # But if the card states a real deadline as well, that one is used.
        (f"The project runs 2026-01-15 to 2028-01-15. Deadline: {FUTURE}",
         DeadlineStatus.OPEN),
    ]
    for text, want in cases:
        d = extract_deadline(text, today=TODAY)
        check(f"status of {text[:36]!r}", d.status, want)

    print("\n=== ISO wins over ambiguous text ===")
    # "Deadline: 18/10/2026 (posted 2026-10-01)" — the first date in the text is
    # the deadline; the ISO one in the parens is a posting date and must be
    # ignored. The deadline cue wins over format preference.
    d = extract_deadline("Deadline: 18/10/2026 (posted 2026-10-01)", today=TODAY)
    check("the cue'd date wins over a later ISO posting date",
          d.value.isoformat(), "2026-10-18")

    d = extract_deadline("Applications close 2026-12-31. Posted 2026-01-05",
                         today=TODAY)
    check("ISO deadline wins over an earlier posting date",
          d.value.isoformat(), "2026-12-31")

    print("\n=== day/month order ===")
    d = extract_deadline("Deadline: 18/10/2026", today=TODAY)   # unambiguous
    check("18/10 is 18 Oct (only reading works)", d.value.isoformat(), "2026-10-18")
    d = extract_deadline("Deadline: 10/18/2026", today=TODAY)  # only valid month-first
    check("10/18 is 18 Oct (only reading works)", d.value.isoformat(), "2026-10-18")

    d = extract_deadline("Deadline: 05/10/2026", day_first=True, today=TODAY)
    check("05/10 day-first -> 5 Oct", d.value.isoformat(), "2026-10-05")
    d = extract_deadline("Deadline: 05/10/2026", day_first=False, today=TODAY)
    check("05/10 month-first -> 10 May", d.value.isoformat(), "2026-05-10")

    print("\n=== a year-less date rolls forward ===")
    d = extract_deadline(f"Deadline: {(TODAY - timedelta(days=10)).strftime('%d %b')}",
                         today=TODAY)
    check("a past month/day means next year", d.value.year, TODAY.year + 1)
    check("...and is therefore open", d.status, DeadlineStatus.OPEN)

    print("\n=== a posting date is not a deadline ===")
    # Measured on researchjobseurope.com: the card ends with
    # "Date Placed: 28 Sep" and no deadline at all. Taking that as the deadline
    # produced a date one year in the future.
    d = extract_deadline(
        "DPhil Studentship - Oxford. Salary: 21,805 Date Placed: 28 Sep",
        today=TODAY)
    check("'Date Placed' is not a deadline", d.status, DeadlineStatus.UNKNOWN)

    d = extract_deadline("PhD in gait. Posted: 03/09/2026", today=TODAY)
    check("'Posted' is not a deadline", d.status, DeadlineStatus.UNKNOWN)

    d = extract_deadline("PhD in gait. Published 2 weeks ago. 2026-10-16",
                         today=TODAY)
    check("ISO after 'Published' is not a deadline",
          d.status, DeadlineStatus.UNKNOWN)

    d = extract_deadline("PhD in gait. Start date 01 Sep 2026, end date 2028-09-01",
                         today=TODAY)
    check("'Start date' is not a deadline", d.status, DeadlineStatus.UNKNOWN)

    # A real deadline must survive the same guard, even on a card that also
    # prints a posting date.
    d = extract_deadline(
        f"Date Placed: 01 Sep 2026. Applications close {FUTURE}", today=TODAY)
    check("an explicit deadline still wins over 'Date Placed'",
          d.status, DeadlineStatus.OPEN)
    d = extract_deadline(
        f"Posted 01 Sep 2026. Deadline: {FUTURE}", today=TODAY)
    check("'Deadline:' still wins over 'Posted'", d.status, DeadlineStatus.OPEN)

    d = extract_deadline(
        f"Closing on: 2026-10-16 (Europe/Zurich) PhD in robotics", today=TODAY)
    check("'Closing on' is read (euraxess style)", d.value.isoformat(), "2026-10-16")

    print("\n=== a date with no label at all is still read ===")
    d = extract_deadline(f"{FUTURE} — PhD position in biomechanics",
                         today=TODAY)
    check("bare future date kept", d.status, DeadlineStatus.OPEN)
    d = extract_deadline(f"{PAST} — this call has closed", today=TODAY)
    check("bare past date reads as expired", d.status, DeadlineStatus.EXPIRED)

    print("\n=== nothing to parse ===")
    d = extract_deadline("PhD in biomechanics at a university", today=TODAY)
    check("no date -> UNKNOWN", d.status, DeadlineStatus.UNKNOWN)
    check("no date -> no value", d.value, None)

    print("\n=== impossible dates are not invented ===")
    d = extract_deadline("Deadline: 31/02/2027", today=TODAY)
    check("31 Feb -> UNKNOWN", d.status, DeadlineStatus.UNKNOWN)

    print("\n=== rolling posts are not expired ===")
    check("'open until filled' detected",
          is_rolling("This post is open until filled"), True)
    check("'rolling basis' detected",
          is_rolling("Applications reviewed on a rolling basis"), True)
    check("a normal deadline is not rolling",
          is_rolling("Deadline: 18 Oct 2026"), False)

    print("\n=== page classification ===")
    page = [
        {"title": "PhD A", "context": f"Deadline: {PAST}"},
        {"title": "PhD B", "context": f"Deadline: {FUTURE}"},
        {"title": "PhD C", "context": "open until filled"},
        {"title": "PhD D", "context": "no dates on this card"},
    ]
    f = classify_page(page)
    check("total", f.total, 4)
    check("expired", f.expired, 1)
    check("open", f.open, 1)
    check("rolling", f.rolling, 1)
    check("unknown", f.unknown, 1)
    check("has live content", f.has_live_content, True)
    check("not all expired", f.all_expired, False)

    stale = [{"title": f"PhD {i}", "context": f"Deadline: {PAST}"} for i in range(3)]
    f2 = classify_page(stale)
    check("all-expired page detected", f2.all_expired, True)

    print("\n=== the stop rule ===")
    def S(): return PageFreshness(total=3, expired=3)

    check("1 stale page -> keep going", should_stop_paginating([S()], 4), False)
    check("3 stale pages -> keep going", should_stop_paginating([S(), S(), S()], 4), False)
    check("4 stale pages -> stop", should_stop_paginating([S()] * 4, 4), True)
    check("5 stale pages -> stop", should_stop_paginating([S()] * 5, 4), True)

    fresh = PageFreshness(total=3, open=1, unknown=2)
    hist = [S(), S(), fresh, S()]
    check("a fresh page resets the run", should_stop_paginating(hist, 4), False)
    check("4 stale after a reset -> stop",
          should_stop_paginating([S(), fresh, S(), S(), S(), S()], 4), True)

    rolling_page = PageFreshness(total=2, rolling=2)
    check("a rolling-only page is not stale",
          should_stop_paginating([S(), S(), rolling_page, S()], 4), False)

    unreadable = PageFreshness(total=3, unknown=3)
    check("an unreadable page is not stale",
          should_stop_paginating([S(), S(), unreadable, S()], 4), False)

    passed = sum(results)
    print(f"\n{'=' * 62}\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
