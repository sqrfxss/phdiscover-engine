# Makefile for PhDiscover Engine

.PHONY: help install test lint format type-check clean build run-crawl run-bot run-scheduler export seed migrate docker-build docker-up docker-down docker-logs

# Default target
help:
	@echo "PhDiscover Engine - Available commands:"
	@echo ""
	@echo "Development:"
	@echo "  install       - Install package in development mode"
	@echo "  test          - Run all tests with coverage"
	@echo "  test-unit     - Run unit tests only"
	@echo "  test-integration - Run integration tests only"
	@echo "  test-golden   - Run golden dataset regression tests"
	@echo "  lint          - Run Ruff linter"
	@echo "  format        - Format code with Ruff"
	@echo "  type-check    - Run MyPy type checker"
	@echo "  clean         - Clean build artifacts"
	@echo ""
	@echo "Running:"
	@echo "  run-crawl     - Run all crawlers once"
	@echo "  run-bot       - Start Telegram bot (polling)"
	@echo "  run-scheduler - Start scheduler"
	@echo "  run-pipeline  - Run full pipeline once"
	@echo ""
	@echo "Data:"
	@echo "  export        - Export positions to Excel/JSON/CSV"
	@echo "  seed          - Seed research fingerprint from CV"
	@echo "  migrate       - Run database migrations"
	@echo "  gen-content   - Generate SEO/social content"
	@echo ""
	@echo "Docker:"
	@echo "  docker-build  - Build Docker image"
	@echo "  docker-up     - Start all services with docker-compose"
	@echo "  docker-down   - Stop all services"
	@echo "  docker-logs   - View docker-compose logs"
	@echo "  docker-shell  - Open shell in app container"
	@echo ""

# Development
install:
	pip install -e .[dev]

test:
	pytest tests/ -v --tb=short --cov=src/phdiscover --cov-report=term-missing --cov-report=html

test-unit:
	pytest tests/unit -v --tb=short

test-integration:
	pytest tests/integration -v --tb=short

test-golden:
	pytest tests/golden -v --tb=short

lint:
	ruff check src/ tests/ scripts/

format:
	ruff format src/ tests/ scripts/
	ruff check --fix src/ tests/ scripts/

type-check:
	mypy src/

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Running
run-crawl:
	python -m phdiscover.crawlers.run_all

run-bot:
	python -m phdiscover.bot.run

run-scheduler:
	python -m phdiscover.scheduler.run

run-pipeline:
	python -c "from phdiscover.pipeline import run_pipeline; from phdiscover.models import ResearchFingerprint; import asyncio; fp = ResearchFingerprint(user_id='test'); asyncio.run(run_pipeline(fp))"

# Data
export:
	python -m phdiscover.export.cli --format excel --output exports/phdiscover_export

export-json:
	python -m phdiscover.export.cli --format json --output exports/phdiscover_export

export-csv:
	python -m phdiscover.export.cli --format csv --output exports/phdiscover_export

seed:
	python scripts/seed_fingerprint.py cv.pdf saeid_soraghi

migrate:
	python scripts/migrate_db.py

migrate-drop:
	python scripts/migrate_db.py --drop

gen-content:
	python -m phdiscover.content.generator --days 7 --output content

test-parsers:
	python scripts/test_parsers.py

# Docker
docker-build:
	docker build -f docker/Dockerfile -t phdiscover-engine:latest .

docker-up:
	docker-compose -f docker/docker-compose.yml up -d --build

docker-down:
	docker-compose -f docker/docker-compose.yml down

docker-logs:
	docker-compose -f docker/docker-compose.yml logs -f

docker-shell:
	docker-compose -f docker/docker-compose.yml exec app bash

docker-ps:
	docker-compose -f docker/docker-compose.yml ps

# Database (requires running docker-up first)
db-shell:
	docker-compose -f docker/docker-compose.yml exec postgres psql -U postgres -d phdiscover

redis-cli:
	docker-compose -f docker/docker-compose.yml exec redis redis-cli

# GitHub Actions simulation
act-crawl:
	act -j crawl --secret-file .secrets

act-ci:
	act -j lint-and-test --secret-file .secrets

# Pre-commit
pre-commit-install:
	pre-commit install

pre-commit-run:
	pre-commit run --all-files

# Development helpers
dev-setup: install pre-commit-install
	@echo "Development environment ready!"

check-all: lint type-check test
	@echo "All checks passed!"

# Release
version-patch:
	bump2version patch

version-minor:
	bump2version minor

version-major:
	bump2version major