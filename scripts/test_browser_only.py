"""Quick browser-only test on the sources that failed the selector bug."""
import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from crawl_v2 import crawl_browser, open_browser  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAMES = sys.argv[1:] or ["euraxess", "academicpositions", "eluta", "nwo", "kth"]


async def main() -> None:
    with open(ROOT / "config" / "sources.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sources = {s["name"]: s for s in cfg["sources"]}
    targets = [sources[n] for n in NAMES if n in sources]

    pw, br = await open_browser()
    try:
        for src in targets:
            items, note = await crawl_browser(src, br)
            print(f"  {src['name']:24} {len(items):3}  {note}")
            for it in items[:3]:
                print(f"       - {it.title[:64]}")
                print(f"         {it.url[:100]}")
            await asyncio.sleep(2)
    finally:
        await br.close()
        await pw.stop()


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
