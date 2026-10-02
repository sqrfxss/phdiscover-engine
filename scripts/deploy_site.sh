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

# Resolve the repo root to a native path. Under git-bash `pwd` prints
# /f/hermes/phdiscover-engine, and git then creates the worktree at
# F:/f/hermes/... — a directory that does not exist as far as the shell is
# concerned, so files copied there never reach the branch and the publish step
# reports "unchanged" forever.
NATIVE_ROOT="$(cygpath -w "$ROOT" 2>/dev/null || echo "$ROOT")"
NATIVE_PARENT="$(dirname "$NATIVE_ROOT")"
WORK="$NATIVE_PARENT/.ghpages-$(date +%s)"

# ── refuse to publish an empty site ────────────────────────────────
SITE_JSON="$WEB/data/ranked_opportunities.json"
if [ ! -f "$SITE_JSON" ]; then
  echo "No $SITE_JSON — run scripts/run_pipeline.sh first." >&2
  exit 1
fi

# Same interpreter choice as run_pipeline.sh: the system python may be missing
# the project's dependencies. Absolute paths, because the counting step below
# changes directory and a relative .venv path would no longer resolve.
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"

# Native separators: the runner may be git-bash, which reports /f/hermes/...,
# and the Python interpreter then cannot open that path. Hence a relative path
# plus os.path.join rather than an absolute /f/... argument.
COUNT=$(cd "$WEB" && "$PY" -c "
import json, os
print(len(json.load(open(os.path.join('data', 'ranked_opportunities.json'), encoding='utf-8'))))
")
if [ "$COUNT" -eq 0 ]; then
  echo "The site has 0 positions; keeping what is live." >&2
  exit 1
fi
echo "==> $COUNT positions to publish"

# ── stage the site at the branch root ──────────────────────────────
# `git worktree add` must be given origin/$BRANCH explicitly. The local branch
# can be stale — it is only updated by a fetch, and a scheduled crawl pushes to
# the branch without touching this checkout — so naming it checks out an older
# tree, the copy lands on files that git then sees as unchanged, and the script
# reports "site unchanged" while the live site stays behind.
git fetch "$REMOTE" "$BRANCH" >/dev/null 2>&1
git worktree add --detach "$WORK" "origin/$BRANCH" >/dev/null

# Copy with native paths on the source side too: the shell reports /f/hermes/...
# and cp must not hand that to a Windows tool.
NATIVE_WEB="$(cygpath -w "$WEB" 2>/dev/null || echo "$WEB")"
for item in "$NATIVE_WEB"/*; do
  cp -R "$item" "$WORK/"
done
[ -f "$ROOT/data/COVERAGE.md" ] && cp "$ROOT/data/COVERAGE.md" "$WORK/COVERAGE.md" || true
[ -f "$ROOT/data/coverage_report.json" ] && cp "$ROOT/data/coverage_report.json" "$WORK/coverage_report.json" || true
rm -rf "$WORK/data/cache"

git -C "$WORK" add -A
if git -C "$WORK" diff --staged --quiet; then
  echo "==> Site unchanged; nothing to publish."
else
  git -C "$WORK" commit -q -m "site: update positions ($(date +%Y-%m-%d))"
  # The scheduled crawl pushes to the same branch, so it may have moved since
  # the fetch above. Rebase rather than fail, or the local publish is lost every
  # time the two overlap.
  if ! git -C "$WORK" push "$REMOTE" "HEAD:$BRANCH" 2>/dev/null; then
    echo "==> remote moved; rebasing"
    git -C "$WORK" fetch "$REMOTE" "$BRANCH"
    git -C "$WORK" rebase "origin/$BRANCH"
    git -C "$WORK" push "$REMOTE" "HEAD:$BRANCH"
  fi
  echo "==> Pushed to $REMOTE/$BRANCH"
fi

git worktree remove "$WORK" --force
echo "==> https://sqrfxss.github.io/phdiscover-engine/"