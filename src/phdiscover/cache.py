"""
Persistent per-source cache with merge-on-read.

The problem this solves: `applykite` returned 19 postings in one run and timed
out in the next. Without a cache, one flaky network moment silently deletes
those 19 positions from the site.

The rule here is asymmetric on purpose:

  - A source that returned results    → its cache becomes the fresh record.
  - A source that failed or timed out → its CACHED results are kept.

A failure never erases good data. The only thing a failed run can do is add
nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path("F:/hermes/phdiscover-engine")
CACHE = ROOT / "data" / "cache" / "positions_by_source.json"

# A cached record older than this is shown but flagged, never silently served.
STALE_AFTER = timedelta(days=21)


class PositionCache:
    def __init__(self, path: Path = CACHE):
        self.path = path
        self.data: dict[str, Any] = {}
        self.dirty = False
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.data = {"version": 1, "sources": {}}
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt cache must never block a crawl.
            self.data = {"version": 1, "sources": {}}
        self.data.setdefault("sources", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)          # atomic: never leave a half-written file

    # ── read ───────────────────────────────────────────────────────

    def get(self, source: str) -> tuple[list[dict], int, str | None]:
        """
        Return (positions, age_days, fetched_at).

        age_days is -1 and fetched_at is None when the source has been tried
        but has never completed a successful crawl.
        """
        rec = self.data["sources"].get(source)
        if not rec or not rec.get("fetched_at"):
            return [], -1, None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(rec["fetched_at"])
        return rec.get("positions", []), age.days, rec["fetched_at"]

    def is_stale(self, source: str) -> bool:
        rec = self.data["sources"].get(source)
        if not rec or not rec.get("fetched_at"):
            return True
        age = datetime.now(timezone.utc) - datetime.fromisoformat(rec["fetched_at"])
        return age > STALE_AFTER

    # ── write ──────────────────────────────────────────────────────

    def record_success(self, source: str, positions: list[dict]) -> None:
        self.data["sources"][source] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count": len(positions),
            "positions": positions,
        }
        self.dirty = True

    def record_failure(self, source: str, reason: str) -> None:
        """Log a failure WITHOUT touching the stored positions."""
        rec = self.data["sources"].get(source)
        if rec is None:
            # Never crawled successfully before — remember the failure so the
            # report can say "tried, never worked" rather than "not tried".
            self.data["sources"][source] = {
                "fetched_at": None,
                "count": 0,
                "positions": [],
                "last_error": reason,
            }
        else:
            rec["last_error"] = reason
            rec["last_error_at"] = datetime.now(timezone.utc).isoformat()
        self.dirty = True

    # ── merge ──────────────────────────────────────────────────────

    def merge_into(
        self,
        fresh_positions: list[dict],
        per_source: dict[str, dict],
    ) -> tuple[list[dict], list[dict]]:
        """
        Combine this run's results with the cache.

        Returns (merged_positions, notes) where notes describe every source whose
        data came from the cache rather than this run, so the report never claims
        a stale figure is fresh.
        """
        fresh_by_source: dict[str, list[dict]] = {}
        for p in fresh_positions:
            fresh_by_source.setdefault(p.get("source", "?"), []).append(p)

        all_sources = set(per_source) | set(self.data["sources"])
        merged: list[dict] = []
        notes: list[dict] = []

        for source in sorted(all_sources):
            info = per_source.get(source, {})
            succeeded = info.get("n", 0) > 0
            skipped = info.get("method") == "skipped"

            if succeeded or skipped:
                merged.extend(fresh_by_source.get(source, []))
                continue

            # This source returned nothing. Was that a failure or a real zero?
            cached, age, fetched_at = self.get(source)
            reason = info.get("note", "no note")
            if not cached or fetched_at is None:
                # Never had a successful crawl, so there is nothing to preserve.
                continue

            stale = self.is_stale(source)
            for p in cached:
                q = dict(p)
                q["from_cache"] = True
                q["cache_age_days"] = age
                q["cache_fetched_at"] = fetched_at
                merged.append(q)

            notes.append({
                "source": source,
                "reason": reason,
                "cached_positions": len(cached),
                "cache_age_days": age,
                "cache_fetched_at": fetched_at,
                "stale": stale,
            })

        return merged, notes

    def stats(self) -> dict[str, Any]:
        sources = self.data["sources"]
        with_data = {k: v for k, v in sources.items() if v.get("count", 0) > 0}
        never = {k: v for k, v in sources.items() if not v.get("fetched_at")}
        return {
            "sources_tracked": len(sources),
            "sources_with_data": len(with_data),
            "sources_never_succeeded": sorted(never),
            "total_cached_positions": sum(v.get("count", 0) for v in sources.values()),
        }
