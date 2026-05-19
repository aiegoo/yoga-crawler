#!/usr/bin/env bash
# =============================================================================
# pipeline.sh — Yoga Resource Crawl Pipeline
# Runs on EC2 (ubuntu@ip-172-31-32-23, ap-northeast-2)
#
# Schedule (crontab -e):
#   0 18 * * *  /home/ubuntu/crawler/pipeline.sh >> /home/ubuntu/crawler/logs/pipeline.log 2>&1
#   (18:00 UTC = 03:00 KST)
#
# Manual run:
#   bash /home/ubuntu/crawler/pipeline.sh
#   bash /home/ubuntu/crawler/pipeline.sh --dry-run
#   bash /home/ubuntu/crawler/pipeline.sh --only studios
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="/home/ubuntu"
SCRIPTS_DIR="/home/ubuntu/scripts"
DATA_DIR="/home/ubuntu/data"
VENV_DIR="/home/ubuntu/venv"
LOG_DIR="/home/ubuntu/crawler/logs"
S3_BUCKET="yogaq-crawl-raw-ap2"
REGION="ap-northeast-2"
DATE=$(date -u +%Y-%m-%d)
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)

DRY_RUN=false
ONLY=""

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) DRY_RUN=true; shift ;;
    --only)    ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/pipeline-${RUN_ID}.log") 2>&1

echo "=============================="
echo "Yoga Crawl Pipeline"
echo "Run ID : $RUN_ID"
echo "Date   : $DATE"
echo "Dry run: $DRY_RUN"
echo "Only   : ${ONLY:-all}"
echo "=============================="

# Load env vars from /etc/environment (API keys)
set -a
# shellcheck disable=SC1091
source /etc/environment 2>/dev/null || true
set +a

# Activate Python virtualenv
source "$VENV_DIR/bin/activate"

cd "$REPO_DIR"
export PYTHONPATH="$SCRIPTS_DIR"

DRY_FLAG=""
$DRY_RUN && DRY_FLAG="--dry-run"

S3_FLAG=""
$DRY_RUN || S3_FLAG="--s3-sync"

# ── 1. Studios (Kakao + Naver) ────────────────────────────────────────────────
if [[ -z "$ONLY" || "$ONLY" == "studios" ]]; then
  echo ""
  echo ">>> [1/3] Scraping yoga studios..."
  python "$SCRIPTS_DIR/scrape_studios.py" \
    --all-cities \
    --delay 1.5 \
    --out-dir "$DATA_DIR/studios" \
    $DRY_FLAG \
    $S3_FLAG \
    && echo "    Studios: OK" \
    || echo "    Studios: FAILED (continuing)"
fi

# ── 2. Instructors (Yoga Alliance + Instagram) ────────────────────────────────
if [[ -z "$ONLY" || "$ONLY" == "instructors" ]]; then
  echo ""
  echo ">>> [2/3] Scraping instructors..."
  python "$SCRIPTS_DIR/scrape_instructors.py" \
    --source yogaalliance \
    --city Seoul \
    --pages 5 \
    --delay 2.0 \
    $DRY_FLAG \
    && echo "    Instructors: OK" \
    || echo "    Instructors: FAILED (continuing)"

  # Sync instructors to S3
  if ! $DRY_RUN; then
    aws s3 sync "$DATA_DIR/instructors/" \
      "s3://${S3_BUCKET}/${DATE}/instructors/" \
      --exclude "*.sql" \
      --region "$REGION" \
      && echo "    Instructors S3 sync: OK" \
      || echo "    Instructors S3 sync: FAILED"
  fi
fi

# ── 3. Associations ───────────────────────────────────────────────────────────
if [[ -z "$ONLY" || "$ONLY" == "associations" ]]; then
  echo ""
  echo ">>> [3/3] Scraping associations..."
  python "$SCRIPTS_DIR/scrape_associations.py" \
    --source all \
    --pages 5 \
    --delay 2.0 \
    --out-dir "$DATA_DIR/associations" \
    $DRY_FLAG \
    $S3_FLAG \
    && echo "    Associations: OK" \
    || echo "    Associations: FAILED (continuing)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=============================="
echo "Pipeline complete: $RUN_ID"

if ! $DRY_RUN; then
  STUDIO_COUNT=$(python3 -c "
import json, pathlib
f = pathlib.Path('data/studios/studios_raw.json')
print(len(json.loads(f.read_text())) if f.exists() else 0)
" 2>/dev/null || echo 0)

  INSTRUCTOR_COUNT=$(python3 -c "
import json, pathlib
f = pathlib.Path('data/instructors/instructors_raw.json')
print(len(json.loads(f.read_text())) if f.exists() else 0)
" 2>/dev/null || echo 0)

  ASSOC_COUNT=$(python3 -c "
import json, pathlib
f = pathlib.Path('data/associations/associations_raw.json')
print(len(json.loads(f.read_text())) if f.exists() else 0)
" 2>/dev/null || echo 0)

  echo "  Studios      : $STUDIO_COUNT"
  echo "  Instructors  : $INSTRUCTOR_COUNT"
  echo "  Associations : $ASSOC_COUNT"
  echo ""
  echo "  S3 bucket    : s3://${S3_BUCKET}/${DATE}/"
fi
echo "=============================="
