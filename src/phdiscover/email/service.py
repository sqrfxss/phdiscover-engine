"""
PhDiscover — Email Delivery System

Zero-cost email delivery with provider failover.
Primary: Resend (3,000 emails/month free) — best deliverability for transactional.
Fallback: Brevo (300 emails/day free) — good for campaigns.

Iran reachability (verified 2026-09): Resend, Brevo, Buttondown, EmailOctopus all
reachable; Mailchimp is NOT (503). Provider list is ordered accordingly.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any, Optional

import httpx


@dataclass
class EmailResult:
    """Outcome of a single send attempt."""
    success: bool
    provider: str
    message_id: str = ""
    error: str = ""
    recipients: list[str] = field(default_factory=list)


@dataclass
class Subscriber:
    """A newsletter subscriber."""
    email: str
    name: str = ""
    country: str = ""
    interests: list[str] = field(default_factory=list)
    subscribed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    confirmed: bool = False
    unsubscribed: bool = False
    source: str = ""


class EmailProvider:
    """Base class for email providers."""

    name: str = "base"

    async def send(self, to: list[str], subject: str, html_body: str,
                   from_name: str, reply_to: str = "") -> EmailResult:
        raise NotImplementedError


class ResendProvider(EmailProvider):
    """Resend — 3,000 emails/month free, excellent deliverability."""

    name = "resend"

    def __init__(self, api_key: str, domain: str = ""):
        self.api_key = api_key
        self.domain = domain
        self.url = "https://api.resend.com/emails"

    async def send(self, to, subject, html_body, from_name, reply_to="") -> EmailResult:
        payload: dict[str, Any] = {
            "from": f"{from_name} <{self.domain or 'onboarding@resend.dev'}>",
            "to": to,
            "subject": subject,
            "html": html_body,
        }
        if reply_to:
            payload["reply_to"] = reply_to

        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            try:
                r = await client.post(
                    self.url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if r.status_code in (200, 201):
                    data = r.json()
                    return EmailResult(
                        success=True,
                        provider=self.name,
                        message_id=data.get("id", ""),
                        recipients=to,
                    )
                return EmailResult(
                    success=False, provider=self.name,
                    error=f"{r.status_code}: {r.text[:200]}", recipients=to,
                )
            except Exception as e:
                return EmailResult(
                    success=False, provider=self.name,
                    error=f"{type(e).__name__}: {e}", recipients=to,
                )


class BrevoProvider(EmailProvider):
    """Brevo (formerly Sendinblue) — 300 emails/day free."""

    name = "brevo"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.brevo.com/v3/smtp/email"

    async def send(self, to, subject, html_body, from_name, reply_to="") -> EmailResult:
        sender = os.getenv("EMAIL_FROM", "noreply@phdiscover.io")
        payload: dict[str, Any] = {
            "sender": {"name": from_name, "email": sender},
            "to": [{"email": e} for e in to],
            "subject": subject,
            "htmlContent": html_body,
        }
        if reply_to:
            payload["replyTo"] = {"email": reply_to}

        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            try:
                r = await client.post(
                    self.url,
                    json=payload,
                    headers={
                        "api-key": self.api_key,
                        "Content-Type": "application/json",
                    },
                )
                if r.status_code in (200, 201):
                    return EmailResult(
                        success=True, provider=self.name,
                        message_id=str(r.json().get("messageId", "")), recipients=to,
                    )
                return EmailResult(
                    success=False, provider=self.name,
                    error=f"{r.status_code}: {r.text[:200]}", recipients=to,
                )
            except Exception as e:
                return EmailResult(
                    success=False, provider=self.name,
                    error=f"{type(e).__name__}: {e}", recipients=to,
                )


class SMTPProvider(EmailProvider):
    """
    Direct SMTP fallback.

    Useful from Iran where a local mailbox (e.g. a free Gmail/Outlook account or
    an Iranian provider) can relay mail without a paid API. Deliberately uses
    synchronous smtplib inside a thread so the async interface is preserved.
    """

    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str,
                 use_tls: bool = True):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls

    async def send(self, to, subject, html_body, from_name, reply_to="") -> EmailResult:
        def _send() -> tuple[bool, str]:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = formataddr((from_name, self.username))
                msg["To"] = ", ".join(to)
                msg["Date"] = formatdate(localtime=True)
                if reply_to:
                    msg["Reply-To"] = reply_to
                msg.attach(MIMEText(html_body, "html"))

                with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                    if self.use_tls:
                        s.starttls()
                    s.login(self.username, self.password)
                    s.sendmail(self.username, to, msg.as_string())
                return True, ""
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"

        ok, err = await asyncio.to_thread(_send)
        return EmailResult(
            success=ok, provider=self.name,
            message_id="" if ok else err, error=err, recipients=to,
        )


class EmailService:
    """
    Multi-provider email dispatcher with automatic failover.

    Tries providers in order; on failure moves to the next. Records every attempt
    so a dead domain does not silently swallow a campaign.
    """

    def __init__(self, providers: list[EmailProvider], from_name: str = "PhDiscover"):
        if not providers:
            raise ValueError("At least one email provider is required")
        self.providers = providers
        self.from_name = from_name
        self.log_path = Path("data/email_log.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "EmailService":
        """Build the provider chain from environment variables."""
        providers: list[EmailProvider] = []

        # Ordered by Iran reachability and free-tier generosity.
        if key := os.getenv("RESEND_API_KEY"):
            providers.append(ResendProvider(key, os.getenv("EMAIL_FROM", "")))

        if key := os.getenv("BREVO_API_KEY"):
            providers.append(BrevoProvider(key))

        if host := os.getenv("SMTP_HOST"):
            providers.append(SMTPProvider(
                host=host,
                port=int(os.getenv("SMTP_PORT", "587")),
                username=os.getenv("SMTP_USER", ""),
                password=os.getenv("SMTP_PASSWORD", ""),
                use_tls=os.getenv("SMTP_TLS", "true").lower() == "true",
            ))

        if not providers:
            raise ValueError(
                "No email provider configured. Set RESEND_API_KEY, BREVO_API_KEY, "
                "or SMTP_HOST/SMTP_USER/SMTP_PASSWORD in .env"
            )
        return cls(providers)

    async def send(
        self, to: list[str], subject: str, html_body: str, reply_to: str = "",
    ) -> EmailResult:
        """Send an email, failing over across providers until one succeeds."""
        last_error = ""
        for provider in self.providers:
            result = await provider.send(to, subject, html_body, self.from_name, reply_to)
            self._log(result, subject)
            if result.success:
                return result
            last_error = f"{provider.name}: {result.error}"
        return EmailResult(
            success=False, provider="all", error=last_error, recipients=to,
        )

    async def send_broadcast(
        self, subscribers: list[Subscriber], subject: str, html_body: str,
        batch_size: int = 50, delay: float = 2.0,
    ) -> list[EmailResult]:
        """Send a campaign to many subscribers in batches with rate limiting."""
        active = [s for s in subscribers if s.confirmed and not s.unsubscribed]
        results: list[EmailResult] = []

        for i in range(0, len(active), batch_size):
            batch = active[i: i + batch_size]
            result = await self.send(
                [s.email for s in batch], subject, html_body,
                reply_to=os.getenv("EMAIL_REPLY_TO", ""),
            )
            results.append(result)
            print(f"  batch {i // batch_size + 1}: {len(batch)} recipients "
                  f"-> {'OK' if result.success else 'FAIL'} ({result.provider})")
            if i + batch_size < len(active):
                await asyncio.sleep(delay)

        return results

    def _log(self, result: EmailResult, subject: str) -> None:
        """Append-only delivery log — never overwrite history."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": result.provider,
            "success": result.success,
            "message_id": result.message_id,
            "recipients": result.recipients,
            "subject": subject,
            "error": result.error,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Subscriber storage — JSON for now; swap to Postgres/Supabase when scaling.
# ---------------------------------------------------------------------------

class SubscriberStore:
    """Append-safe subscriber list stored as JSON."""

    def __init__(self, path: str = "data/subscribers.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._subscribers: dict[str, Subscriber] = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for row in json.load(f):
                    sub = Subscriber(**row)
                    self._subscribers[sub.email.lower()] = sub

    def add(self, email: str, name: str = "", country: str = "",
            interests: list[str] | None = None, source: str = "") -> Subscriber:
        """Add or update a subscriber. Idempotent on email."""
        key = email.strip().lower()
        if key in self._subscribers:
            sub = self._subscribers[key]
            sub.name = name or sub.name
            sub.country = country or sub.country
            if interests:
                sub.interests = sorted(set(sub.interests) | set(interests))
            sub.unsubscribed = False
        else:
            sub = Subscriber(
                email=key, name=name, country=country,
                interests=interests or [], source=source,
            )
        self._subscribers[key] = sub
        self.save()
        return sub

    def unsubscribe(self, email: str) -> bool:
        key = email.strip().lower()
        if key not in self._subscribers:
            return False
        self._subscribers[key].unsubscribed = True
        self.save()
        return True

    def confirmed(self) -> list[Subscriber]:
        return [s for s in self._subscribers.values() if s.confirmed and not s.unsubscribed]

    def all(self) -> list[Subscriber]:
        return list(self._subscribers.values())

    def save(self) -> None:
        data = [
            {
                "email": s.email, "name": s.name, "country": s.country,
                "interests": s.interests, "subscribed_at": s.subscribed_at,
                "confirmed": s.confirmed, "unsubscribed": s.unsubscribed,
                "source": s.source,
            }
            for s in self._subscribers.values()
        ]
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def stats(self) -> dict[str, Any]:
        subs = list(self._subscribers.values())
        return {
            "total": len(subs),
            "confirmed": len([s for s in subs if s.confirmed]),
            "unsubscribed": len([s for s in subs if s.unsubscribed]),
            "pending": len([s for s in subs if not s.confirmed and not s.unsubscribed]),
            "by_country": _count_by(s.country for s in subs if s.country),
        }


def _count_by(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# Newsletter rendering
# ---------------------------------------------------------------------------

def render_position_newsletter(
    positions: list[dict[str, Any]], site_url: str = "https://phdiscover.pages.dev",
) -> tuple[str, str]:
    """Render the weekly digest. Returns (subject, html_body)."""
    high = [p for p in positions if p.get("contact_priority") == "HIGH"]
    top = positions[:12]
    subject = f"{len(top)} new PhD positions in biomechanics & human movement"

    rows = []
    for p in top:
        fit = p.get("research_fit_score", 0)
        priority = p.get("contact_priority", "LOW")
        badge = {"HIGH": "#16a34a", "MEDIUM": "#ca8a04", "LOW": "#64748b"}[priority]
        rows.append(f"""
        <tr>
          <td style="padding:16px;border-bottom:1px solid #e2e8f0;">
            <div style="font-size:11px;font-weight:700;color:{badge};letter-spacing:.5px;">
              {priority} · FIT {fit}/100
            </div>
            <a href="{p.get('url', site_url)}" style="color:#0f172a;font-size:16px;
               font-weight:600;text-decoration:none;line-height:1.4;display:block;
               margin:4px 0;">{html.escape(p.get('title', 'Untitled'))}</a>
            <div style="font-size:13px;color:#475569;">
              {html.escape(p.get('university', 'N/A'))} · {html.escape(p.get('country', 'N/A'))}
              {('· posted ' + html.escape(str(p.get('posted')))) if p.get('posted') else ''}
            </div>
          </td>
        </tr>""")

    body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,
  'Segoe UI',Tahoma,sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:32px 16px;">

  <div style="background:#2563eb;color:#fff;padding:24px;border-radius:12px;">
    <div style="font-size:20px;font-weight:700;">🎓 PhDiscover</div>
    <div style="font-size:14px;opacity:.9;margin-top:4px;">
      PI-first PhD position discovery · evidence-backed
    </div>
  </div>

  <div style="background:#fff;border-radius:12px;padding:24px;margin-top:16px;
       border:1px solid #e2e8f0;">
    <h1 style="margin:0 0 8px;font-size:20px;color:#0f172a;">
      {len(top)} new PhD positions this week
    </h1>
    <p style="margin:0 0 20px;color:#475569;font-size:14px;">
      {len(high)} high-priority match{'' if len(high) == 1 else 'es'} for your
      fingerprint (biomechanics · sensorimotor · ML on human movement).
    </p>
    <table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>
  </div>

  <div style="text-align:center;margin-top:24px;">
    <a href="{site_url}"
       style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;
              padding:12px 28px;border-radius:8px;font-weight:600;font-size:14px;">
      Search all positions
    </a>
  </div>

  <div style="text-align:center;margin-top:32px;color:#94a3b8;font-size:12px;line-height:1.7;">
    Every listing links to its official source — no invented positions or emails.<br>
    <a href="{site_url}/unsubscribe" style="color:#94a3b8;">Unsubscribe</a>
  </div>

</div>
</body></html>"""
    return subject, body


def render_welcome_email(site_url: str = "https://phdiscover.pages.dev") -> tuple[str, str]:
    """Double-opt-in welcome email."""
    subject = "Confirm your PhDiscover subscription"
    body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,
  'Segoe UI',Tahoma,sans-serif;">
<div style="max-width:560px;margin:0 auto;padding:32px 16px;">
  <div style="background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    <div style="font-size:22px;font-weight:700;color:#0f172a;margin-bottom:12px;">
      🎓 PhDiscover
    </div>
    <p style="color:#334155;font-size:15px;line-height:1.7;">
      One click and your weekly digest of funded PhD positions in biomechanics,
      human movement, and sensorimotor research is on its way.
    </p>
    <p style="color:#334155;font-size:15px;line-height:1.7;">
      Every listing is pulled from an official source and ranked against your
      research fingerprint — no invented supervisors, no dead links.
    </p>
    <div style="text-align:center;margin:28px 0;">
      <a href="{site_url}/confirm?token=YOUR_TOKEN"
         style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;
                padding:13px 32px;border-radius:8px;font-weight:600;">
        Confirm subscription
      </a>
    </div>
    <p style="color:#94a3b8;font-size:12px;text-align:center;">
      Didn't request this? Ignore this email — nothing will be sent.
    </p>
  </div>
</div>
</body></html>"""
    return subject, body
