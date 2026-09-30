#!/usr/bin/env bash
# PhDiscover — full pipeline: crawl every source, filter, enrich, publish to web/.
#
#   ./scripts/run_pipeline.sh          # crawl + filter + enrich + publish
#   ./scripts/run_pipeline.sh --fast   # skip the browser-heavy sources
#
# Expect roughly 15-20 minutes: the browser stage opens a real Chromium session
# per Cloudflare-walled or client-rendered board.

set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"

# A second concurrent run would have two crawlers writing the same
# data/crawl_v2.json, and the later write would silently discard the earlier
# one's postings. One run at a time.
LOCK="data/.pipeline.lock"
mkdir -p data
if ! command -v flock >/dev/null 2>&1; then
  # No flock on this host (git-bash on Windows). A lock directory is atomic
  # enough: mkdir fails if it already exists.
  if ! mkdir "$LOCK" 2>/dev/null; then
    echo "Another pipeline run holds $LOCK. Wait for it, or remove the dir if it crashed." >&2
    exit 1
  fi
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
fi

echo "=== [1/5] Probing source health ==="
$PY scripts/probe_sources.py || true

echo
echo "=== [2/5] Crawling all boards ==="
$PY scripts/crawl_v2.py

echo
echo "=== [3/5] Filtering to doctoral biomechanics positions ==="
$PY scripts/filter_and_merge.py

echo
echo "=== [4/5] Enriching each position detail page ==="
$PY scripts/enrich_positions.py

echo
echo "=== [5/5] Publishing to web/data ==="
$PY scripts/publish_site.py
$PY scripts/coverage_report.py

echo
echo "Done. Start the site with:"
echo "  cd web && python -m http.server 8765"
