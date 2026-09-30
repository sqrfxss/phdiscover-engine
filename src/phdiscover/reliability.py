"""
Retry with exponential backoff and jitter.

The Iranian network drops connections without warning: a board that returned 25
postings last run times out this one, then works again an hour later. A single
attempt turns a transient failure into a permanent gap in the data.

Retries here are per-source and only cover failures that are plausibly
transient. A 403 from a bot wall is not retried — the answer will be identical
every time, and hammering the host makes the block worse.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

# Failures worth a second attempt: the connection died, not the answer.
TRANSIENT_MARKERS = (
    "timed out", "timeout", "err_timed_out", "err_connection",
    "connecterror", "connecttimeout", "readtimeout", "writetimeout",
    "readerror", "remotedisconnected", "connectionreset", "connection aborted",
    "econnreset", "econnrefused", "etimedout", "temporary failure",
    "network is unreachable", "ssl: wrong_version", "handshake",
    "service unavailable", "bad gateway", "gateway timeout", "502", "503", "504",
)

# Failures that will not change on a retry.
PERMANENT_MARKERS = (
    "403", "401", "404", "forbidden", "not found", "unauthorized",
    "err_cert", "certificate", "getaddrinfo", "name or service not known",
    "no such host", "blocked_bot_wall",
)


def is_transient(text: str) -> bool:
    """Decide whether a failure is worth retrying."""
    low = text.lower()
    # Certificate and DNS problems are permanent here; check them first because
    # "ssl" also appears in some transient handshake errors.
    if any(m in low for m in ("err_cert", "certificate", "getaddrinfo",
                              "no such host", "name or service not known")):
        return False
    if any(m in low for m in ("403", "401", "forbidden", "unauthorized", "404")):
        return False
    return any(m in low for m in TRANSIENT_MARKERS)


@dataclass
class RetryPolicy:
    attempts: int = 3            # total tries, not extra tries
    base_delay: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.4          # fraction of the delay to randomise

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full-width jitter.

        Without jitter, two sources that failed together retry together and fail
        together again.
        """
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        spread = raw * self.jitter
        return raw - spread + random.random() * spread * 2


DEFAULT = RetryPolicy()


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT,
    label: str = "",
    on_retry=None,
) -> tuple[T | None, str]:
    """
    Run `fn` with retries. Returns (result, final_note).

    `fn` returns (value, note) so a caller can treat "reachable but zero
    results" as a success — that is a fact about the page, not a failure.
    """
    last_note = ""
    for attempt in range(1, policy.attempts + 1):
        try:
            result, note = await fn()
            if attempt > 1 and on_retry:
                on_retry(label, attempt, note)
            return result, note
        except Exception as e:  # noqa: BLE001
            last_note = f"{type(e).__name__}: {str(e)[:70]}"
        else:  # pragma: no cover
            break

        if attempt == policy.attempts:
            break
        if not is_transient(last_note):
            break
        wait = policy.delay_for(attempt)
        if on_retry:
            on_retry(label, attempt, f"retrying in {wait:.1f}s ({last_note})")
        await asyncio.sleep(wait)

    return None, last_note
