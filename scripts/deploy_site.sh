#!/usr/bin/env bash
# Publish web/ to the gh-pages branch that GitHub Pages actually serves.
#
# The repository root is not the site — index.html lives in web/ — so Pages is
# pointed at gh-pages, whose root holds the deployed copy. This mirrors the
# publish step in crawl.yml exactly, so a local crawl and a scheduled one end
# up serving the same bytes.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
WEB="$ROOT/web"
BRANCH="${BRANCH:-gh-pages}"
REMOTE="${REMOTE:-origin}"
WORK="$(dirname "$ROOT")/.ghpages-$(date +%s)"

# ── refuse to publish an empty site ────────────────────────────────
SITE_JSON="$WEB/data/ranked_opportunities.json"
if [ ! -f "$SITE_JSON" ]; then
  echo "No $SITE_JSON — run scripts/run_pipeline.sh first." >&2
  exit 1
fi
COUNT=$(python -c "import json,sys; print(len(json.load(open(sys.argv[1], encoding='utf-8'))))" "$SITE_JSON")
if [ "$COUNT" -eq 0 ]; then
  echo "The site has 0 positions; keeping what is live." >&2
  exit 1
fi
echo "==> $COUNT positions to publish"

# ── stage the site at the branch root ──────────────────────────────
rm -rf "$WORK"
mkdir -p "$WORK"
for item in "$WEB"/*; do
  cp -R "$item" "$WORK/"
done
# Reports live under data/, which git ignores; copy them in by hand.
[ -f "$ROOT/data/COVERAGE.md" ] && cp "$ROOT/data/COVERAGE.md" "$WORK/COVERAGE.md" || true
[ -f "$ROOT/data/coverage_report.json" ] && cp "$ROOT/data/coverage_report.json" "$WORK/coverage_report.json" || true
# Never ship the crawl cache.
rm -rf "$WORK/data/cache"

# ── commit on the branch, leaving the working tree untouched ───────
git worktree add -B "$BRANCH" "$WORK" 2>/dev/null || git worktree add "$WORK" "$BRANCH"

git -C "$WORK" add -A
if git -C "$WORK" diff --staged --quiet; then
  echo "==> Site unchanged; nothing to publish."
else
  git -C "$WORK" commit -q -m "site: update positions ($(date +%Y-%m-%d))"
  git -C "$WORK" push "$REMOTE" "$BRANCH"
  echo "==> Pushed to $REMOTE/$BRANCH"
fi

git worktree remove "$WORK" --force
echo "==> https://sqrfxss.github.io/phdiscover-engine/"