"""
PhDiscover Engine - Scheduler
Runs daily crawls and alert notifications
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from phdiscover.config import get_settings
from phdiscover.models import ResearchFingerprint, Position
from phdiscover.pipeline import run_pipeline
from phdiscover.bot.run import TelegramBot

logger = structlog.get_logger(__name__)


class PhDiscoverScheduler:
    """Scheduler for automated crawling and alerts"""

    def __init__(self):
        self.settings = get_settings()
        self.scheduler = AsyncIOScheduler(timezone=self.settings.app.timezone)
        self.bot: Optional[TelegramBot] = None
        self._running = False

    def set_bot(self, bot: TelegramBot):
        """Set bot instance for sending alerts"""
        self.bot = bot

    def start(self):
        """Start the scheduler"""
        if self._running:
            return

        # Daily crawl at 02:00 UTC
        crawl_trigger = CronTrigger.from_crontab(
            self.settings.scheduler.crawl_schedule,
            timezone=self.settings.app.timezone,
        )
        self.scheduler.add_job(
            self.run_daily_crawl,
            trigger=crawl_trigger,
            id="daily_crawl",
            name="Daily PhD Position Crawl",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # Daily alerts at 09:00 IRST
        alert_trigger = CronTrigger.from_crontab(
            self.settings.scheduler.alert_schedule,
            timezone=self.settings.app.timezone,
        )
        self.scheduler.add_job(
            self.send_daily_alerts,
            trigger=alert_trigger,
            id="daily_alerts",
            name="Daily Alert Notifications",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # Weekly cleanup on Sunday 03:00 UTC
        cleanup_trigger = CronTrigger.from_crontab(
            self.settings.scheduler.cleanup_schedule,
            timezone=self.settings.app.timezone,
        )
        self.scheduler.add_job(
            self.run_cleanup,
            trigger=cleanup_trigger,
            id="weekly_cleanup",
            name="Weekly Database Cleanup",
            max_instances=1,
        )

        self.scheduler.start()
        self._running = True
        logger.info("scheduler_started", jobs=len(self.scheduler.get_jobs()))

    def stop(self):
        """Stop the scheduler"""
        if self._running:
            self.scheduler.shutdown(wait=True)
            self._running = False
            logger.info("scheduler_stopped")

    async def run_daily_crawl(self):
        """Run daily crawl for all active users"""
        logger.info("daily_crawl_started")

        try:
            # In production, iterate over active user fingerprints
            # For now, run with a default fingerprint
            default_fingerprint = ResearchFingerprint(
                user_id="system",
                core_topics=["biomechanics", "motor control", "sensorimotor", "motor learning"],
                adjacent_topics=["wearable sensing", "computational neuroscience", "multimodal AI"],
                methods=["motion capture", "force platforms", "signal processing"],
                technical_methods=["MATLAB", "Python", "LSTM", "GRNN", "MLP", "CNN"],
                data_modalities=["3D kinematics", "kinetics", "EMG", "IMU"],
                populations=["human adults", "clinical populations"],
                preferences={
                    "countries": ["CA", "NL", "DE", "FI", "NZ"],
                    "funding_required": True,
                    "language": "en",
                },
            )

            result = await run_pipeline(default_fingerprint, min_fit_score=70.0)

            positions = result.get("final_output", {}).get("ranked_positions", [])
            logger.info("daily_crawl_complete", positions_found=len(positions))

            # Store new positions for alerts
            await self._store_new_positions(positions)

        except Exception as e:
            logger.exception("daily_crawl_failed", error=str(e))

    async def send_daily_alerts(self):
        """Send daily alerts to subscribed users"""
        logger.info("daily_alerts_started")

        if not self.bot:
            logger.warning("bot_not_set_for_alerts")
            return

        try:
            # Get new high-priority positions from last 24h
            new_positions = await self._get_new_high_priority_positions(hours=24)

            if not new_positions:
                logger.info("no_new_positions_for_alerts")
                return

            # Group by user preferences (simplified)
            # In production, query user subscriptions from database
            await self.bot.send_alert(0, new_positions)  # 0 = broadcast to channel

            logger.info("daily_alerts_sent", positions=len(new_positions))

        except Exception as e:
            logger.exception("daily_alerts_failed", error=str(e))

    async def run_cleanup(self):
        """Weekly cleanup of old data"""
        logger.info("weekly_cleanup_started")

        try:
            # Clean up old crawl logs (>90 days)
            # Mark stale positions (>60 days no update)
            # Remove duplicate positions
            # Vacuum database

            logger.info("weekly_cleanup_complete")
        except Exception as e:
            logger.exception("weekly_cleanup_failed", error=str(e))

    async def _store_new_positions(self, positions: List[Position]):
        """Store new positions for alerting"""
        # In production, save to database with timestamp
        # For now, just log
        logger.debug("storing_positions", count=len(positions))

    async def _get_new_high_priority_positions(self, hours: int = 24) -> List[Position]:
        """Get new high-priority positions from last N hours"""
        # In production, query database
        # For now, return empty list
        return []

    def run_once(self, job_id: str):
        """Run a specific job once (for testing)"""
        job = self.scheduler.get_job(job_id)
        if job:
            job.modify(next_run_time=datetime.now())
        else:
            logger.warning("job_not_found", job_id=job_id)


def main(run_once: bool = False):
    """Main entry point"""
    scheduler = PhDiscoverScheduler()

    if run_once:
        # Run crawl once and exit
        asyncio.run(scheduler.run_daily_crawl())
    else:
        # Start scheduler
        scheduler.start()
        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            scheduler.stop()


if __name__ == "__main__":
    main()