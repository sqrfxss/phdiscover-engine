"""
Board-specific parsers for high-traffic university career portals.

These are tried before the generic harvest() and return List[Found] or [].
"""

from __future__ import annotations

from urllib.parse import urljoin
from bs4 import BeautifulSoup

from phdiscover.crawlers.base import Found  # uses the same dataclass


def _parse_utoronto(html: str, base: str, source: str, label: str) -> list:
    """careers.utoronto.ca uses a search-results grid with data-jobid attrs."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("[data-jobid]"):
        href = card.get("href")
        if not href:
            link = card.select_one("a[href]")
            if link:
                href = link.get("href")
        if not href:
            continue
        title = card.select_one("[data-job-title]")
        title = title.get_text(strip=True) if title else card.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        ctx = card.get_text(" ", strip=True)[:700]
        out.append(Found(url=urljoin(base, href), title=title[:180],
                         context=ctx, source=source, source_label=label))
    return out


def _parse_lund(html: str, base: str, source: str, label: str) -> list:
    """lu.se/vacancies uses varbi.com embed or direct job list."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='varbi.com']"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        out.append(Found(url=href, title=title[:180], context="",
                         source=source, source_label=label))
    if not out:
        for a in soup.select("a[href*='/job/'], a[href*='/vacanc']"):
            href = a.get("href")
            title = a.get_text(strip=True)
            if title and len(title) > 10:
                out.append(Found(url=urljoin(base, href), title=title[:180],
                                 context="", source=source, source_label=label))
    return out


def _parse_polimi(html: str, base: str, source: str, label: str) -> list:
    """polimi.it uses /en/phd/ for PhD programme listings."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='/en/phd/']"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        out.append(Found(url=urljoin(base, href), title=title[:180],
                         context=a.get_text(" ", strip=True)[:700],
                         source=source, source_label=label))
    return out


def _parse_helsinki(html: str, base: str, source: str, label: str) -> list:
    """helsinki.fi doctoral programmes are in a structured grid."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='/admissions-and-education/apply-doctoral']"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        out.append(Found(url=urljoin(base, href), title=title[:180],
                         context=a.get_text(" ", strip=True)[:700],
                         source=source, source_label=label))
    return out


def _parse_uzh(html: str, base: str, source: str, label: str) -> list:
    """jobs.uzh.ch uses a standard job list with data attributes."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".job-item, [data-job-id], .vacancy-item"):
        a = card.select_one("a[href]")
        if not a:
            continue
        href = a.get("href")
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        ctx = card.get_text(" ", strip=True)[:700]
        out.append(Found(url=urljoin(base, href), title=title[:180],
                         context=ctx, source=source, source_label=label))
    return out


def _parse_rwth(html: str, base: str, source: str, label: str) -> list:
    """jobs.rwth-aachen.de uses a table with job links."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='/stellenangebote/'], a[href*='/job/']"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        out.append(Found(url=urljoin(base, href), title=title[:180],
                         context=a.get_text(" ", strip=True)[:700],
                         source=source, source_label=label))
    return out


def _parse_ku_phd(html: str, base: str, source: str, label: str) -> list:
    """jobportal.ku.dk/phd/ has PhD listings at /phd/?show=XXXXX."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    # The actual PhD position listings are at /phd/?show=XXXXX
    for a in soup.select("a[href*='/phd/?show=']"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        out.append(Found(url=urljoin(base, href), title=title[:180],
                         context=a.get_text(" ", strip=True)[:700],
                         source=source, source_label=label))
    return out


# Hostname -> parser mapping
BOARD_PARSERS = {
    "careers.utoronto.ca": _parse_utoronto,
    "lu.se": _parse_lund,
    "careers.ualberta.ca": _parse_lund,
    "polimi.it": _parse_polimi,
    "www.helsinki.fi": _parse_helsinki,
    "jobs.uzh.ch": _parse_uzh,
    "jobs.rwth-aachen.de": _parse_rwth,
    "phd.ku.dk": _parse_ku_phd,
}