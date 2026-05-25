#!/usr/bin/env bash
# =============================================================================
# export_and_sync.sh
#
# Standalone unattended export + GitHub sync path.
# Intended for cron/@reboot so remote GitHub is refreshed even when the full
# crawler pipeline is not manually invoked during the day.
# =============================================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.crawl_automation.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

SCRIPTS_DIR="${SCRIPTS_DIR:-$REPO_DIR/scripts}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
LOG_DIR="${LOG_DIR:-$REPO_DIR/logs}"
EXPORT_TABLES="${EXPORT_TABLES:-studios instructors classes}"
GIT_SYNC_BRANCH="${GIT_SYNC_BRANCH:-crawl-data}"

mkdir -p "$LOG_DIR"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
exec > >(tee -a "$LOG_DIR/export-sync-${RUN_ID}.log") 2>&1

set -a
source /etc/environment 2>/dev/null || true
set +a

echo "=============================="
echo "Export + Sync"
echo "Run ID      : $RUN_ID"
echo "Repo        : $REPO_DIR"
echo "Git branch  : $GIT_SYNC_BRANCH"
echo "Tables      : $EXPORT_TABLES"
echo "=============================="

cd "$REPO_DIR"

"$VENV_DIR/bin/python3" "$SCRIPTS_DIR/export_crawl_snapshots.py" \
  --stamp "$RUN_ID" \
  --tables $EXPORT_TABLES

"$VENV_DIR/bin/python3" "$SCRIPTS_DIR/git_watch.py" \
  --once \
  --branch "$GIT_SYNC_BRANCH" \
  --create-branch

echo "Done: export + sync"