-- Database Initialization Script
-- Run on first container startup

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Create schemas
CREATE SCHEMA IF NOT EXISTS phdiscover;
CREATE SCHEMA IF NOT EXISTS crawl;

-- Set search path
ALTER DATABASE phdiscover SET search_path TO phdiscover, crawl, public;

-- Create enum types
CREATE TYPE phdiscover.opportunity_type AS ENUM (
    'FUNDED_PHD',
    'PHD_PROJECT',
    'DOCTORAL_RESEARCHER',
    'PHD_SCHOLARSHIP',
    'PHD_CALL',
    'RESEARCH_ASSISTANT',
    'POSTDOC',
    'MASTER',
    'INTERNSHIP',
    'UNKNOWN'
);

CREATE TYPE phdiscover.verification_level AS ENUM (
    'STRONG',
    'MEDIUM',
    'NONE'
);

CREATE TYPE phdiscover.contact_priority AS ENUM (
    'HIGH',
    'MEDIUM',
    'LOW'
);

CREATE TYPE phdiscover.source_type AS ENUM (
    'official_university',
    'official_doctoral_school',
    'official_pi',
    'official_project',
    'government',
    'euraxess',
    'findaphd',
    'academicpositions',
    'aggregator',
    'search_snippet',
    'university_directory',
    'orcid',
    'google_scholar'
);

CREATE TYPE phdiscover.position_state AS ENUM (
    'DISCOVERED',
    'FETCH_PENDING',
    'FETCHED',
    'EXTRACTED',
    'NORMALIZED',
    'DEDUPLICATED',
    'SCREENED',
    'SEMANTIC_MATCHED',
    'VERIFICATION_PENDING',
    'POSITION_VERIFIED',
    'SUPERVISOR_VERIFIED',
    'FUNDING_VERIFIED',
    'ELIGIBILITY_VERIFIED',
    'EVIDENCE_COMPLETE',
    'RANKED',
    'OPPORTUNITY_ACTIVE',
    'REJECTED',
    'DUPLICATE',
    'STALE',
    'CLOSED',
    'INSUFFICIENT_EVIDENCE',
    'BLOCKED',
    'RETRY_PENDING',
    'MANUAL_REVIEW',
    'PRE_VACANCY'
);

CREATE TYPE crawl.failure_type AS ENUM (
    'NETWORK_ERROR',
    'RATE_LIMIT',
    'ROBOTS_BLOCK',
    'TIMEOUT',
    'JS_REQUIRED',
    'EMPTY_CONTENT',
    'SELECTOR_FAILURE',
    'API_CHANGED',
    'SCHEMA_CHANGED',
    'PARSER_FAILURE',
    'PDF_FAILURE',
    'AUTH_REQUIRED',
    'CAPTCHA',
    'DUPLICATE',
    'UNKNOWN'
);

-- Core Tables

-- Research Fingerprint (User Profile)
CREATE TABLE phdiscover.research_fingerprint (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) UNIQUE NOT NULL,
    version VARCHAR(50) NOT NULL DEFAULT '1.0',
    core_topics JSONB NOT NULL DEFAULT '[]',
    adjacent_topics JSONB NOT NULL DEFAULT '[]',
    methods JSONB NOT NULL DEFAULT '[]',
    technical_methods JSONB NOT NULL DEFAULT '[]',
    experimental_methods JSONB NOT NULL DEFAULT '[]',
    data_modalities JSONB NOT NULL DEFAULT '[]',
    populations JSONB NOT NULL DEFAULT '[]',
    research_questions JSONB NOT NULL DEFAULT '[]',
    career_goals JSONB NOT NULL DEFAULT '[]',
    preferences JSONB NOT NULL DEFAULT '{}',
    hard_exclusions JSONB NOT NULL DEFAULT '[]',
    soft_exclusions JSONB NOT NULL DEFAULT '[]',
    embedding vector(384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Universities
CREATE TABLE phdiscover.universities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(500) NOT NULL,
    country_code CHAR(2) NOT NULL,
    city VARCHAR(200),
    website VARCHAR(500),
    whed_url VARCHAR(500),
    ranking_data JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, country_code)
);

CREATE INDEX idx_universities_country ON phdiscover.universities(country_code);
CREATE INDEX idx_universities_name_trgm ON phdiscover.universities USING gin(name gin_trgm_ops);

-- Departments
CREATE TABLE phdiscover.departments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    university_id UUID NOT NULL REFERENCES phdiscover.universities(id) ON DELETE CASCADE,
    name VARCHAR(500) NOT NULL,
    website VARCHAR(500),
    research_areas JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_departments_university ON phdiscover.departments(university_id);

-- Research Groups / Labs
CREATE TABLE phdiscover.research_groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    department_id UUID NOT NULL REFERENCES phdiscover.departments(id) ON DELETE CASCADE,
    name VARCHAR(500) NOT NULL,
    pi_id UUID, -- Will reference PI table (circular, handled in app)
    website VARCHAR(500),
    research_keywords JSONB DEFAULT '[]',
    equipment JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_research_groups_department ON phdiscover.research_groups(department_id);

-- PIs (Principal Investigators)
CREATE TABLE phdiscover.pis (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(300) NOT NULL,
    university_id UUID NOT NULL REFERENCES phdiscover.universities(id) ON DELETE CASCADE,
    department_id UUID REFERENCES phdiscover.departments(id) ON DELETE SET NULL,
    research_group_id UUID REFERENCES phdiscover.research_groups(id) ON DELETE SET NULL,
    profile_url VARCHAR(1000),
    lab_url VARCHAR(1000),
    email VARCHAR(300),
    email_source VARCHAR(100),
    email_confidence DECIMAL(3,2) DEFAULT 0.0,
    email_verified BOOLEAN DEFAULT FALSE,
    verification_level phdiscover.verification_level DEFAULT 'NONE',
    orcid_id VARCHAR(50),
    google_scholar_id VARCHAR(100),
    openalex_id VARCHAR(100),
    research_keywords JSONB DEFAULT '[]',
    recent_papers JSONB DEFAULT '[]',
    active_grants JSONB DEFAULT '[]',
    phd_students_count INTEGER DEFAULT 0,
    hiring_signals JSONB DEFAULT '[]',
    research_fit_score DECIMAL(5,2) DEFAULT 0.0,
    verification_status phdiscover.verification_level DEFAULT 'NONE',
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pis_university ON phdiscover.pis(university_id);
CREATE INDEX idx_pis_email ON phdiscover.pis(email);
CREATE INDEX idx_pis_research_fit ON phdiscover.pis(research_fit_score DESC);
CREATE INDEX idx_pis_verification ON phdiscover.pis(verification_status);

-- Positions
CREATE TABLE phdiscover.positions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    canonical_id VARCHAR(100) UNIQUE NOT NULL, -- For deduplication across sources
    title VARCHAR(1000) NOT NULL,
    opportunity_type phdiscover.opportunity_type NOT NULL DEFAULT 'UNKNOWN',
    university_id UUID NOT NULL REFERENCES phdiscover.universities(id) ON DELETE CASCADE,
    department_id UUID REFERENCES phdiscover.departments(id) ON DELETE SET NULL,
    research_group_id UUID REFERENCES phdiscover.research_groups(id) ON DELETE SET NULL,
    pi_id UUID REFERENCES phdiscover.pis(id) ON DELETE SET NULL,
    country_code CHAR(2) NOT NULL,
    city VARCHAR(200),
    description TEXT,
    requirements JSONB DEFAULT '[]',
    preferred_requirements JSONB DEFAULT '[]',
    methods JSONB DEFAULT '[]',
    topics JSONB DEFAULT '[]',
    application_deadline DATE,
    funding_amount VARCHAR(200),
    funding_currency CHAR(3),
    funding_details TEXT,
    url VARCHAR(1000) NOT NULL,
    source_url VARCHAR(1000),
    source_ids JSONB DEFAULT '[]',
    email_verified BOOLEAN DEFAULT FALSE,
    verification_source VARCHAR(200),
    verification_method VARCHAR(100),
    email_confidence DECIMAL(3,2) DEFAULT 0.0,
    verification_level phdiscover.verification_level DEFAULT 'NONE',
    research_domain VARCHAR(100),
    research_fit_score DECIMAL(5,2) DEFAULT 0.0,
    final_rank_score DECIMAL(5,2) DEFAULT 0.0,
    contact_priority phdiscover.contact_priority DEFAULT 'LOW',
    state phdiscover.position_state DEFAULT 'DISCOVERED',
    freshness_score DECIMAL(4,3) DEFAULT 1.0,
    confidence DECIMAL(3,2) DEFAULT 0.0,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_crawled_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ,
    posting_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_positions_canonical ON phdiscover.positions(canonical_id);
CREATE INDEX idx_positions_university ON phdiscover.positions(university_id);
CREATE INDEX idx_positions_pi ON phdiscover.positions(pi_id);
CREATE INDEX idx_positions_country ON phdiscover.positions(country_code);
CREATE INDEX idx_positions_state ON phdiscover.positions(state);
CREATE INDEX idx_positions_rank ON phdiscover.positions(final_rank_score DESC);
CREATE INDEX idx_positions_deadline ON phdiscover.positions(application_deadline);
CREATE INDEX idx_positions_priority ON phdiscover.positions(contact_priority, final_rank_score DESC);
CREATE INDEX idx_positions_discovered ON phdiscover.positions(discovered_at DESC);

-- Evidence (Every claim must have evidence)
CREATE TABLE phdiscover.evidence (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    candidate_id UUID NOT NULL, -- References positions.id or pis.id
    candidate_type VARCHAR(50) NOT NULL, -- 'position' or 'pi'
    claim TEXT NOT NULL,
    value JSONB,
    source_url VARCHAR(1000) NOT NULL,
    source_type phdiscover.source_type NOT NULL,
    source_priority INTEGER NOT NULL DEFAULT 5,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evidence_text TEXT NOT NULL,
    confidence DECIMAL(3,2) NOT NULL DEFAULT 0.5,
    verification_state VARCHAR(50) DEFAULT 'unverified', -- unverified, confirmed, contradicted, uncertain
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_evidence_candidate ON phdiscover.evidence(candidate_id, candidate_type);
CREATE INDEX idx_evidence_source ON phdiscover.evidence(source_type, source_priority);
CREATE INDEX idx_evidence_verification ON phdiscover.evidence(verification_state);

-- Crawl Logs
CREATE TABLE crawl.crawl_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_name VARCHAR(100) NOT NULL,
    url VARCHAR(1000) NOT NULL,
    status_code INTEGER,
    content_hash VARCHAR(64),
    parser_version VARCHAR(50),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    success BOOLEAN DEFAULT FALSE,
    error_type crawl.failure_type,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    items_extracted INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_crawl_logs_source ON crawl.crawl_logs(source_name, success);
CREATE INDEX idx_crawl_logs_url ON crawl.crawl_logs(url);
CREATE INDEX idx_crawl_logs_created ON crawl.crawl_logs(created_at DESC);

-- Recovery Solutions
CREATE TABLE crawl.recovery_solutions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_name VARCHAR(100) NOT NULL,
    failure_type crawl.failure_type NOT NULL,
    url_pattern VARCHAR(500),
    solution_method VARCHAR(200) NOT NULL,
    solution_details JSONB,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_name, failure_type, url_pattern, solution_method)
);

CREATE INDEX idx_recovery_solutions_lookup ON crawl.recovery_solutions(source_name, failure_type);

-- User Subscriptions (Telegram)
CREATE TABLE phdiscover.user_subscriptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    telegram_user_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(200),
    first_name VARCHAR(200),
    language_code CHAR(2) DEFAULT 'fa',
    research_fingerprint_id UUID REFERENCES phdiscover.research_fingerprint(id) ON DELETE SET NULL,
    alert_frequency VARCHAR(20) DEFAULT 'daily', -- daily, weekly, instant
    min_rank_score DECIMAL(5,2) DEFAULT 70.0,
    preferred_countries JSONB DEFAULT '[]',
    preferred_fields JSONB DEFAULT '[]',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_alert_at TIMESTAMPTZ
);

CREATE INDEX idx_subscriptions_active ON phdiscover.user_subscriptions(is_active);

-- Alert History
CREATE TABLE phdiscover.alert_history (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    subscription_id UUID NOT NULL REFERENCES phdiscover.user_subscriptions(id) ON DELETE CASCADE,
    position_ids JSONB NOT NULL DEFAULT '[]',
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivery_status VARCHAR(50) DEFAULT 'sent',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_alert_history_subscription ON phdiscover.alert_history(subscription_id);
CREATE INDEX idx_alert_history_sent ON phdiscover.alert_history(sent_at DESC);

-- Source Health Monitoring
CREATE TABLE crawl.source_health (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_name VARCHAR(100) UNIQUE NOT NULL,
    last_successful_crawl TIMESTAMPTZ,
    last_failed_crawl TIMESTAMPTZ,
    consecutive_failures INTEGER DEFAULT 0,
    total_crawls INTEGER DEFAULT 0,
    successful_crawls INTEGER DEFAULT 0,
    avg_duration_ms INTEGER,
    avg_items_per_crawl DECIMAL(10,2),
    current_parser_version VARCHAR(50),
    status VARCHAR(50) DEFAULT 'healthy', -- healthy, degraded, down, maintenance
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION phdiscover.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply updated_at triggers
CREATE TRIGGER update_research_fingerprint_updated_at BEFORE UPDATE ON phdiscover.research_fingerprint FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_universities_updated_at BEFORE UPDATE ON phdiscover.universities FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_departments_updated_at BEFORE UPDATE ON phdiscover.departments FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_research_groups_updated_at BEFORE UPDATE ON phdiscover.research_groups FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_pis_updated_at BEFORE UPDATE ON phdiscover.pis FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_positions_updated_at BEFORE UPDATE ON phdiscover.positions FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_user_subscriptions_updated_at BEFORE UPDATE ON phdiscover.user_subscriptions FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_source_health_updated_at BEFORE UPDATE ON crawl.source_health FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();
CREATE TRIGGER update_recovery_solutions_updated_at BEFORE UPDATE ON crawl.recovery_solutions FOR EACH ROW EXECUTE FUNCTION phdiscover.update_updated_at_column();

-- Vector index for semantic search (created after data exists)
-- CREATE INDEX idx_positions_embedding ON phdiscover.positions USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- CREATE INDEX idx_pis_embedding ON phdiscover.pis USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Grant permissions (adjust for production)
GRANT ALL ON ALL TABLES IN SCHEMA phdiscover TO postgres;
GRANT ALL ON ALL TABLES IN SCHEMA crawl TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA phdiscover TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA crawl TO postgres;