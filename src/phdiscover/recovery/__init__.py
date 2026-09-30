"""
PhDiscover Engine - Recovery System
Autonomous failure classification and recovery
"""

from __future__ import annotations

import asyncio
import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from phdiscover.agents.base import FailureType
from phdiscover.config import get_settings

logger = structlog.get_logger(__name__)


class RecoveryAgent:
    """Autonomous recovery agent with persistent solution store"""

    def __init__(self):
        self.settings = get_settings()
        self.db_path = Path(self.settings.recovery.sqlite_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

        # Recovery methods ordered by success probability
        self.recovery_methods = [
            ("retry_with_backoff", self._retry_with_backoff, 0.9),
            ("rotate_user_agent", self._rotate_user_agent, 0.7),
            ("use_proxy", self._use_proxy, 0.6),
            ("simplify_request", self._simplify_request, 0.5),
            ("switch_to_playwright", self._switch_to_playwright, 0.6),
            ("try_alternative_url", self._try_alternative_url, 0.5),
            ("use_cached_version", self._use_cached_version, 0.4),
            ("search_api_fallback", self._search_api_fallback, 0.5),
            ("delay_and_retry", self._delay_and_retry, 0.3),
        ]

    async def initialize(self):
        """Initialize recovery database"""
        if self._initialized:
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recovery_solutions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    failure_type TEXT NOT NULL,
                    url_pattern TEXT,
                    solution_method TEXT NOT NULL,
                    solution_details TEXT,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    last_used_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_name, failure_type, url_pattern, solution_method)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_recovery_lookup
                ON recovery_solutions(source_name, failure_type)
            """)
            await db.commit()

        self._initialized = True
        logger.info("recovery_db_initialized", path=str(self.db_path))

    async def classify_and_recover(
        self,
        source_name: str,
        url: str,
        error: Exception,
        attempt_func,
        *args,
        max_attempts: int = 3,
        **kwargs,
    ) -> Any:
        """Classify failure and attempt recovery"""
        await self.initialize()

        failure_type = self._classify_failure(error)
        logger.warning(
            "failure_detected",
            source=source_name,
            url=url,
            failure_type=failure_type.value,
            error=str(error),
        )

        # Check for known solutions
        known_solutions = await self._get_known_solutions(source_name, failure_type, url)
        if known_solutions:
            for solution in known_solutions:
                try:
                    result = await self._apply_solution(solution, attempt_func, *args, **kwargs)
                    if result is not None:
                        await self._record_solution_result(solution["id"], True)
                        return result
                except Exception as e:
                    await self._record_solution_result(solution["id"], False)
                    logger.warning("known_solution_failed", solution=solution["solution_method"], error=str(e))

        # Try standard recovery methods
        for method_name, method_func, probability in self.recovery_methods:
            if probability < 0.3:  # Skip low-probability methods
                continue

            try:
                logger.info("trying_recovery_method", method=method_name)
                result = await method_func(method_name, source_name, failure_type, url, attempt_func, *args, **kwargs)
                if result is not None:
                    await self._store_solution(source_name, failure_type, url, method_name, {}, True)
                    return result
            except Exception as e:
                logger.warning("recovery_method_failed", method=method_name, error=str(e))
                await self._store_solution(source_name, failure_type, url, method_name, {}, False)
                continue

        # All methods failed
        logger.error(
            "all_recovery_methods_failed",
            source=source_name,
            url=url,
            failure_type=failure_type.value,
        )
        raise error

    def _classify_failure(self, error: Exception) -> FailureType:
        """Classify failure type"""
        error_str = str(error).lower()

        if any(kw in error_str for kw in ["timeout", "timed out", "read timeout"]):
            return FailureType.TIMEOUT
        elif any(kw in error_str for kw in ["rate limit", "429", "too many requests"]):
            return FailureType.RATE_LIMIT
        elif any(kw in error_str for kw in ["robots.txt", "disallowed", "403 forbidden"]):
            return FailureType.ROBOTS_BLOCK
        elif any(kw in error_str for kw in ["javascript", "js required", "dynamic", "spa"]):
            return FailureType.JS_REQUIRED
        elif any(kw in error_str for kw in ["empty", "no content", "blank page"]):
            return FailureType.EMPTY_CONTENT
        elif any(kw in error_str for kw in ["selector", "element not found", "xpath", "css"]):
            return FailureType.SELECTOR_FAILURE
        elif any(kw in error_str for kw in ["network", "connection", "dns", "unreachable"]):
            return FailureType.NETWORK_ERROR
        elif any(kw in error_str for kw in ["pdf", "document"]):
            return FailureType.PDF_FAILURE
        elif any(kw in error_str for kw in ["auth", "unauthorized", "401", "login"]):
            return FailureType.AUTH_REQUIRED
        elif any(kw in error_str for kw in ["captcha", "challenge"]):
            return FailureType.CAPTCHA
        elif any(kw in error_str for kw in ["api changed", "schema", "unexpected format"]):
            return FailureType.API_CHANGED
        elif any(kw in error_str for kw in ["schema", "validation", "pydantic"]):
            return FailureType.SCHEMA_CHANGED
        else:
            return FailureType.UNKNOWN

    async def _get_known_solutions(
        self,
        source_name: str,
        failure_type: FailureType,
        url: str,
    ) -> List[Dict[str, Any]]:
        """Get known solutions from database"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM recovery_solutions
                WHERE source_name = ? AND failure_type = ?
                AND (url_pattern IS NULL OR url LIKE ?)
                ORDER BY success_count DESC, failure_count ASC
                """,
                (source_name, failure_type.value, f"%{url}%"),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _apply_solution(
        self,
        solution: Dict[str, Any],
        attempt_func,
        *args,
        **kwargs,
    ) -> Any:
        """Apply a stored solution"""
        method_name = solution["solution_method"]
        details = json.loads(solution["solution_details"]) if solution["solution_details"] else {}

        if method_name == "retry_with_backoff":
            return await self._retry_with_backoff(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "rotate_user_agent":
            return await self._rotate_user_agent(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "use_proxy":
            return await self._use_proxy(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "simplify_request":
            return await self._simplify_request(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "switch_to_playwright":
            return await self._switch_to_playwright(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "try_alternative_url":
            return await self._try_alternative_url(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "use_cached_version":
            return await self._use_cached_version(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "search_api_fallback":
            return await self._search_api_fallback(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)
        elif method_name == "delay_and_retry":
            return await self._delay_and_retry(method_name, "", FailureType.UNKNOWN, "", attempt_func, *args, **details, **kwargs)

        return None

    async def _store_solution(
        self,
        source_name: str,
        failure_type: FailureType,
        url: str,
        method_name: str,
        details: Dict[str, Any],
        success: bool,
    ):
        """Store or update solution in database"""
        async with aiosqlite.connect(self.db_path) as db:
            url_pattern = self._extract_url_pattern(url)

            if success:
                await db.execute(
                    """
                    INSERT INTO recovery_solutions
                    (source_name, failure_type, url_pattern, solution_method, solution_details, success_count, last_used_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(source_name, failure_type, url_pattern, solution_method) DO UPDATE SET
                        success_count = success_count + 1,
                        last_used_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (source_name, failure_type.value, url_pattern, method_name, json.dumps(details), datetime.utcnow(), datetime.utcnow()),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO recovery_solutions
                    (source_name, failure_type, url_pattern, solution_method, solution_details, failure_count)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(source_name, failure_type, url_pattern, solution_method) DO UPDATE SET
                        failure_count = failure_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (source_name, failure_type.value, url_pattern, method_name, json.dumps(details)),
                )
            await db.commit()

    async def _record_solution_result(self, solution_id: int, success: bool):
        """Record result of applying a known solution"""
        async with aiosqlite.connect(self.db_path) as db:
            if success:
                await db.execute(
                    "UPDATE recovery_solutions SET success_count = success_count + 1, last_used_at = ? WHERE id = ?",
                    (datetime.utcnow(), solution_id),
                )
            else:
                await db.execute(
                    "UPDATE recovery_solutions SET failure_count = failure_count + 1 WHERE id = ?",
                    (solution_id,),
                )
            await db.commit()

    def _extract_url_pattern(self, url: str) -> str:
        """Extract pattern from URL for matching"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # Keep domain and path structure, replace dynamic parts
        path_parts = parsed.path.split('/')
        pattern_parts = []
        for part in path_parts:
            if part.isdigit() or len(part) > 30:
                pattern_parts.append('*')
            else:
                pattern_parts.append(part)
        return f"{parsed.netloc}/{'/'.join(pattern_parts)}"

    # Recovery method implementations
    async def _retry_with_backoff(self, method_name, source_name, failure_type, url, attempt_func, *args, base_delay=2, max_delay=60, **kwargs):
        for attempt in range(3):
            delay = min(base_delay * (2 ** attempt), max_delay)
            await asyncio.sleep(delay)
            try:
                return await attempt_func(*args, **kwargs)
            except Exception as e:
                logger.warning("retry_failed", attempt=attempt+1, error=str(e))
        return None

    async def _rotate_user_agent(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        ]
        # This would require modifying the HTTP client - placeholder
        return await attempt_func(*args, **kwargs)

    async def _use_proxy(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        # Placeholder for proxy rotation
        return await attempt_func(*args, **kwargs)

    async def _simplify_request(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        # Reduce request complexity - fewer headers, no cookies, etc.
        return await attempt_func(*args, **kwargs)

    async def _switch_to_playwright(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        # Would launch Playwright browser - placeholder
        logger.info("would_switch_to_playwright", url=url)
        return await attempt_func(*args, **kwargs)

    async def _try_alternative_url(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        # Try mobile version, amp, textise dot iitty, etc.
        alternatives = [
            url.replace("www.", "m."),
            f"https://r.jina.ai/http://{url}",
            f"https://r.jina.ai/http://cc.bingj.com/cache.aspx?d=503-123&u={url}",
        ]
        for alt_url in alternatives:
            try:
                return await attempt_func(alt_url, *args[1:], **kwargs)
            except Exception:
                continue
        return None

    async def _use_cached_version(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        # Try Wayback Machine or Google Cache
        cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
        try:
            return await attempt_func(cache_url, *args[1:], **kwargs)
        except Exception:
            return None

    async def _search_api_fallback(self, method_name, source_name, failure_type, url, attempt_func, *args, **kwargs):
        # Use search API to find same content elsewhere
        from ddgs import DDGS
        ddgs = DDGS()
        # Extract title/keywords from URL
        # This is a placeholder
        return None

    async def _delay_and_retry(self, method_name, source_name, failure_type, url, attempt_func, *args, delay=300, **kwargs):
        await asyncio.sleep(delay)
        return await attempt_func(*args, **kwargs)


# Global instance
recovery_agent = RecoveryAgent()


async def with_recovery(source_name: str, url: str, attempt_func, *args, **kwargs):
    """Convenience function to run with automatic recovery"""
    return await recovery_agent.classify_and_recover(source_name, url, Exception("initial"), attempt_func, *args, **kwargs)