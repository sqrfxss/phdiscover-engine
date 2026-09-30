"""Prove pagination discovery works against the four URL shapes boards use."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from crawl_v2 import next_page_url

CASES = [
    # (name, html, current_url, expected)
    ("query string ?page=2",
     '<a href="?page=2">Next</a>',
     "https://x.com/jobs", "https://x.com/jobs?page=2"),

    ("path segment /page/2/",
     '<a href="/page/2/">2</a>',
     "https://x.com/jobs", "https://x.com/page/2/"),

    ("offset parameter",
     '<a href="?offset=20">Next</a>',
     "https://x.com/jobs", "https://x.com/jobs?offset=20"),

    ("index with query",
     '<a href="/job/job/index?page=2">More Feature</a>',
     "https://x.com/jobs", "https://x.com/job/job/index?page=2"),

    ("relative next text",
     '<a href="/jobs">Next &raquo;</a>',
     "https://x.com/", "https://x.com/jobs"),

    ("german 'mehr'",
     '<a href="?page=2">Mehr</a>',
     "https://x.de/jobs", "https://x.de/jobs?page=2"),

    ("absolute next link",
     '<a href="https://x.com/jobs?page=2">Next</a>',
     "https://x.com/jobs", "https://x.com/jobs?page=2"),

    # Must NOT return anything
    ("no pagination at all",
     '<a href="/jobs/1">PhD in Biomechanics</a><a href="/about">About</a>',
     "https://x.com/jobs", None),

    ("self-link is not next",
     '<a href="https://x.com/jobs">Next</a>',
     "https://x.com/jobs", None),

    ("empty page param is not next",
     '<a href="?page=">Next</a>',
     "https://x.com/jobs", None),

    # Measured on researchjobseurope.com: the "Next" link on page 3 points back
    # to page 2. Following it would loop until max_pages with the same rows.
    ("next pointing backwards is rejected",
     '<a href="?page=2">Next</a>',
     "https://x.com/jobs?page=3", None),

    ("next pointing to the same page is rejected",
     '<a href="?page=4">Next</a>',
     "https://x.com/jobs?page=4", None),

    ("forward next from a numbered page is allowed",
     '<a href="?page=5">Next</a>',
     "https://x.com/jobs?page=4", "https://x.com/jobs?page=5"),

    # Measured on jobs.ac.uk: the pager on /jobs/phd links to /search/?..., a
    # different search mode. Once paginating a numbered page it must keep the
    # same path shape, or the "phd" filter is silently dropped.
    ("pager switching to a different search path is rejected",
     '<a href="/search/?sortOrder=1&pageSize=25">Next</a>',
     "https://x.com/jobs/phd?page=1", None),

    ("first pager on an unnumbered URL is followed",
     '<a href="/page/2/">Next</a>',
     "https://x.com/jobs", "https://x.com/page/2/"),

    ("pager within a path segment is followed",
     '<a href="/jobs/phd?page=2">Next</a>',
     "https://x.com/jobs/phd", "https://x.com/jobs/phd?page=2"),

    ("/page/N/ segment within the same path is followed",
     '<a href="/jobs/page/3/">Next</a>',
     "https://x.com/jobs/page/2/", "https://x.com/jobs/page/3/"),
]


def main():
    passed = 0
    print("=== pagination discovery ===")
    for name, html, current, want in CASES:
        got = next_page_url(html, current)
        ok = got == want
        passed += ok
        shown = got if got else "None"
        print(f"  {'PASS' if ok else 'FAIL'}  {name:32} -> {str(shown)[:44]}")
        if not ok:
            print(f"        expected: {want}")

    print(f"\n{passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
