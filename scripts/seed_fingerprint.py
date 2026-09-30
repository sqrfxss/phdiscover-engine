"""
PhDiscover Engine - Fingerprint Seeding Script
Extracts research fingerprint from CV
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog

from phdiscover.config import get_settings
from phdiscover.models import ResearchFingerprint

logger = structlog.get_logger(__name__)


async def extract_fingerprint_from_cv(cv_path: Path) -> ResearchFingerprint:
    """Extract research fingerprint from CV file"""
    logger.info("extracting_fingerprint", cv_path=str(cv_path))

    # In production, this would:
    # 1. Parse PDF/DOCX/TXT
    # 2. Use LLM to extract structured information
    # 3. Create embedding

    # For now, return a default fingerprint based on your CV
    return ResearchFingerprint(
        user_id="saeid_soraghi",
        version="1.0",
        core_topics=[
            "biomechanics",
            "motor control",
            "sensorimotor control",
            "motor learning",
            "gait analysis",
            "postural control",
        ],
        adjacent_topics=[
            "wearable sensing",
            "computational neuroscience",
            "multimodal AI",
            "rehabilitation",
            "injury prevention",
            "sports science",
        ],
        methods=[
            "motion capture",
            "force platforms",
            "signal processing",
            "time normalization",
            "feature extraction",
            "statistical analysis",
        ],
        technical_methods=[
            "MATLAB",
            "Python",
            "LSTM",
            "GRNN",
            "MLP",
            "CNN",
            "RNN",
            "KNN",
            "Random Forest",
            "Autoencoders",
            "GANs",
        ],
        experimental_methods=[
            "human movement experiments",
            "biomechanical testing",
            "load carrying studies",
            "asymmetric gait analysis",
        ],
        data_modalities=[
            "3D kinematics",
            "kinetics",
            "EMG",
            "IMU",
            "ground reaction forces",
        ],
        populations=[
            "human adults",
            "healthy individuals",
            "clinical populations",
            "athletes",
        ],
        research_questions=[
            "How does sensory feedback modulate motor adaptation?",
            "Can deep learning improve gait injury prediction?",
            "What are the biomechanical determinants of running injuries?",
            "How does asymmetric loading affect bilateral force distribution?",
        ],
        career_goals=[
            "academic research",
            "postdoc in biomechanics/motor control",
            "tenure-track faculty position",
        ],
        preferences={
            "countries": ["Canada", "Netherlands", "Germany", "Finland", "New Zealand"],
            "funding_required": True,
            "language": "en",
        },
        hard_exclusions=[
            "military research",
            "animal experimentation",
            "pure chemistry",
            "civil engineering",
        ],
        soft_exclusions=[
            "heavy electrical engineering focus",
            "pure theoretical physics",
        ],
    )


async def save_fingerprint(fingerprint: ResearchFingerprint):
    """Save fingerprint to database"""
    settings = get_settings()
    database_url = settings.database_url

    if not database_url:
        logger.error("database_url_not_configured")
        return

    import asyncpg

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
            json.dumps(fingerprint.core_topics),
            json.dumps(fingerprint.adjacent_topics),
            json.dumps(fingerprint.methods),
            json.dumps(fingerprint.technical_methods),
            json.dumps(fingerprint.experimental_methods),
            json.dumps(fingerprint.data_modalities),
            json.dumps(fingerprint.populations),
            json.dumps(fingerprint.research_questions),
            json.dumps(fingerprint.career_goals),
            json.dumps(fingerprint.preferences),
            json.dumps(fingerprint.hard_exclusions),
            json.dumps(fingerprint.soft_exclusions),
        )
        logger.info("fingerprint_saved", user_id=fingerprint.user_id)
    finally:
        await conn.close()


async def main(cv_path: Path, user_id: str = "default"):
    """CLI entry point"""
    if not cv_path.exists():
        logger.warning("cv_not_found_using_default", path=str(cv_path))

    fingerprint = await extract_fingerprint_from_cv(cv_path)
    fingerprint.user_id = user_id

    # Save to JSON for inspection
    output_path = Path("data") / f"fingerprint_{user_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(fingerprint.model_dump_json(indent=2), encoding="utf-8")
    logger.info("fingerprint_saved_to_json", path=str(output_path))

    # Save to database if configured
    settings = get_settings()
    if settings.database_url:
        await save_fingerprint(fingerprint)

    # Print summary
    print(f"\n{'='*50}")
    print(f"RESEARCH FINGERPRINT - {fingerprint.user_id}")
    print(f"{'='*50}")
    print(f"Core Topics: {', '.join(fingerprint.core_topics)}")
    print(f"Adjacent Topics: {', '.join(fingerprint.adjacent_topics)}")
    print(f"Methods: {', '.join(fingerprint.methods)}")
    print(f"Technical Methods: {', '.join(fingerprint.technical_methods)}")
    print(f"Countries: {', '.join(fingerprint.preferences.get('countries', []))}")
    print(f"Funding Required: {fingerprint.preferences.get('funding_required')}")


if __name__ == "__main__":
    import sys
    cv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("cv.pdf")
    user_id = sys.argv[2] if len(sys.argv) > 2 else "default"
    asyncio.run(main(cv_path, user_id))