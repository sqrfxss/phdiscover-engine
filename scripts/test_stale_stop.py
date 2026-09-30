"""
End-to-end pagination + stale-page stop, against a fake board served locally.

Builds a listing of N pages where only the first few hold live postings, then
crawls it with the real crawl_http and checks how far it walked.
"""
import asyncio
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import httpx  # noqa: E402
from crawl_v2 import crawl_http  # noqa: E402
from phdiscover.reliability import RetryPolicy  # noqa: E402

PAST = (date.today() - timedelta(days=60)).strftime("%d %b %Y")
LIVE = (date.today() + timedelta(days=60)).strftime("%d %b %Y")
PER_PAGE = 20
TOTAL = 200


def build_page(n: int) -> str:
    """Page n: pages 1-2 live, pages 3+ expired. Real boards sort newest first."""
    deadline = LIVE if n <= 2 else PAST
    rows = []
    for i in range(PER_PAGE):
        idx = (n - 1) * PER_PAGE + i
        rows.append(
            f'<li class="job"><a href="/jobs/phd-candidate-biomechanics-{idx}">'
            f'<h3>PhD Candidate in Biomechanics of Movement {idx}</h3></a>'
            f'<p>University of Test {idx % 20} — doctoral position in gait '
            f'analysis and human movement biomechanics, sensorimotor control. '
            f'Deadline: {deadline}</p></li>')
    nxt = ""
    if n < TOTAL // PER_PAGE:
        nxt = (f'<a href="/jobs?page={n + 1}">Next</a>'
               f'<a href="/jobs?page={n + 1}">{n + 1}</a>')
    return (f'<html><body><ul class="jobs">{"".join(rows)}</ul>'
            f'<nav>{nxt}</nav></body></html>')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        page = int(parse_qs(u.query).get("page", ["1"])[0])
        body = build_page(page).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:52} got={got} want={want}")


def main():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}/jobs"

    async def go():
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     trust_env=False) as c:
            src = {"name": "fakeboard", "url": base, "search_url": base,
                   "label": "Fake", "max_pages": 20, "stale_page_run": 4}
            return await crawl_http(src, c, c)

    items, note = asyncio.run(go())
    srv.shutdown()

    print("=== crawler walked a board with 2 live pages then 8 expired ===\n")
    print(f"  note: {note}\n")

    check("stale rule fires, not max_pages", "all-expired" in note, True)
    check("stopped at 6 pages (2 live + 4 stale)", "6 page(s)" in note, True)
    # 6 pages x 20 rows were read. The 80 expired rows are kept because the
    # freshness verdict needs them — the filter stage drops them, not the crawl.
    check("read all 6 pages", len(items), 120)
    check("all 6 pages were read", {i.page_no for i in items} == {1, 2, 3, 4, 5, 6},
          True)

    # The crawl keeps expired rows as evidence; it is the filter that must drop
    # them. Verify the signal the filter will use is actually there.
    live = [i for i in items if i.deadline_status == "open"]
    dead = [i for i in items if i.deadline_status == "expired"]
    check("40 live postings found", len(live), 40)
    check("80 expired postings found", len(dead), 80)
    check("live ones are exactly pages 1-2",
          sorted({i.page_no for i in live}), [1, 2])
    check("expired ones are exactly pages 3-6",
          sorted({i.page_no for i in dead}), [3, 4, 5, 6])
    check("no live posting was wrongly marked expired",
          all(not i.deadline_date or i.deadline_date > date.today().isoformat()
              for i in live), True)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if sum(results) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
