"""
Send the weekly PhD position digest.

Usage:
    python scripts/send_newsletter.py --preview          # render only, no send
    python scripts/send_newsletter.py --send --limit 100  # actually deliver
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from phdiscover.email import (
    EmailService,
    SubscriberStore,
    render_position_newsletter,
    render_welcome_email,
)

POSITIONS = Path("web/data/ranked_opportunities.json")
PREVIEW_DIR = Path("data/preview")


def load_positions() -> list[dict]:
    if not POSITIONS.exists():
        print(f"ERROR: {POSITIONS} not found — run the crawler first.")
        sys.exit(1)
    with open(POSITIONS, encoding="utf-8") as f:
        return json.load(f)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="Actually deliver (default: preview)")
    ap.add_argument("--limit", type=int, default=0, help="Cap recipients")
    ap.add_argument("--welcome", action="store_true", help="Send welcome/confirm email")
    ap.add_argument("--to", default="", help="Send a test to one address only")
    args = ap.parse_args()

    positions = load_positions()
    site = os.getenv("SITE_URL", "https://phdiscover.pages.dev")

    if args.welcome:
        subject, body = render_welcome_email(site)
    else:
        subject, body = render_position_newsletter(positions, site)

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = "welcome" if args.welcome else "digest"
    out = PREVIEW_DIR / f"{stamp}.html"
    out.write_text(body, encoding="utf-8")
    print(f"Subject : {subject}")
    print(f"HTML    : {len(body):,} bytes -> {out}")

    if not args.send:
        print("\nPREVIEW ONLY. Add --send to deliver.")
        return

    # Resolve recipients
    if args.to:
        recipients = [args.to]
    else:
        store = SubscriberStore()
        subs = store.confirmed()
        if args.limit:
            subs = subs[: args.limit]
        recipients = [s.email for s in subs]
        print(f"Subscribers: {len(recipients)} confirmed")

    if not recipients:
        print("No recipients. Nothing sent.")
        return

    service = EmailService.from_env()
    print(f"Providers: {[p.name for p in service.providers]}")

    result = await service.send(recipients, subject, body)
    if result.success:
        print(f"\nSENT via {result.provider} — id={result.message_id}")
    else:
        print(f"\nFAILED: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
