# PhDiscover Engine — Autonomous PhD Position Discovery System

## Project Overview
Build a **PI-First PhD Position Discovery Engine** that:
1. Maps research landscape → finds relevant PIs → scores research fit → discovers positions → verifies everything with evidence
2. Runs autonomously from Iran (no paid APIs, free-tier infrastructure)
3. Delivers via Telegram Bot + Excel/JSON export + SEO pages
4. Self-recovers from parser failures, rate limits, site changes

## Architecture (6 Independent Agents)
```
1. Research-Mapping Agent    → University/Dept/Lab mapping by keywords
2. PI Discovery Agent        → Find PIs with fallback chain (Univ → Lab → Dept → ORCID → Scholar)
3. Research-Fit Agent        → Score PIs against user CV (threshold ≥70)
4. Opportunity Search Agent  → Scoped queries only within verified PI domains
5. Verification Agent        → Position + PI + Email (Source Hierarchy: Official=1.0 → Aggregator=0.3)
6. Ranking Agent             → Final Score + Contact Priority (HIGH/MEDIUM/LOW)
```

## Tech Stack (Zero-Cost)
- **Language**: Python 3.11+
- **Crawling**: `crawlee` (HTTP) + `playwright` (JS fallback) + `requests` + `BeautifulSoup`
- **Search**: `ddgs` (DuckDuckGo) + `exa` (free tier) via `web_search` tool
- **Semantic**: `sentence-transformers` (local CPU) + `pgvector` (Supabase/Neon free tier)
- **Research Graph**: `PyAlex` (OpenAlex) + `Semantic Scholar` API (free)
- **Queue/State**: `Redis` (Upstash free) + `LangGraph` for orchestration
- **DB**: `PostgreSQL + pgvector` (Supabase 500MB / Neon 512MB free)
- **Interface**: `python-telegram-bot` (Telegram Bot API) + `Streamlit` (Cloud free)
- **Scheduler**: GitHub Actions (2000 min/mo free) + `apscheduler`
- **Export**: `openpyxl` (Excel) + `json`/`parquet`
- **Deploy**: Docker Compose → Railway/Render/Oracle Cloud free tiers

## Core Data Models (Pydantic)
```python
# Research Fingerprint (from user CV)
class ResearchFingerprint:
    core_topics: List[str]           # biomechanics, motor control, sensorimotor, motor learning
    adjacent_topics: List[str]       # wearable sensing, computational neuroscience, multimodal AI
    methods: List[str]               # motion capture, force platforms, signal processing
    technical_methods: List[str]     # MATLAB, Python, LSTM, GRNN, MLP, CNN
    data_modalities: List[str]       # 3D kinematics, kinetics, EMG, IMU
    populations: List[str]           # human adults, clinical populations
    research_questions: List[str]
    career_goals: List[str]
    preferences: Dict                # countries, language, funding_required
    hard_exclusions: List[str]
    soft_exclusions: List[str]

# PI Profile
class PIProfile:
    id: str
    name: str
    university: str
    department: str
    lab_name: str
    profile_url: str
    lab_url: str
    email: str
    email_source: str                # official_univ | lab_page | dept_page | orcid | scholar
    email_confidence: float          # 0.0-1.0
    research_keywords: List[str]
    recent_papers: List[Paper]
    active_grants: List[Grant]
    phd_students_count: int
    hiring_signals: List[str]
    research_fit_score: float        # 0-100
    verification_level: str          # VERIFIED | PROBABLE | UNVERIFIED

# Position
class Position:
    id: str
    title: str
    opportunity_type: str            # FUNDED_PHD | PHD_PROJECT | DOCTORAL_RESEARCHER | PHD_SCHOLARSHIP | PHD_CALL
    university: str
    department: str
    country: str
    pi_id: str
    pi_name: str
    pi_email: str
    application_deadline: Optional[date]
    funding: str
    url: str
    source_url: str
    email_verified: bool
    verification_source: str
    email_confidence: float
    verification_level: str          # STRONG | MEDIUM | NONE
    research_domain: str
    research_fit_score: float
    final_rank_score: float
    contact_priority: str            # HIGH | MEDIUM | LOW
    discovered_at: datetime
    evidence: List[Evidence]

# Evidence (every claim must have evidence)
class Evidence:
    claim: str
    value: Any
    source_url: str
    source_type: str                 # official_university | official_doctoral_school | official_pi | government | euraxess | findaphd | academicpositions | aggregator | search_snippet
    source_priority: int             # 1-10
    retrieved_at: datetime
    evidence_text: str
    confidence: float
    verification_state: str          # unverified | confirmed | contradicted | uncertain
```

## Source Hierarchy (Email/Position Verification)
| Priority | Source | Weight |
|----------|--------|--------|
| 1 | Official university faculty page | 1.00 |
| 2 | Official doctoral school | 0.98 |
| 3 | Official PI/Lab page | 0.95 |
| 4 | University directory | 0.95 |
| 5 | ORCID | 0.80 |
| 6 | Google Scholar | 0.75 |
| 7 | EURAXESS | 0.75 |
| 8 | AcademicPositions | 0.65 |
| 9 | FindAPhD | 0.60 |
| 10 | Generic aggregator | 0.30 |

**Rule**: Aggregator-only sources NEVER pass email verification.

## Target Countries (Phase 1)
1. **Canada** (NSERC, Vanier, university portals)
2. **Netherlands** (NWO, university portals, VSNU)
3. **Germany** (DAAD, DFG, TV-L E13, Hochschulrektorenkonferenz)
4. **Finland** (Research.fi, university portals)
5. **New Zealand** (Universities NZ, Marsden Fund)

## Hard Rejection Rules (Auto-filter)
- REJECT: Chemistry, Civil/Mech/Electrical Engineering, Pure Math, Astrophysics, Plant Biology
- REJECT: opportunity_type not in [FUNDED_PHD, PHD_PROJECT, DOCTORAL_RESEARCHER, PHD_SCHOLARSHIP, PHD_CALL]
- REJECT: Research Assistant ≠ PhD Position
- REJECT: Deadline expired before 2026
- REJECT: No university-domain email source
- REJECT: Aggregator-only without university verification

## Ranking Formula (Default Weights)
```
FinalScore =
  0.25 × ResearchFit
+ 0.15 × MethodFit
+ 0.12 × SupervisorFit
+ 0.10 × LabFit
+ 0.08 × Eligibility
+ 0.08 × Funding
+ 0.07 × CareerAlignment
+ 0.05 × Freshness
+ 0.05 × OpportunityStrength
+ 0.05 × InstitutionalFit
```

## Parser Versioning (Mandatory)
Every crawler/parser MUST have:
```python
class BaseParser:
    version: str = "v1"
    source_name: str
    source_priority: float
    rate_limit: int = 30  # per minute
    
    async def discover(self, query: DiscoveryQuery) -> List[RawPosition]: ...
    async def extract(self, url: str) -> ExtractedPosition: ...
```

Parsers: `euraxess:v1`, `findaphd:v1`, `academicpositions:v1`, `openalex:v1`, `university_portal:v1`

## Golden Dataset (Regression Testing)
```
tests/golden/
  euraxess/
  findaphd/
  academicpositions/
  university/
  pdf/
  javascript/
  duplicates/
  stale/
  contradictions/
```
Metrics: Precision, Recall, F1, Duplicate accuracy, Deadline accuracy, Funding accuracy, Supervisor accuracy

## Recovery Agent (Failure Taxonomy)
```
NETWORK_ERROR → retry with backoff + proxy rotation
RATE_LIMIT → exponential backoff + respect Retry-After
ROBOTS_BLOCK → try alternative path / search API
TIMEOUT → increase timeout / simplify request
JS_REQUIRED → escalate to Playwright
EMPTY_CONTENT → check selectors / try alternative URL
SELECTOR_FAILURE → inspect DOM → patch parser → replay → regression test
API_CHANGED → detect schema change → update parser version
SCHEMA_CHANGED → same as above
PARSER_FAILURE → log full HTML → classify → apply recovery method
PDF_FAILURE → try docling → unstructured → manual
AUTH_REQUIRED → try public view / search snippet
CAPTCHA → rotate IP / use search API fallback
DUPLICATE → deduplicate by canonical_id
UNKNOWN → log full context → escalate after 3 attempts
```

## Telegram Bot Commands
```
/start          → Research Fingerprint wizard (Persian/English)
/search         → Ranked opportunities (filters: country, field, funding)
/alert          → Daily/Weekly notification for new HIGH priority
/export         → Excel/JSON download
/profile        → View/update fingerprint
/help           → Persian guide
/stats          → System stats (positions, PIs, crawl health)
```

## Content Engine (Auto-Generated)
- Daily position alerts → Telegram Channel + LinkedIn + Twitter
- PI Spotlight posts → LinkedIn
- Funding analysis threads → Twitter/X
- SEO pages: /position/{slug}, /pi/{slug}, /university/{slug}, /country/{slug}

## Development Rules for Claude Code
1. **Type hints on ALL public functions** (mypy strict)
2. **Pydantic v2 models** for all data contracts
3. **Async/await** throughout (httpx, asyncpg, aioredis)
4. **Structured logging** (structlog) with correlation IDs
5. **Error handling**: Custom exceptions, retry decorators, circuit breakers
6. **Tests**: pytest + pytest-asyncio, golden dataset fixtures, >80% coverage
7. **Config**: pydantic-settings with .env support (no hardcoded secrets)
8. **Documentation**: Docstrings (Google style), README, ARCHITECTURE.md, DEPLOY.md
9. **CI/CD**: GitHub Actions (lint → test → build → deploy)
10. **No mock data in production code** — only in tests/fixtures

## Project Structure
```
phdiscover-engine/
├── src/
│   ├── phdiscover/
│   │   ├── __init__.py
│   │   ├── config.py                 # Settings, constants
│   │   ├── models/                   # Pydantic models
│   │   │   ├── __init__.py
│   │   │   ├── fingerprint.py
│   │   │   ├── pi.py
│   │   │   ├── position.py
│   │   │   ├── evidence.py
│   │   │   └── enums.py
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── research_mapping.py
│   │   │   ├── pi_discovery.py
│   │   │   ├── research_fit.py
│   │   │   ├── opportunity_search.py
│   │   │   ├── verification.py
│   │   │   └── ranking.py
│   │   ├── crawlers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── euraxess.py
│   │   │   ├── findaphd.py
│   │   │   ├── academicpositions.py
│   │   │   ├── openalex.py
│   │   │   └── university_portal.py
│   │   ├── intelligence/
│   │   │   ├── __init__.py
│   │   │   ├── ontology.py
│   │   │   ├── embeddings.py
│   │   │   ├── matching.py
│   │   │   └── ranking.py
│   │   ├── verification/
│   │   │   ├── __init__.py
│   │   │   ├── evidence.py
│   │   │   ├── position.py
│   │   │   ├── funding.py
│   │   │   ├── eligibility.py
│   │   │   └── supervisor.py
│   │   ├── bot/
│   │   │   ├── __init__.py
│   │   │   ├── handlers.py
│   │   │   ├── keyboards.py
│   │   │   └── formatters.py
│   │   ├── scheduler/
│   │   │   ├── __init__.py
│   │   │   ├── crawl_scheduler.py
│   │   │   └── alert_scheduler.py
│   │   ├── export/
│   │   │   ├── __init__.py
│   │   │   ├── excel.py
│   │   │   └── json.py
│   │   ├── content/
│   │   │   ├── __init__.py
│   │   │   ├── generator.py
│   │   │   └── seo.py
│   │   └── recovery/
│   │       ├── __init__.py
│   │       ├── classifier.py
│   │       ├── methods.py
│   │       └── store.py
│   └── main.py                       # Entry point
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── golden/
│   └── regression/
├── scripts/
│   ├── deploy.py
│   ├── migrate_db.py
│   └── seed_fingerprint.py
├── config/
│   ├── settings.yaml
│   ├── countries.yaml
│   ├── sources.yaml
│   └── weights.yaml
├── .claude/
│   ├── agents/
│   │   ├── pi_discovery.md
│   │   ├── research_fit.md
│   │   ├── verification.md
│   │   └── parser_maintenance.md
│   ├── rules/
│   │   ├── code_style.md
│   │   ├── testing.md
│   │   └── git_workflow.md
│   └── commands/
│       ├── crawl.md
│       ├── test_parsers.md
│       └── deploy.md
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-compose.prod.yml
├── .github/
│   └── workflows/
│       ├── ci.yml
│       ├── crawl.yml
│       └── deploy.yml
├── CLAUDE.md
├── pyproject.toml
├── README.md
├── ARCHITECTURE.md
├── DEPLOY.md
└── requirements.txt
```

## Environment Variables (.env.template)
```
# Database
DATABASE_URL=postgresql://user:pass@host:5432/db
# Redis
REDIS_URL=redis://user:pass@host:6379/0
# Telegram
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHANNEL_ID=@yourchannel
# Search APIs
EXA_API_KEY=optional
# OpenAlex (no key required but recommended)
OPENALEX_EMAIL=your_email
# Semantic Scholar
SEMANTIC_SCHOLAR_API_KEY=optional
# Deploy
RAILWAY_TOKEN=optional
RENDER_API_KEY=optional
```

## Key Commands
```bash
# Local development
make install          # pip install -e .[dev]
make test             # pytest -v
make lint             # ruff + mypy
make crawl            # python -m phdiscover.crawlers.run_all
make bot              # python -m phdiscover.bot.run
make export           # python -m phdiscover.export.cli

# CI/CD
make docker-build
make docker-push
make deploy-staging
make deploy-prod
```

## Success Criteria (MVP - Week 4-6)
- [ ] 5 countries mapped with 200+ verified PIs
- [ ] 4 parsers operational (EURAXESS, FindAPhD, AcademicPositions, OpenAlex)
- [ ] Daily crawl via GitHub Actions producing 50+ positions/week
- [ ] Telegram Bot responding to /search, /alert, /export
- [ ] Verification pipeline: Position + PI + Email with evidence
- [ ] Ranking producing contact_priority HIGH/MEDIUM/LOW
- [ ] Excel/JSON export working
- [ ] Streamlit demo deployed
- [ ] Golden dataset regression tests passing
- [ ] Parser versioning + recovery methods documented

## Anti-Patterns (DO NOT)
- Fabricate positions or emails
- Mark records verified without evidence
- Treat aggregator as more authoritative than official source
- Equate zero results with zero opportunities
- Use browser automation for every URL (HTTP first)
- Send thousands of candidates to LLM (cheap filter → embedding → rerank → LLM)
- Create duplicate entity records (canonical_id + deduplication)
- Silently discard contradictions (preserve all evidence, flag conflicts)
- Overwrite historical evidence (append-only evidence log)
- Endlessly retry failures (max 3 attempts → MANUAL_REVIEW)
- Modify user constraints without approval
- Claim a parser works without regression tests