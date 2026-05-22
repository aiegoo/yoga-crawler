#!/usr/bin/env bash
# =============================================================================
# install_crawl_automation.sh
#
# One-time installer for unattended crawl automation.
# - ensures a daily cron entry exists
# - writes a small env file consumed by the cron job
# - defaults to DB snapshot export + auto-publish to crawl-data
#
# Usage:
#   bash scripts/install_crawl_automation.sh
#   PIPELINE_BRANCH=plan GIT_SYNC_BRANCH=crawl-data bash scripts/install_crawl_automation.sh
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE_SCRIPT="$REPO_DIR/scripts/pipeline.sh"
ENV_FILE="$REPO_DIR/.crawl_automation.env"
LOG_DIR="$REPO_DIR/logs"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
SCRIPTS_DIR="$REPO_DIR/scripts"
DATA_DIR="$REPO_DIR/data"

SCHEDULE_MINUTE="${SCHEDULE_MINUTE:-0}"
SCHEDULE_HOUR="${SCHEDULE_HOUR:-18}"
PIPELINE_BRANCH="${PIPELINE_BRANCH:-master}"
AUTO_EXPORT_SNAPSHOTS="${AUTO_EXPORT_SNAPSHOTS:-true}"
AUTO_GIT_SYNC="${AUTO_GIT_SYNC:-true}"
GIT_SYNC_BRANCH="${GIT_SYNC_BRANCH:-crawl-data}"
EXPORT_TABLES="${EXPORT_TABLES:-studios instructors classes}"

mkdir -p "$LOG_DIR"

cat > "$ENV_FILE" <<EOF
REPO_DIR='${REPO_DIR}'
SCRIPTS_DIR='${SCRIPTS_DIR}'
DATA_DIR='${DATA_DIR}'
VENV_DIR='${VENV_DIR}'
LOG_DIR='${LOG_DIR}'
PIPELINE_BRANCH='${PIPELINE_BRANCH}'
AUTO_EXPORT_SNAPSHOTS='${AUTO_EXPORT_SNAPSHOTS}'
AUTO_GIT_SYNC='${AUTO_GIT_SYNC}'
GIT_SYNC_BRANCH='${GIT_SYNC_BRANCH}'
EXPORT_TABLES='${EXPORT_TABLES}'
EOF

CRON_CMD=". '$ENV_FILE'; bash '$PIPELINE_SCRIPT' >> '$LOG_DIR/pipeline-cron.log' 2>&1"
CRON_ENTRY="$SCHEDULE_MINUTE $SCHEDULE_HOUR * * * $CRON_CMD"

TMP_CRON="$(mktemp)"
trap 'rm -f "$TMP_CRON"' EXIT

crontab -l 2>/dev/null | grep -Fv "$PIPELINE_SCRIPT" > "$TMP_CRON" || true
printf '%s\n' "$CRON_ENTRY" >> "$TMP_CRON"
crontab "$TMP_CRON"

echo "Installed crawl automation."
echo "  Schedule    : ${SCHEDULE_HOUR}:${SCHEDULE_MINUTE} UTC daily"
echo "  Pipeline     : $PIPELINE_SCRIPT"
echo "  Env file     : $ENV_FILE"
echo "  Log file     : $LOG_DIR/pipeline-cron.log"
echo "  Code branch  : $PIPELINE_BRANCH"
echo "  Git branch   : $GIT_SYNC_BRANCH"