#!/usr/bin/env bash
# Create the GitHub repository, push, and turn on Pages.
#
# Written to run with nothing prepared: it installs gh if missing, reads the
# token from stdin (never from this file, never from a shell argument that
# would land in history), creates the repo, pushes, and prints the two UI
# settings that cannot be set from a script.
#
# Usage:
#   bash scripts/push_now.sh                 # public
#   bash scripts/push_now.sh private         # private
#   bash scripts/push_now.sh public other-name
set -euo pipefail
cd "$(dirname "$0")/.."

VISIBILITY="${1:-public}"
REPO_NAME="${2:-phdiscover-engine}"
BIN_DIR="$HOME/bin"

# ── 1. git identity and branch ────────────────────────────────────
if [ ! -d .git ]; then
  git init -q
fi
if ! git config user.email >/dev/null 2>&1; then
  git config user.email "saeidsoraghi6@gmail.com"
  git config user.name "Saeid Soraghi"
fi
# The workflow commits, so it needs a branch name to push to.
git branch -M main 2>/dev/null || true

# ── 2. gh ────────────────────────────────────────────────────────
if ! command -v gh >/dev/null 2>&1 && [ ! -x "$BIN_DIR/gh.exe" ]; then
  echo "==> Fetching the GitHub CLI"
  python - <<'PY'
import asyncio, httpx, zipfile, io, os, sys

URL = "https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_windows_amd64.zip"

async def go():
    # trust_env=False: the machine's SOCKS proxy env vars break every request.
    async with httpx.AsyncClient(timeout=180, follow_redirects=True,
                                 trust_env=False) as c:
        r = await c.get(URL)
        if r.status_code != 200:
            sys.exit(f"download failed: HTTP {r.status_code}")
        dest = os.path.expanduser("~/bin")
        os.makedirs(dest, exist_ok=True)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        for n in z.namelist():
            if n.endswith("gh.exe"):
                open(os.path.join(dest, "gh.exe"), "wb").write(z.read(n))
                print(f"    installed to {dest}/gh.exe")

asyncio.run(go())
PY
fi
export PATH="$BIN_DIR:$PATH"
command -v gh >/dev/null || GH="$BIN_DIR/gh.exe"
echo "==> $(gh --version | head -1)"

# ── 3. auth ──────────────────────────────────────────────────────
if gh auth status >/dev/null 2>&1; then
  echo "==> Already authenticated"
else
  echo "==> Paste a GitHub personal access token (scope: repo)."
  echo "    Create one at:  https://github.com/settings/tokens"
  echo "    The token is read from stdin and never stored in this repo."
  echo "    (press Ctrl+C to abort)"
  printf '    token: '
  gh auth login --hostname github.com --git-protocol https --with-token
fi

# ── 4. commit anything outstanding ───────────────────────────────
git add -A
if ! git diff --cached --quiet; then
  git -c core.autocrlf=false commit -q -m "chore: refresh crawl output"
  echo "==> Committed outstanding changes"
fi
echo "==> $(git log --oneline -1)"

# ── 5. create and push ───────────────────────────────────────────
# gh takes two separate boolean flags, not --public=false.
VIS_FLAG="--public"
[ "$VISIBILITY" = "private" ] && VIS_FLAG="--private"

if gh repo view "$REPO_NAME" >/dev/null 2>&1; then
  echo "==> Repository $REPO_NAME already exists; adding it as a remote"
  gh repo set-default "$REPO_NAME" >/dev/null 2>&1 || true
else
  gh repo create "$REPO_NAME" \
    "$VIS_FLAG" \
    --source=. --remote=origin --push --description \
    "PI-first PhD position discovery: paginated crawl, deadline gate, evidence-backed rankings"
  echo "==> Created and pushed $REPO_NAME"
fi

# ── 6. enable Pages (the API can do this, unlike the two settings below) ──
gh api -X POST "repos/$REPO_NAME/pages" \
  -f source[branch]=main -f source[path]=/ 2>/dev/null \
  && echo "==> Pages enabled" || echo "==> Pages must be enabled in the UI (see below)"

cat <<EOF

────────────────────────────────────────────────────────────────
Pushed:  https://github.com/$REPO_NAME

Two settings left, both in the UI because the token cannot change them:

1. Actions -> General -> Workflow permissions
     -> Read and write permissions
   The daily crawl commits web/data/ back to the repo. Without this it can
   only upload artefacts and the site never updates itself.

2. Actions -> Crawl and publish -> Run workflow
     The first run installs Chromium and crawls every board: allow ~20 min.
     Scheduled runs start the next day.

Pages, if step 6 above did not report success:
   Settings -> Pages -> Source: Deploy from a branch -> main / (root)

The site is static HTML reading web/data/, so Pages serves it as-is.
Newsletter delivery needs a repo secret RESEND_API_KEY (resend.com, 3k/mo free).
────────────────────────────────────────────────────────────────
EOF
