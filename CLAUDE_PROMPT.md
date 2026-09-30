# PhDiscover Engine — Master Prompt for Claude Code

## Project Overview
Build **PhDiscover** — an autonomous PhD/Postdoc position discovery engine with PI-First architecture, evidence-backed verification, and zero-cost operation from Iran. Target: Telegram Bot + Excel/JSON export + SEO pages.

## Core Architecture (6 Independent Agents)

```
┌─────────────────────────────────────────────────────────────────┐
│                    PhDiscover Engine                            │
├─────────────────────────────────────────────────────────────────┤
│  1. Research-Mapping    → University/Dept/Lab → Keywords        │
│  2. PI Discovery        → Find Professors + Fallback Chain      │
│  3. Research-Fit        → Score via User CV (70+ threshold)     │
│  4. Opportunity Search  → Only in Verified PI Domains           │
│  5. Verification        → Position + PI + Email (Official Src)  │
│  6. Ranking             → Final Score + Contact Priority        │
└─────────────────────────────────────────────────────────────────┘
```

## Tech Stack (Zero-Cost, Iran-Operable)
| Layer | Tool | Reason |
|-------|------|--------|
| Crawling | `crawlee` (HTTP) + `playwright` (JS fallback) | 80% pages need no browser |
| Extraction | `crawl4ai` + `BeautifulSoup` | Structured extraction |
| Search | `requests` + DDGS/Exa via web_search | Works from Iran |
| Semantic | `sentence-transformers` + `pgvector` (PostgreSQL) | Local, free, fast |
| Research Graph | `OpenAlex` (PyAlex) + `Semantic Scholar` | Public API, no key needed |
| State/Queue | `Redis` + `LangGraph` | Durable execution |
| DB | `PostgreSQL + pgvector` (Supabase free tier) | Vector + relational |
| Bot | `python-telegram-bot` | Works from Iran natively |
| Deploy | GitHub Actions + Supabase + Docker | Free CI/CD + DB |
| Export | `openpyxl` + JSON | Excel/JSON output |

## Data Models (SQLAlchemy + Pydantic)

```python
# Core entities
class University(Base):
    id, name, country, whed_id, domain, rankings_json

class Department(Base):
    id, university_id, name, url, research_areas_json

class Professor(Base):
    id, department_id, name, email, profile_url, openalex_id,
    research_fingerprint_json, h_index, verified_sources_json

class Position(Base):
    id, professor_id, title, description, funding_type,
    funding_amount, deadline, source_url, verification_level,
    evidence_json, contact_priority, status

class UserProfile(Base):
    id, telegram_id, cv_text, research_fingerprint_json,
    target_countries, target_degrees, preferences_json
```

## Phase 1: Foundation (Week 1)
**Goal**: Research Fingerprint + University Mapping + PI Discovery (5 countries, 200+ PIs)

### Tasks:
1. **Research Fingerprint Extractor**
   - Input: User CV text (Persian/English)
   - Output: Structured fingerprint {topics, methods, populations, keywords_en, keywords_fa}
   - Use: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`

2. **University/Department Mapper**
   - Source: WHED database + OpenAlex institutions
   - Target countries: Germany, Canada, Australia, Netherlands, Sweden (expandable)
   - Output: `config/universities.json` with {id, name, country, domain, dept_urls[]}

3. **PI Discovery Agent**
   - For each department: crawl "Future Students", "PhD Positions", "Research Group" pages
   - Extract: Name, Email, Profile URL, Research Areas, OpenAlex ID
   - Fallback Chain: Univ Directory → Lab Page → ORCID → Google Scholar → Semantic Scholar
   - Verification: Only university-domain emails accepted as STRONG
   - Output: `data/verified_professors.json`

## Phase 2: Discovery (Week 2-3)
**Goal**: Opportunity Search + 10+ Parsers + Cheap Filter → Embedding → Rerank

### Parsers to Implement (Priority Order):
1. **EURAXESS** — European research jobs (structured, reliable)
2. **FindAPhD** — UK/Global PhD listings
3. **AcademicPositions** — EU/Global academic jobs
4. **OpenAlex Works** — Recent papers → infer active grants/projects
5. **University Portal Crawlers** (per country):
   - Germany: `phdgermany.de`, individual uni portals
   - Canada: `mitacs.ca`, university graduate studies pages
   - Australia: `jobs.ac.uk` AU section, uni portals
   - Netherlands: `academictransfer.com`, uni portals
   - Sweden: `jobbsajten.se`, uni portals

### Pipeline:
```
Raw HTML → Parser (site-specific) → Normalized Position → 
Cheap Filter (keyword overlap ≥3) → Embedding (MiniLM) → 
Cross-Encoder Rerank (ms-marco-MiniLM-L6-v2) → Candidate Positions
```

## Phase 3: Intelligence (Week 3-4)
**Goal**: Research Fit Scoring + Verification Pipeline + Final Ranking

### Research Fit Scoring (Weighted Dimensions):
- Research Fit 30% — Topic/Method/Population semantic similarity
- Method Fit 20% — Technique overlap (motion capture, ML, force plates, etc.)
- Topic Fit 20% — Domain alignment (biomechanics, motor control, neuroscience)
- Supervisor Fit 15% — PI publication record, h-index, grant activity
- Funding Fit 15% — Stipend adequacy, guaranteed funding, duration

### Verification Pipeline (Source Hierarchy):
| Level | Source | Weight |
|-------|--------|--------|
| 1.0 | University official page (grad studies, dept, lab) | 1.00 |
| 0.9 | Professor personal page (univ domain) | 0.90 |
| 0.8 | Official job board (EURAXESS, AcademicPositions) | 0.80 |
| 0.6 | Aggregator (FindAPhD, PhDPortal) | 0.60 |
| 0.3 | Third-party/unverified | 0.30 |

**Position Verified** = max(source_weight) ≥ 0.80 AND university-domain email found
**Email Verified** = Found on univ domain page + matches professor name

### Final Ranking:
`Final_Score = Research_Fit * 0.4 + Verification_Score * 0.3 + Funding_Score * 0.2 + Recency * 0.1`

## Phase 4: Product (Week 5-6)
**Goal**: Telegram Bot + Streamlit Cloud Demo + SEO Pages + Export

### Telegram Bot Commands:
- `/start` — Onboarding, CV upload, preferences
- `/search` — Run discovery for user profile
- `/positions` — List matched positions (paginated, filterable)
- `/professor <id>` — PI profile with contact info
- `/export` — Excel/JSON download
- `/alert` — Set up daily/weekly alerts
- `/settings` — Modify preferences

### Streamlit Cloud Demo:
- Public demo with sample data
- Position search/filter UI
- Professor profiles
- Export button

### SEO Page Generator:
- Dynamic routes: `/position/<id>`, `/professor/<id>`, `/university/<id>`, `/country/<code>`
- Structured data (JobPosting, Person, Organization)
- Auto-generated from verified data

## Phase 5: Launch (Week 7-8)
**Goal**: Content Engine + Landing Page + First 100 Users

### Auto-Content Engine:
- Daily: "New Funded Positions in [Field] — [Date]"
- Weekly: "Top 10 PIs Hiring in [Country] — [Week]"
- Per-position: Thread/Tweet/LinkedIn post generator
- All content: Evidence-backed, no marketing fluff

## Quality Gates (Non-Negotiable)
1. **Golden Dataset**: 50 manually verified positions → CI regression test
2. **Parser Health**: Each parser has success_rate metric, alert if <80%
3. **Verification Audit**: Random sample of 20 positions/week manual check
4. **Rate Limit Handling**: Exponential backoff, IP rotation via free proxies
5. **Error Recovery**: Failed URLs → retry queue → manual review queue

## File Structure
```
phdiscover-engine/
├── src/
│   ├── agents/
│   │   ├── research_mapping.py
│   │   ├── pi_discovery.py
│   │   ├── research_fit.py
│   │   ├── opportunity_search.py
│   │   ├── verification.py
│   │   └── ranking.py
│   ├── crawlers/
│   │   ├── base.py
│   │   ├── euraxess.py
│   │   ├── findaphd.py
│   │   ├── academic_positions.py
│   │   └── univ_portals/
│   ├── models/
│   │   ├── database.py
│   │   ├── schemas.py
│   │   └── fingerprint.py
│   ├── bot/
│   │   ├── handlers.py
│   │   ├── keyboards.py
│   │   └── export.py
│   ├── api/
│   │   ├── openalex.py
│   │   ├── semantic_scholar.py
│   │   └── embeddings.py
│   └── utils/
│       ├── recovery.py
│       ├── rate_limit.py
│       └── export.py
├── tests/
│   ├── golden_dataset.json
│   ├── test_parsers.py
│   └── test_scoring.py
├── config/
│   ├── universities.json
│   ├── countries.yaml
│   └── weights.yaml
├── data/
│   └── verified_professors.json
├── scripts/
│   ├── run_crawl.py
│   ├── run_verification.py
│   └── generate_seo.py
├── .github/workflows/
│   ├── crawl.yml
│   ├── verify.yml
│   └── deploy.yml
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml
├── README.md
└── CLAUDE_PROMPT.md (this file)
```

## Execution Rules for Claude Code
1. **Work in small, verifiable increments** — each agent/file tested before next
2. **Write tests first** — golden dataset drives parser development
3. **Recovery-first** — every crawler has retry/fallback/logic
4. **No hardcoded secrets** — all config via env vars
5. **Iran-operable** — no blocked services, use free tiers only
6. **Document as you go** — each module has docstring + usage example
7. **Commit often** — atomic commits with clear messages

## Start Command for Claude Code
```bash
cd F:/hermes/phdiscover-engine
claude -p "$(cat CLAUDE_PROMPT.md)" --allowedTools "Read,Write,Edit,Bash,Glob,Grep,LS,TodoWrite,Task"
```

## Success Criteria (MVP)
- [ ] 5 countries mapped, 200+ verified PIs in database
- [ ] 4 parsers working (EURAXESS, FindAPhD, AcademicPositions, OpenAlex)
- [ ] Research Fit scoring produces ranked results for test CV
- [ ] Verification pipeline marks ≥80% positions with confidence ≥0.8
- [ ] Telegram bot responds to `/search` with formatted results
- [ ] Excel export works with all position fields
- [ ] GitHub Actions crawl runs daily without manual intervention
- [ ] Zero monetary cost (all free tiers)

---

**Note to Claude Code**: You are the autonomous builder. I (Manager Agent) will monitor via GitHub Actions logs and kanban. If you hit blockers, use recovery patterns. If you need human credentials (Supabase, BotFather), I will provide. Otherwise, decide and execute.