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

# Ensure aws CLI is on PATH for cron (cron uses minimal PATH)
export PATH="/usr/local/bin:$PATH"

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="/home/ubuntu/yoga-crawler"
SCRIPTS_DIR="/home/ubuntu/yoga-crawler/scripts"
DATA_DIR="/home/ubuntu/yoga-crawler/data"
VENV_DIR="/home/ubuntu/venv"
LOG_DIR="/home/ubuntu/yoga-crawler/logs"
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

# Pull latest code from git
echo ">>> [0/4] git pull..."
git -C "$REPO_DIR" pull --ff-only origin master \
  && echo "    git pull: OK" \
  || echo "    git pull: FAILED (continuing with local code)"

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
  echo ">>> [1/3] Scraping yoga studios (batched by city to limit memory)..."
  # Run in 5 batches of 5 cities — avoids OOM on t3.micro (1 GB RAM)
  # scrape_studios.py merges with existing studios_raw.json on each run
  declare -a CITY_BATCHES=(
    "Seoul Busan Daegu Incheon Gwangju"
    "Daejeon Ulsan Suwon Changwon Seongnam"
    "Goyang Yongin Bucheon Cheongju Ansan"
    "Jeonju Anyang Cheonan Namyangju Hwaseong"
    "Jeju Gimhae Hanam Uijeongbu Siheung"
  )
  STUDIO_STATUS="OK"
  for batch in "${CITY_BATCHES[@]}"; do
    # shellcheck disable=SC2086
    python "$SCRIPTS_DIR/scrape_studios.py" \
      --cities $batch \
      --delay 1.5 \
      --out-dir "$DATA_DIR/studios" \
      $DRY_FLAG \
      || { STUDIO_STATUS="FAILED"; echo "    Batch [$batch] FAILED (continuing)"; }
  done
  # S3 sync once after all batches complete
  if [[ "$STUDIO_STATUS" == "OK" ]] && ! $DRY_RUN; then
    aws s3 sync "$DATA_DIR/studios/" \
      "s3://${S3_BUCKET}/${DATE}/studios/" \
      --exclude "*.sql" \
      --region "$REGION" \
      && echo "    Studios S3 sync: OK" \
      || echo "    Studios S3 sync: FAILED"
  fi
  echo "    Studios: $STUDIO_STATUS"
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
    $DRY_FLAG \
    $S3_FLAG \
    && echo "    Associations: OK" \
    || echo "    Associations: FAILED (continuing)"
fi

# ── 4. Load into PostgreSQL ───────────────────────────────────────────────────
if ! $DRY_RUN && [[ -z "$ONLY" || "$ONLY" == "db" ]]; then
  echo ""
  echo ">>> [4/4] Loading data into PostgreSQL..."
  python "$SCRIPTS_DIR/db_load.py" \
    --data-dir "$DATA_DIR" \
    && echo "    DB load: OK" \
    || echo "    DB load: FAILED (data still in S3)"
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

# ── 5. Gov sangga (소상공인마당) — monthly refresh ────────────────────────────
# Runs automatically on day 1 of each month, OR when --only gov is passed.
# Requires SANGGA_API_KEY env var (free API key from https://data.go.kr).
# Register at: https://www.data.go.kr/data/15012005/openapi.do
if [[ -z "$ONLY" && "$(date -u +%d)" == "01" ]] || [[ "$ONLY" == "gov" ]]; then
  echo ""
  echo ">>> [5/5] Gov sangga monthly refresh (소상공인 상가정보 API)..."

  if [[ -z "${SANGGA_API_KEY:-}" ]]; then
    echo "    SKIPPED — SANGGA_API_KEY not set."
    echo "    To enable: sudo sh -c 'echo SANGGA_API_KEY=your_key >> /etc/environment'"
  else
    python "$SCRIPTS_DIR/scrape_gov_sangga.py" \
      --load-db \
      --delay 0.5 \
      $DRY_FLAG \
      && echo "    Gov sangga: OK" \
      || echo "    Gov sangga: FAILED (continuing)"

    # Cross-reference after load
    if ! $DRY_RUN; then
      python "$SCRIPTS_DIR/crossref_gov_kakao.py" \
        --out-dir "$DATA_DIR" \
        && echo "    Crossref: OK" \
        || echo "    Crossref: FAILED"

      aws s3 cp "$DATA_DIR/crossref_report_$(date -u +%Y%m%d).csv" \
        "s3://${S3_BUCKET}/${DATE}/reports/" \
        --region "$REGION" 2>/dev/null || true
    fi
  fi
fi
