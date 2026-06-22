#!/usr/bin/env bash
# check-upstream-behind.sh
# Fetch upstream and report if origin/main is behind upstream/main.
# Designed for daily cron use — quiet when everything is up-to-date, noisy when behind.
set -euo pipefail

REPO_DIR="${1:-/workspace/hermes-agent-fork}"
cd "$REPO_DIR"

# Ensure upstream remote exists
if ! git remote get-url upstream &>/dev/null; then
  echo "ERROR: no 'upstream' remote configured in $REPO_DIR"
  exit 1
fi

# Fetch upstream refs (quietly)
git fetch upstream --quiet 2>&1 || { echo "ERROR: git fetch upstream failed"; exit 1; }

# Get commit counts
BEHIND=$(git rev-list --count HEAD..upstream/main 2>/dev/null || echo "0")
AHEAD=$(git rev-list --count upstream/main..HEAD 2>/dev/null || echo "0")

# Get the latest upstream commit info
UPSTREAM_LATEST=$(git log -1 --format="%h %s" upstream/main 2>/dev/null || echo "unknown")
FORK_LATEST=$(git log -1 --format="%h %s" HEAD 2>/dev/null || echo "unknown")

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

if [ "$BEHIND" -gt 0 ]; then
  echo "🔴 [${TIMESTAMP}] FORK IS BEHIND UPSTREAM by ${BEHIND} commit(s)"
  echo "   Behind: ${BEHIND} commit(s)"
  echo "   Ahead:  ${AHEAD} commit(s)"
  echo ""
  echo "   Upstream main latest:  ${UPSTREAM_LATEST}"
  echo "   Fork main latest:      ${FORK_LATEST}"
  echo ""
  echo "   To merge: cd ${REPO_DIR} && git merge upstream/main"
else
  echo "🟢 [${TIMESTAMP}] Fork is up-to-date with upstream/main"
  echo "   Ahead: ${AHEAD} commit(s) (your fork's own changes)"
  echo "   Upstream main: ${UPSTREAM_LATEST}"
fi
