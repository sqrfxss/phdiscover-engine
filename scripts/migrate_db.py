"""
PhDiscover Engine - Database Migration Script
"""

from __future__ import annotations

import asyncio
import asyncpg
import structlog
from pathlib import Path

from phdiscover.config import get_settings

logger = structlog.get_logger(__name__)


async def run_migrations(drop: bool = False):
    """Run database migrations"""
    settings = get_settings()
    database_url = settings.database_url

    if not database_url:
        logger.error("database_url_not_configured")
        return

    logger.info("connecting_to_database", url=database_url.split("@")[-1] if "@" in database_url else database_url)

    conn = await asyncpg.connect(database_url)

    try:
        if drop:
            logger.warning("dropping_all_tables")
            await conn.execute("DROP SCHEMA IF EXISTS phdiscover CASCADE")
            await conn.execute("DROP SCHEMA IF EXISTS crawl CASCADE")

        # Read and execute init script
        init_script = Path(__file__).parent.parent.parent / "docker" / "init-db.sql"
        if init_script.exists():
            sql = init_script.read_text(encoding="utf-8")
            # Split by semicolon and execute each statement
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                if stmt:
                    try:
                        await conn.execute(stmt)
                    except Exception as e:
                        logger.warning("statement_failed", statement=stmt[:100], error=str(e))

        logger.info("migrations_complete")

        # Verify tables
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema IN ('phdiscover', 'crawl')
            ORDER BY table_name
        """)
        logger.info("tables_created", tables=[t["table_name"] for t in tables])

    finally:
        await conn.close()


async def seed_fingerprint(cv_path: Path, user_id: str = "default"):
    """Extract and seed research fingerprint from CV"""
    settings = get_settings()
    database_url = settings.database_url

    if not database_url:
        logger.error("database_url_not_configured")
        return

    logger.info("seeding_fingerprint", cv_path=str(cv_path), user_id=user_id)

    # In production, this would:
    # 1. Parse CV (PDF/DOCX/TXT)
    # 2. Extract research topics, methods, etc. using LLM
    # 3. Create embedding
    # 4. Save to database

    # For now, create default fingerprint for biomechanics researcher
    from phdiscover.models import ResearchFingerprint

    fingerprint = ResearchFingerprint(
        user_id=user_id,
        core_topics=["biomechanics", "motor control", "sensorimotor control", "motor learning"],
        adjacent_topics=["wearable sensing", "computational neuroscience", "multimodal AI", "rehabilitation"],
        methods=["motion capture", "force platforms", "signal processing", "gait analysis"],
        technical_methods=["MATLAB", "Python", "LSTM", "GRNN", "MLP", "CNN", "RNN"],
        experimental_methods=["human movement experiments", "biomechanical testing"],
        data_modalities=["3D kinematics", "kinetics", "EMG", "IMU"],
        populations=["human adults", "clinical populations", "athletes"],
        research_questions=[
            "How does sensory feedback modulate motor adaptation?",
            "Can deep learning improve gait injury prediction?",
            "What are the biomechanical determinants of running injuries?",
        ],
        career_goals=["academic research", "postdoc in biomechanics", "tenure-track faculty"],
        preferences={
            "countries": ["Canada", "Netherlands", "Germany", "Finland", "New Zealand"],
            "funding_required": True,
            "language": "en",
        },
        hard_exclusions=["military research", "animal experimentation"],
        soft_exclusions=["heavy electrical engineering focus"],
    )

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            """
            INSERT INTO phdiscover.research_fingerprint
            (user_id, version, core_topics, adjacent_topics, methods, technical_methods,
             experimental_methods, data_modalities, populations, research_questions,
             career_goals, preferences, hard_exclusions, soft_exclusions)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (user_id) DO UPDATE SET
                core_topics = EXCLUDED.core_topics,
                adjacent_topics = EXCLUDED.adjacent_topics,
                methods = EXCLUDED.methods,
                technical_methods = EXCLUDED.technical_methods,
                experimental_methods = EXCLUDED.experimental_methods,
                data_modalities = EXCLUDED.data_modalities,
                populations = EXCLUDED.populations,
                research_questions = EXCLUDED.research_questions,
                career_goals = EXCLUDED.career_goals,
                preferences = EXCLUDED.preferences,
                hard_exclusions = EXCLUDED.hard_exclusions,
                soft_exclusions = EXCLUDED.soft_exclusions,
                updated_at = NOW()
            """,
            fingerprint.user_id,
            fingerprint.version,
            json_dumps(fingerprint.core_topics),
            json_dumps(fingerprint.adjacent_topics),
            json_dumps(fingerprint.methods),
            json_dumps(fingerprint.technical_methods),
            json_dumps(fingerprint.experimental_methods),
            json_dumps(fingerprint.data_modalities),
            json_dumps(fingerprint.populations),
            json_dumps(fingerprint.research_questions),
            json_dumps(fingerprint.career_goals),
            json_dumps(fingerprint.preferences),
            json_dumps(fingerprint.hard_exclusions),
            json_dumps(fingerprint.soft_exclusions),
        )
        logger.info("fingerprint_seeded", user_id=user_id)
    finally:
        await conn.close()


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


async def main(drop: bool = False):
    await run_migrations(drop=drop)


if __name__ == "__main__":
    asyncio.run(main())