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

# Prefer the project's own virtualenv. A stale or partial .venv shadows the
# system interpreter, and the run then dies on `ModuleNotFoundError: bs4`
# several steps in — after the probe has already rewritten the health report.
# Fall back to the system python only when .venv is absent or lacks the deps.
pick_python() {
  local candidates=()
  [ -x ".venv/Scripts/python.exe" ] && candidates+=(".venv/Scripts/python.exe")
  [ -x ".venv/bin/python" ] && candidates+=(".venv/bin/python")
  candidates+=("${PYTHON:-}" python python3)

  for c in "${candidates[@]}"; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    if "$c" -c "import bs4, httpx, yaml, lxml, playwright" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  echo "${PYTHON:-python}"
}

PY="$(pick_python)"

# Say so when the chosen interpreter cannot import what the pipeline needs,
# instead of letting it fail three steps in.
if ! "$PY" -c "import bs4, httpx, yaml, lxml" >/dev/null 2>&1; then
  cat >&2 <<EOF
Missing dependencies for: $PY

  $PY -m pip install -r requirements.lock
  $PY -m pip install -e . --no-deps

or activate the project venv:

  source .venv/Scripts/activate    # Windows
  source .venv/bin/activate        # Linux/macOS
EOF
  exit 1
fi
echo "==> interpreter: $PY"

# Warn when the installed versions have drifted from requirements.lock. Not an
# error — an upgraded patch release is usually fine — but editing a range in
# pyproject.toml once made pip drop typing_extensions, and pydantic imports it
# on every use, so the pipeline died at the import instead of at the upgrade.
if [ -f requirements.lock ]; then
  DRIFT=$("$PY" - <<'PYEOF' 2>/dev/null || true
import sys
from importlib.metadata import version, PackageNotFoundError

try:
    with open("requirements.lock", encoding="utf-8") as fh:
        pinned = dict(
            line.strip().split("==", 1)
            for line in fh
            if "==" in line and not line.strip().startswith("#")
        )
except OSError:
    sys.exit(0)

drift = []
for name, want in sorted(pinned.items()):
    try:
        have = version(name)
    except PackageNotFoundError:
        drift.append(f"{name}: missing (want {want})")
    else:
        if have != want:
            drift.append(f"{name}: {have} (lock says {want})")
print("\n".join(drift))
PYEOF
)
  if [ -n "$DRIFT" ]; then
    echo "==> version drift from requirements.lock:"
    echo "$DRIFT" | sed 's/^/    /'
    echo "    refresh with: $PY -m pip install -r requirements.lock"
  fi
fi

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
echo "=== [2/6] Crawling all boards ==="
$PY scripts/crawl_v2.py

# A local run and the GitHub runner see different boards: the runner reaches
# what times out here, and a local run walks more pages on what it can reach.
# Measured on two consecutive runs — 415 postings from GitHub, 368 from Iran,
# with 8 positions (5 of them EURAXESS) found only by the local run. Merging
# keeps both instead of letting whichever ran last overwrite the other.
if [ -f data/crawl_github_run.json ]; then
  echo
  echo "=== [3/6] Merging with the last GitHub-runner crawl ==="
  $PY scripts/merge_crawls.py data/crawl_github_run.json data/crawl_v2.json \
      -o data/crawl_merged.json
  if [ -s data/crawl_merged.json ]; then
    mv data/crawl_merged.json data/crawl_v2.json
    echo "Merged into data/crawl_v2.json"
  else
    echo "Merge produced nothing; keeping this run's crawl." >&2
  fi
fi

echo
echo "=== [4/6] Filtering to doctoral biomechanics positions ==="
$PY scripts/filter_and_merge.py

echo
echo "=== [5/6] Enriching each position detail page ==="
$PY scripts/enrich_positions.py

echo
echo "=== [6/6] Publishing to web/data ==="
$PY scripts/publish_site.py
$PY scripts/coverage_report.py

echo
echo "Done. Start the site with:"
echo "  cd web && python -m http.server 8765"
