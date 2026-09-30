"""
Extract application deadlines and decide whether a posting is still open.

Most boards do not normalise dates. Measured formats seen across the registry:

    "18 Oct 2026"        "18 October 2026"       "2026-10-18"
    "18/10/2026"         "10/18/2026"            "18.10.2026"
    "Deadline: 18 Oct"   "Closes: 2026-10-18"    "apply by 18 Oct 2026"
    "18 Oct 2026 23:59"  "2026-10-18T23:59:59Z"  "18 Oct"

Day-first versus month-first is genuinely ambiguous for 01–12, and getting it
wrong marks a live position as closed. So the resolution is explicit: a board
whose locale is day-first (most of Europe) gets day-first parsing, and any
value that is still ambiguous after that is reported as UNKNOWN rather than
guessed. A posting with an unknown deadline is kept — an open position wrongly
removed is worse than an expired one wrongly shown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum

# ── Formats ────────────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_ALT = "|".join(MONTHS)
MONTH_RE = rf"(?:{MONTH_ALT})"

# Words that introduce a deadline rather than merely a date. The negative
# patterns matter as much as the positive ones: boards print a "Date Placed" or
# "Published" line on the same card, and treating that as the application
# deadline invents a date that is not there — and, with a year-less value, can
# invent a date a whole year in the future.
DEADLINE_CUE = re.compile(
    r"(?:deadline|closing\s+date|closes?\s+(?:on|in)?|application\s+deadline|"
    r"apply\s+(?:by|before|until)|applications?\s+(?:are\s+)?due|"
    r"submit\s+(?:by|before)|registration\s+deadline|"
    r"applications?\s+open\s+until|closing\s+on)", re.IGNORECASE)

# Labels that mark a date as *not* a deadline.
NOT_A_DEADLINE = re.compile(
    r"(?:date\s+placed|placed\s+on|posted|published|advertised|"
    r"date\s+posted|created|updated|last\s+updated|"
    r"start\s+date|end\s+date|project\s+(?:start|end|runs?)|"
    r"project\s+period|contract\s+period)", re.IGNORECASE)

# 18 Oct 2026 | 18 October 2026 | 18 Oct
_DMY_TEXT = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RE})\.?\s*(\d{{4}})?\b",
                       re.IGNORECASE)
# Oct 18, 2026 | October 18 2026
_MDY_TEXT = re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?\b",
                       re.IGNORECASE)
# 2026-10-18 or 2026/10/18
_ISO = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
# 18/10/2026 or 10/18/2026
_DMY_NUM = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b")


class DeadlineStatus(str, Enum):
    OPEN = "open"            # deadline in the future
    EXPIRED = "expired"      # deadline has passed
    UNKNOWN = "unknown"      # no date found, or too ambiguous to trust
    # Rolling: some boards never state a date. Not expired, but not countable
    # for the "3 pages of expired posts, stop" rule either.
    ROLLING = "rolling"


@dataclass
class Deadline:
    raw: str
    value: date | None
    status: DeadlineStatus
    confidence: float

    @property
    def days_left(self) -> int | None:
        if self.value is None:
            return None
        return (self.value - date.today()).days


UNKNOWN = Deadline("", None, DeadlineStatus.UNKNOWN, 0.0)
ROLLING = Deadline("", None, DeadlineStatus.ROLLING, 0.5)


def _day_first_default() -> bool:
    """
    Most of the registry is European, where 18/10 means 18 October. Boards
    outside that convention can override it via DAY_FIRST_DEFAULT = False.
    """
    return True


# Set to False for boards that write US-style dates.
DAY_FIRST_DEFAULT = _day_first_default()  # noqa: N816


def _mk(year: int | None, month: int, day: int, raw: str, conf: float,
        today: date | None = None) -> Deadline:
    today = today or date.today()
    if year is None:
        # A date with no year means the next occurrence. A deadline already
        # past this year but with no year given is almost always next year's.
        year = today.year
        candidate = _safe_date(year, month, day)
        if candidate is None or candidate < today:
            year += 1
    value = _safe_date(year, month, day)
    if value is None:
        return Deadline(raw, None, DeadlineStatus.UNKNOWN, 0.0)
    status = DeadlineStatus.OPEN if value >= today else DeadlineStatus.EXPIRED
    return Deadline(raw, value, status, conf)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_deadline(text: str, *, day_first: bool | None = None,
                     today: date | None = None) -> Deadline:
    """
    Find an application deadline in a blob of text.

    Prefers a date that sits next to a deadline cue ("Deadline: 18 Oct 2026"),
    then falls back to any parseable date. The fallback matters because many
    cards print the date on its own line under a "Closing date" label that the
    card text strips out.
    """
    if not text:
        return UNKNOWN
    today = today or date.today()
    day_first = DAY_FIRST_DEFAULT if day_first is None else day_first

    # 1. ISO first — unambiguous, no ordering question. Same posting-date guard
    # as step 3, since a card can print both an ISO posting date and a
    # differently-formatted deadline.
    for m in _ISO.finditer(text):
        if _preceded_by(text, m.start(), NOT_A_DEADLINE):
            continue
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                   m.group(0), 0.95, today)

    # 2. A date preceded by a deadline cue anywhere in the text.
    for cue in DEADLINE_CUE.finditer(text):
        window = text[cue.end():cue.end() + 60]
        d = _parse_text_date(window, day_first, today)
        if d.status is not DeadlineStatus.UNKNOWN:
            return Deadline(f"{cue.group(0)} {d.raw}", d.value, d.status,
                            min(d.confidence + 0.05, 1.0))

    # 3. Any parseable date — but only if the nearest label before it is not one
    # of the "this is the posting date, not the deadline" labels. Without this
    # check a card reading "Date Placed: 28 Sep" yields an invented deadline,
    # and because the value carries no year it lands a year in the future.
    #
    # The guard is applied per candidate, not once: a card can carry a posting
    # date *and* a real deadline, and only the latter is wanted.
    for m in _all_dates(text):
        if _preceded_by(text, m.start(), NOT_A_DEADLINE):
            continue
        d = _match_to_deadline(m, day_first, today)
        if d.status is not DeadlineStatus.UNKNOWN:
            return Deadline(d.raw, d.value, d.status, max(d.confidence - 0.15, 0.3))

    return UNKNOWN


def _all_dates(text: str) -> list[re.Match]:
    """Every date in the text, ordered by position, earliest first."""
    found: list[tuple[int, re.Match]] = []
    for pat in (_ISO, _DMY_TEXT, _MDY_TEXT, _DMY_NUM):
        for m in pat.finditer(text):
            found.append((m.start(), m))
    found.sort(key=lambda pair: pair[0])
    # Drop overlaps: "2026-10-18" also matches the numeric pattern, and a
    # day-month pair can be re-matched inside a longer run.
    out: list[re.Match] = []
    consumed_to = -1
    for _, m in found:
        if m.start() < consumed_to:
            continue
        out.append(m)
        consumed_to = m.end()
    return out


def _preceded_by(text: str, pos: int, pattern: re.Pattern) -> bool:
    """True if `pattern` matches in the label immediately before `pos`.

    Looks at a short window behind the date, stopping at a sentence boundary, so
    a "Deadline: 18 Oct" earlier in the card does not mark a later "Date placed:
    28 Sep" as a deadline, and vice versa.
    """
    window = text[max(0, pos - 40):pos]

    # Split at the last sentence break, then decide twice:
    #   - the same clause as the date: a label here names THIS date. If it is a
    #     posting-date label, the date is not a deadline.
    #   - an earlier clause that carries no date of its own ("Published 2 weeks
    #     ago. 2026-10-16"): the period is not a real boundary, because a
    #     relative phrase names no calendar date. A label there still applies.
    cut = -1
    for stop in (". ", "\n", "; ", " | "):
        i = window.rfind(stop)
        if i != -1:
            cut = max(cut, i + len(stop))
    near = window[cut:] if cut != -1 else window

    if pattern.search(near):
        return True
    if cut != -1 and pattern.search(window[:cut]) and not DEADLINE_CUE.search(window):
        return True
    return False


def _match_to_deadline(m: re.Match, day_first: bool, today: date) -> Deadline:
    """
    Convert one already-matched date into a Deadline.

    Needed because re-running the patterns over `text[m.start():]` would find a
    *later* date instead — the match itself is the only correct answer, and it
    identifies its own format by which pattern produced it.
    """
    groups = m.groups()
    raw = m.group(0)
    if m.re is _ISO:
        return _mk(int(groups[0]), int(groups[1]), int(groups[2]), raw, 0.9, today)
    if m.re is _DMY_TEXT:
        return _mk(_maybe_year(groups[2]), MONTHS[groups[1].lower().rstrip(".")],
                   int(groups[0]), raw, 0.9, today)
    if m.re is _MDY_TEXT:
        return _mk(_maybe_year(groups[2]), MONTHS[groups[0].lower().rstrip(".")],
                   int(groups[1]), raw, 0.9, today)
    # _DMY_NUM: two numbers and a year, order depends on which exceeds 12.
    a, b = int(groups[0]), int(groups[1])
    y = int(groups[2])
    if y < 100:
        y += 2000
    if a > 12 and b <= 12:
        return _mk(y, b, a, raw, 0.9, today)
    if b > 12 and a <= 12:
        return _mk(y, a, b, raw, 0.9, today)
    if day_first:
        return _mk(y, b, a, raw, 0.6, today)
    return _mk(y, a, b, raw, 0.6, today)


def _maybe_year(g: str | None) -> int | None:
    return int(g) if g else None


def _parse_text_date(text: str, day_first: bool, today: date) -> Deadline:
    m = _DMY_TEXT.search(text)
    if m:
        day, mon = int(m.group(1)), MONTHS[m.group(2).lower().rstrip(".")]
        year = int(m.group(3)) if m.group(3) else None
        return _mk(year, mon, day, m.group(0), 0.9, today)

    m = _MDY_TEXT.search(text)
    if m:
        mon, day = MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        return _mk(year, mon, day, m.group(0), 0.9, today)

    m = _DMY_NUM.search(text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if a > 12 and b <= 12:
            return _mk(y, b, a, m.group(0), 0.9, today)   # unambiguous day-first
        if b > 12 and a <= 12:
            return _mk(y, a, b, m.group(0), 0.9, today)   # unambiguous month-first
        if day_first:
            return _mk(y, b, a, m.group(0), 0.6, today)   # a guess, marked as one
        return _mk(y, a, b, m.group(0), 0.6, today)

    return UNKNOWN


def is_rolling(text: str) -> bool:
    """
    True when a board says the post stays open without naming a date.

    These must not count toward the "consecutive expired pages" stop rule,
    because a rolling posting is not expired — the page is not stale.
    """
    return bool(re.search(
        r"(?:rolling|open\s+until\s+filled|until\s+(?:filled|closed)|"
        r"no\s+(?:application\s+)?deadline|applications?\s+reviewed\s+on\s+a\s+"
        r"rolling\s+basis|as\s+long\s+as\s+(?:funds?|budget)\s+(?:last|remain))",
        text, re.IGNORECASE))


# ── The stop rule ──────────────────────────────────────────────────

@dataclass
class PageFreshness:
    """What one listing page looked like, deadline-wise."""
    total: int = 0
    expired: int = 0
    open: int = 0
    unknown: int = 0
    rolling: int = 0

    @property
    def has_live_content(self) -> bool:
        return self.open > 0 or self.rolling > 0

    @property
    def all_expired(self) -> bool:
        """True only when every posting on the page has a known past deadline."""
        return self.total > 0 and self.expired == self.total


def classify_page(postings: list[dict]) -> PageFreshness:
    """Bucket a page's postings by deadline status."""
    f = PageFreshness()
    for p in postings:
        blob = " ".join(str(p.get(k, "")) for k in
                        ("title", "context", "deadline_text", "description"))
        if is_rolling(blob):
            f.rolling += 1
            f.total += 1
            continue
        d = extract_deadline(blob)
        f.total += 1
        if d.status is DeadlineStatus.OPEN:
            f.open += 1
        elif d.status is DeadlineStatus.EXPIRED:
            f.expired += 1
        else:
            f.unknown += 1
    return f


def should_stop_paginating(history: list[PageFreshness], stale_run: int = 4) -> bool:
    """
    Stop once `stale_run` consecutive pages held nothing but expired postings.

    Only pages where *every* posting had a known past deadline count. A page of
    rolling posts, or one where the dates could not be read, is not evidence
    that the rest of the listing is stale — stopping there would throw away live
    positions, which is the exact failure this is meant to prevent.
    """
    if len(history) < stale_run:
        return False
    return all(p.all_expired for p in history[-stale_run:])
