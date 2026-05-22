#!/usr/bin/env bash
# =============================================================================
# install_crawl_automation.sh
#
# One-time installer for unattended crawl automation.
# - ensures a full pipeline cron entry exists before shutdown
# - ensures export+sync runs automatically on reboot and throughout the day
# - writes a small env file consumed by the cron jobs
#
# Usage:
#   bash scripts/install_crawl_automation.sh
#   PIPELINE_BRANCH=plan GIT_SYNC_BRANCH=crawl-data bash scripts/install_crawl_automation.sh
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE_SCRIPT="$REPO_DIR/scripts/pipeline.sh"
EXPORT_SYNC_SCRIPT="$REPO_DIR/scripts/export_and_sync.sh"
ENV_FILE="$REPO_DIR/.crawl_automation.env"
LOG_DIR="$REPO_DIR/logs"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
SCRIPTS_DIR="$REPO_DIR/scripts"
DATA_DIR="$REPO_DIR/data"

SCHEDULE_MINUTE="${SCHEDULE_MINUTE:-0}"
SCHEDULE_HOUR="${SCHEDULE_HOUR:-7}"
SYNC_CRON_EXPR="${SYNC_CRON_EXPR:-*/30 0-8 * * *}"
SYNC_REBOOT_DELAY_SEC="${SYNC_REBOOT_DELAY_SEC:-180}"
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

CRON_CMD="set -a; . '$ENV_FILE'; set +a; bash '$PIPELINE_SCRIPT' >> '$LOG_DIR/pipeline-cron.log' 2>&1"
CRON_ENTRY="$SCHEDULE_MINUTE $SCHEDULE_HOUR * * * $CRON_CMD"
SYNC_CMD="set -a; . '$ENV_FILE'; set +a; bash '$EXPORT_SYNC_SCRIPT' >> '$LOG_DIR/export-sync-cron.log' 2>&1"
SYNC_ENTRY="$SYNC_CRON_EXPR $SYNC_CMD"
REBOOT_ENTRY="@reboot sleep ${SYNC_REBOOT_DELAY_SEC}; $SYNC_CMD"

TMP_CRON="$(mktemp)"
trap 'rm -f "$TMP_CRON"' EXIT

crontab -l 2>/dev/null | grep -Fv "$PIPELINE_SCRIPT" | grep -Fv "$EXPORT_SYNC_SCRIPT" > "$TMP_CRON" || true
printf '%s\n' "$CRON_ENTRY" >> "$TMP_CRON"
printf '%s\n' "$SYNC_ENTRY" >> "$TMP_CRON"
printf '%s\n' "$REBOOT_ENTRY" >> "$TMP_CRON"
crontab "$TMP_CRON"

echo "Installed crawl automation."
echo "  Pipeline     : ${SCHEDULE_HOUR}:${SCHEDULE_MINUTE} UTC daily"
echo "  Export+sync  : ${SYNC_CRON_EXPR} UTC"
echo "  Reboot sync  : ${SYNC_REBOOT_DELAY_SEC}s after boot"
echo "  Pipeline     : $PIPELINE_SCRIPT"
echo "  Export+sync  : $EXPORT_SYNC_SCRIPT"
echo "  Env file     : $ENV_FILE"
echo "  Log file     : $LOG_DIR/pipeline-cron.log"
echo "  Code branch  : $PIPELINE_BRANCH"
echo "  Git branch   : $GIT_SYNC_BRANCH"