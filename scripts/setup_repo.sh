#!/usr/bin/env bash
# One-time setup for the scheduled crawl.
#
# The crawl only publishes data — it needs a git remote, nothing more. This
# creates the repository, pushes, and explains the two settings that must be
# changed in the GitHub UI because they cannot be set from a script with the
# credentials available here.
set -euo pipefail
cd "$(dirname "$0")/.."

REPO_NAME="${1:-phdiscover-engine}"
GH="${GH:-gh}"

command -v git >/dev/null || { echo "git not found" >&2; exit 1; }
if command -v "$GH" >/dev/null; then
  echo "GitHub CLI: $(gh --version | head -1)"
else
  echo "GitHub CLI not installed — the repo will be created in the browser."
fi

if [ -d .git ]; then
  echo "Already a git repository."
else
  git init -q -b main
  echo "Initialised git on main."
fi

git add -A
git status --short | head -20
echo
echo "Files staged: $(git diff --cached --name-only | wc -l)"

if git diff --cached --quiet; then
  echo "Nothing to commit."
else
  git commit -q -m "PhDiscover: crawler, filter, site, scheduled workflow"
  echo "Committed."
fi

cat <<'EOF'

────────────────────────────────────────────────────────────────
Next steps (these need the GitHub UI or a logged-in CLI)

1. Create the repository
     gh repo create phdiscover-engine --public --source=. --push
   or create it at github.com/new and push over HTTPS.

2. Enable write access for the workflow
     Settings -> Actions -> General -> Workflow permissions
     -> Read and write permissions

   The job commits web/data/ back to the repo. Without this it can only
   upload artefacts, and the site never updates itself.

3. Run it once by hand
     Actions -> Crawl and publish -> Run workflow

   The first run installs Chromium and crawls all boards, so allow ~20
   minutes. Scheduled runs start the next day.

4. Turn the site on
     Settings -> Pages -> Source: Deploy from a branch
     -> main / (root)

   The site is static HTML reading web/data/, so Pages serves it as-is.
   No build step, no server.

5. Optional: newsletter delivery
     Add a repository secret RESEND_API_KEY (resend.com, 3000/mo free)
     and run scripts/send_newsletter.py --send.
────────────────────────────────────────────────────────────────
EOF
