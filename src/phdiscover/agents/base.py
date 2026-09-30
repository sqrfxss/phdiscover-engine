"""
PhDiscover Engine - Base Agent Class
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, TypeVar

import structlog
from pydantic import BaseModel, ConfigDict, Field

from phdiscover.config import get_settings
from phdiscover.models import CrawlLog, FailureType, RecoverySolution

logger = structlog.get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")


class AgentResult(BaseModel):
    """Standard agent result wrapper"""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    error_type: Optional[FailureType] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    execution_time_ms: int = 0
    agent_name: str = ""
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])


class BaseAgent(ABC):
    """Base class for all PhDiscover agents"""

    def __init__(self, name: str):
        self.name = name
        self.settings = get_settings()
        self.logger = logger.bind(agent=name)
        self._correlation_id = str(uuid.uuid4())[:8]

    @property
    def correlation_id(self) -> str:
        return self._correlation_id

    def new_correlation_id(self) -> str:
        self._correlation_id = str(uuid.uuid4())[:8]
        return self._correlation_id

    @asynccontextmanager
    async def track_execution(self, operation: str):
        """Context manager to track execution time and log"""
        start = datetime.utcnow()
        cid = self.new_correlation_id()
        self.logger.info("operation_start", operation=operation, correlation_id=cid)
        try:
            yield cid
            duration = int((datetime.utcnow() - start).total_seconds() * 1000)
            self.logger.info("operation_complete", operation=operation, duration_ms=duration, correlation_id=cid)
        except Exception as e:
            duration = int((datetime.utcnow() - start).total_seconds() * 1000)
            self.logger.exception("operation_failed", operation=operation, duration_ms=duration, error=str(e), correlation_id=cid)
            raise

    async def execute_with_retry(
        self,
        operation: str,
        func,
        *args,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        **kwargs,
    ) -> AgentResult:
        """Execute function with exponential backoff retry"""
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                async with self.track_execution(f"{operation}_attempt_{attempt}") as cid:
                    result = await func(*args, **kwargs)
                    return AgentResult(
                        success=True,
                        data=result,
                        agent_name=self.name,
                        correlation_id=cid,
                    )
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    self.logger.warning(
                        "retry_attempt",
                        operation=operation,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(
                        "max_retries_exceeded",
                        operation=operation,
                        max_retries=max_retries,
                        error=str(e),
                    )

        return AgentResult(
            success=False,
            error=str(last_error),
            error_type=FailureType.UNKNOWN,
            agent_name=self.name,
            correlation_id=self.correlation_id,
        )

    @abstractmethod
    async def run(self, *args, **kwargs) -> AgentResult:
        """Main agent execution method"""
        pass

    def create_crawl_log(
        self,
        source_name: str,
        url: str,
        success: bool = False,
        status_code: Optional[int] = None,
        error_type: Optional[FailureType] = None,
        error_message: Optional[str] = None,
        items_extracted: int = 0,
        parser_version: Optional[str] = None,
    ) -> CrawlLog:
        """Create a standardized crawl log entry"""
        return CrawlLog(
            source_name=source_name,
            url=url,
            success=success,
            status_code=status_code,
            error_type=error_type,
            error_message=error_message,
            items_extracted=items_extracted,
            parser_version=parser_version,
        )

    def classify_failure(self, error: Exception, url: str = "") -> FailureType:
        """Classify failure type for recovery"""
        error_str = str(error).lower()

        if any(kw in error_str for kw in ["timeout", "timed out", "read timeout"]):
            return FailureType.TIMEOUT
        elif any(kw in error_str for kw in ["rate limit", "429", "too many requests"]):
            return FailureType.RATE_LIMIT
        elif any(kw in error_str for kw in ["robots.txt", "disallowed", "403 forbidden"]):
            return FailureType.ROBOTS_BLOCK
        elif any(kw in error_str for kw in ["javascript", "js required", "dynamic content", "spa"]):
            return FailureType.JS_REQUIRED
        elif any(kw in error_str for kw in ["empty", "no content", "blank page"]):
            return FailureType.EMPTY_CONTENT
        elif any(kw in error_str for kw in ["selector", "xpath", "css", "element not found"]):
            return FailureType.SELECTOR_FAILURE
        elif any(kw in error_str for kw in ["api changed", "schema", "unexpected format"]):
            return FailureType.API_CHANGED
        elif any(kw in error_str for kw in ["network", "connection", "dns", "unreachable"]):
            return FailureType.NETWORK_ERROR
        elif any(kw in error_str for kw in ["pdf", "document"]):
            return FailureType.PDF_FAILURE
        elif any(kw in error_str for kw in ["auth", "unauthorized", "401", "login"]):
            return FailureType.AUTH_REQUIRED
        elif any(kw in error_str for kw in ["captcha", "challenge"]):
            return FailureType.CAPTCHA
        else:
            return FailureType.UNKNOWN