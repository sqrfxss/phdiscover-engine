"""
PhDiscover Engine - Crawlers Package
"""

from __future__ import annotations

from phdiscover.crawlers.base import BaseCrawler, UniversityPortalCrawler
from phdiscover.crawlers.euraxess import EuraxessCrawler
from phdiscover.crawlers.findaphd import FindAPhDCrawler
from phdiscover.crawlers.academicpositions import AcademicPositionsCrawler
from phdiscover.crawlers.openalex import OpenAlexCrawler

__all__ = [
    "BaseCrawler",
    "UniversityPortalCrawler",
    "EuraxessCrawler",
    "FindAPhDCrawler",
    "AcademicPositionsCrawler",
    "OpenAlexCrawler",
]