"""Do these boards paginate, and how deep do they go?"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TARGETS = [
    ("researchjobseurope", "https://www.researchjobseurope.com/jobs"),
    ("jobrxiv", "https://jobrxiv.com/jobs/"),
    ("jobs_ac_uk", "https://www.jobs.ac.uk/jobs/"),
    ("postdocjobs", "https://www.postdocjobs.com/jobs/"),
    ("thepostdoc", "https://www.thepostdoc.com/jobs"),
    ("academictransfer", "https://www.academictransfer.com/jobs/"),
    ("biosciencecareers", "https://www.biosciencecareers.com/jobs"),
    ("jobs_ch", "https://www.jobs.ch/en/vacancies/"),
    ("nature_careers", "https://www.nature.com/careers/jobs"),
]

PAGE_RE = re.compile(r"[?&](page|p|offset|start)=|/\d+/|/page/\d+", re.I)


async def main():
    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True, trust_env=False,
        headers={"User-Agent": UA},
    ) as c:
        for name, url in TARGETS:
            try:
                r = await c.get(url)
            except Exception as e:  # noqa: BLE001
                print(f"{name:20} ERR {type(e).__name__}: {str(e)[:40]}")
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            anchors = soup.find_all("a", href=True)

            pages: list[tuple[str, str]] = []
            seen: set[str] = set()
            for a in anchors:
                h = a["href"]
                if PAGE_RE.search(h) and h not in seen:
                    seen.add(h)
                    pages.append((a.get_text(" ", strip=True)[:12], h[:76]))

            # How many job links are on this single page?
            jobish = [a for a in anchors
                      if re.search(r"/job|/position|/vacanc|/opening", a["href"], re.I)]

            print(f"{name:20} {r.status_code} {len(r.text):>8,}b  "
                  f"anchors={len(anchors):4}  jobish={len(jobish):3}  "
                  f"page_links={len(pages)}")
            for t, h in pages[:5]:
                print(f"      {t:12} {h}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
