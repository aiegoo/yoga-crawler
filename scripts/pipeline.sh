#!/usr/bin/env bash
# =============================================================================
# pipeline.sh — Yoga Resource Crawl Pipeline (parallel edition)
# Runs on EC2 (ubuntu@ip-172-31-32-23, ap-northeast-2)
#
# Schedule (crontab -e):
#   0 18 * * *  /home/ubuntu/crawler/pipeline.sh >> /home/ubuntu/crawler/logs/pipeline.log 2>&1
#   (18:00 UTC = 03:00 KST)
#
# Manual run:
#   bash pipeline.sh                         # full parallel run
#   bash pipeline.sh --dry-run               # log commands, no API calls
#   bash pipeline.sh --only studios          # single step
#   bash pipeline.sh --only gov              # force gov refresh
#   bash pipeline.sh --only github           # force GitHub mine
#   bash pipeline.sh --sequential            # disable parallelism (for debugging)
#
# Parallel execution plan:
#   Tier 1 (parallel): studios · instructors · associations
#   Tier 1b (serial):  gov_sangga --merge-json   (needs studios_raw.json)
#   Tier 2 (serial):   db_load                   (needs all JSON files)
#   Tier 3 (parallel): scrape_web · scrape_ig_profiles
#   Tier 4 (scheduled):scrape_github_yoga (weekly) · gov --load-db (monthly)
# =============================================================================

set -euo pipefail
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
PARALLEL=true

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)    DRY_RUN=true;  shift ;;
    --only)       ONLY="$2";     shift 2 ;;
    --sequential) PARALLEL=false; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/pipeline-${RUN_ID}.log") 2>&1

echo "=============================="
echo "Yoga Crawl Pipeline"
echo "Run ID     : $RUN_ID"
echo "Date       : $DATE"
echo "Dry run    : $DRY_RUN"
echo "Only       : ${ONLY:-all}"
echo "Parallel   : $PARALLEL"
echo "=============================="

# Load env vars from /etc/environment (API keys)
set -a
# shellcheck disable=SC1091
source /etc/environment 2>/dev/null || true
set +a

# Pull latest code
echo ">>> [0] git pull..."
git -C "$REPO_DIR" pull --ff-only origin master \
  && echo "    git pull: OK" \
  || echo "    git pull: FAILED (continuing with local code)"

source "$VENV_DIR/bin/activate"
cd "$REPO_DIR"
export PYTHONPATH="$SCRIPTS_DIR"

DRY_FLAG=""
$DRY_RUN && DRY_FLAG="--dry-run"

# ── Parallel job helpers ───────────────────────────────────────────────────────

# Arrays tracking in-flight background jobs
_BG_PIDS=()
_BG_NAMES=()
_BG_LOGS=()

# launch <name> <cmd...>
# Runs <cmd> in the background with its own log file.
# When PARALLEL=false, runs synchronously instead.
launch() {
  local name="$1"; shift
  local log="$LOG_DIR/${name}-${RUN_ID}.log"
  if $PARALLEL; then
    echo "    [bg] $name → $(basename "$log")"
    # Run in a subshell so set -e inside doesn't kill the parent
    (set +e; "$@") >"$log" 2>&1 &
    _BG_PIDS+=("$!")
    _BG_NAMES+=("$name")
    _BG_LOGS+=("$log")
  else
    echo "    [seq] $name"
    "$@" 2>&1 | tee "$log" || true
  fi
}

# collect — wait for all launched background jobs and print results.
# Prints last 15 lines of the log for any failed job.
# Returns 1 if any job failed, 0 if all succeeded.
collect() {
  [[ ${#_BG_PIDS[@]} -eq 0 ]] && return 0
  local rc=0
  for i in "${!_BG_PIDS[@]}"; do
    local pid="${_BG_PIDS[$i]}"
    local name="${_BG_NAMES[$i]}"
    local log="${_BG_LOGS[$i]}"
    if wait "$pid"; then
      echo "    ✓ $name: OK"
    else
      echo "    ✗ $name: FAILED"
      echo "      ── last lines of $log ──"
      tail -n 15 "$log" | sed 's/^/      /'
      rc=1
    fi
  done
  _BG_PIDS=(); _BG_NAMES=(); _BG_LOGS=()
  return $rc
}

# ── TIER 1 — parallel scrapers ────────────────────────────────────────────────
echo ""
echo ">>> TIER 1 — launching scrapers in parallel..."

# 1a. Studios (5 city batches run sequentially within this job)
if [[ -z "$ONLY" || "$ONLY" == "studios" ]]; then
  launch "studios" bash -c "
    set -euo pipefail
    source '$VENV_DIR/bin/activate'
    declare -a BATCHES=(
      'Seoul Busan Daegu Incheon Gwangju'
      'Daejeon Ulsan Suwon Changwon Seongnam'
      'Goyang Yongin Bucheon Cheongju Ansan'
      'Jeonju Anyang Cheonan Namyangju Hwaseong'
      'Jeju Gimhae Hanam Uijeongbu Siheung'
    )
    for batch in \"\${BATCHES[@]}\"; do
      python '$SCRIPTS_DIR/scrape_studios.py' \
        --cities \$batch \
        --delay 1.5 \
        --out-dir '$DATA_DIR/studios' \
        $DRY_FLAG \
        || echo \"  Batch [\$batch] FAILED (continuing)\"
    done
    if [[ '$DRY_RUN' != 'true' ]]; then
      aws s3 sync '$DATA_DIR/studios/' \
        's3://${S3_BUCKET}/${DATE}/studios/' \
        --exclude '*.sql' --region '$REGION' \
        && echo '  Studios S3 sync: OK' \
        || echo '  Studios S3 sync: FAILED'
    fi
  "
fi

# 1b. Instructors
if [[ -z "$ONLY" || "$ONLY" == "instructors" ]]; then
  launch "instructors" bash -c "
    set -euo pipefail
    source '$VENV_DIR/bin/activate'
    python '$SCRIPTS_DIR/scrape_instructors.py' \
      --source yogaalliance \
      --city Seoul \
      --pages 5 \
      --delay 2.0 \
      $DRY_FLAG
    if [[ '$DRY_RUN' != 'true' ]]; then
      aws s3 sync '$DATA_DIR/instructors/' \
        's3://${S3_BUCKET}/${DATE}/instructors/' \
        --exclude '*.sql' --region '$REGION'
    fi
  "
fi

# 1c. Associations
if [[ -z "$ONLY" || "$ONLY" == "associations" ]]; then
  launch "associations" bash -c "
    set -euo pipefail
    source '$VENV_DIR/bin/activate'
    python '$SCRIPTS_DIR/scrape_associations.py' \
      --source all \
      --pages 5 \
      --delay 2.0 \
      --out-dir '$DATA_DIR/associations' \
      $DRY_FLAG
    if [[ '$DRY_RUN' != 'true' ]]; then
      aws s3 sync '$DATA_DIR/associations/' \
        's3://${S3_BUCKET}/${DATE}/associations/' \
        --exclude '*.sql' --region '$REGION'
    fi
  "
fi

collect || echo "    Some Tier 1 scrapers failed — continuing to Tier 1b"

# ── TIER 1b — gov merge (depends on studios_raw.json from Tier 1) ────────────
if [[ -z "$ONLY" || "$ONLY" == "studios" || "$ONLY" == "gov" ]]; then
  if [[ -n "${SANGGA_API_KEY:-}" ]]; then
    echo ""
    echo ">>> TIER 1b — 소상공인 gov merge..."
    python "$SCRIPTS_DIR/scrape_gov_sangga.py" \
      --out-dir "$DATA_DIR/studios" \
      --merge-json \
      --delay 0.5 \
      $DRY_FLAG \
      && echo "    ✓ gov_sangga merge: OK" \
      || echo "    ✗ gov_sangga merge: FAILED (continuing)"
  else
    echo ""
    echo ">>> TIER 1b — skipping 소상공인 (SANGGA_API_KEY not set)"
  fi
fi

# ── TIER 2 — DB load (depends on all Tier 1 JSON output) ─────────────────────
if ! $DRY_RUN && [[ -z "$ONLY" || "$ONLY" == "db" ]]; then
  echo ""
  echo ">>> TIER 2 — loading all data into PostgreSQL..."
  python "$SCRIPTS_DIR/db_load.py" \
    --data-dir "$DATA_DIR" \
    && echo "    ✓ db_load: OK" \
    || echo "    ✗ db_load: FAILED (data still in S3)"
fi

# ── TIER 3 — parallel post-processors (depend on DB) ─────────────────────────
if ! $DRY_RUN; then
  echo ""
  echo ">>> TIER 3 — launching post-processors in parallel..."

  if [[ -z "$ONLY" || "$ONLY" == "web" ]]; then
    launch "scrape_web" bash -c "
      set -euo pipefail
      source '$VENV_DIR/bin/activate'
      python '$SCRIPTS_DIR/scrape_web.py' \
        --limit 200 \
        --delay 1.5 \
        $DRY_FLAG
    "
  fi

  if [[ -z "$ONLY" || "$ONLY" == "instagram" || "$ONLY" == "ig" ]]; then
    IG_MODE="oembed"
    [[ -n "${INSTAGRAM_SESSION_ID:-}" ]] && IG_MODE="instaloader"
    [[ -n "${APIFY_TOKEN:-}" ]]          && IG_MODE="apify"
    launch "scrape_ig" bash -c "
      set -euo pipefail
      source '$VENV_DIR/bin/activate'
      python '$SCRIPTS_DIR/scrape_ig_profiles.py' \
        --mode '$IG_MODE' \
        --limit 300 \
        --delay 8 \
        --discover \
        $DRY_FLAG
    "
  fi

  collect || echo "    Some Tier 3 post-processors failed — continuing"
fi

# ── TIER 4a — gov full refresh (monthly, --load-db path) ─────────────────────
if [[ ( -z "$ONLY" && "$(date -u +%d)" == "01" ) || "$ONLY" == "gov" ]]; then
  echo ""
  echo ">>> TIER 4a — gov sangga monthly refresh (소상공인 --load-db)..."

  if [[ -z "${SANGGA_API_KEY:-}" ]]; then
    echo "    SKIPPED — SANGGA_API_KEY not set"
    echo "    Register free at https://www.data.go.kr/data/15012005/openapi.do"
    echo "    Then: sudo sh -c 'echo SANGGA_API_KEY=your_key >> /etc/environment'"
  else
    python "$SCRIPTS_DIR/scrape_gov_sangga.py" \
      --out-dir "$DATA_DIR/studios" \
      --load-db \
      --delay 0.5 \
      $DRY_FLAG \
      && echo "    ✓ gov_sangga --load-db: OK" \
      || echo "    ✗ gov_sangga --load-db: FAILED"

    if ! $DRY_RUN; then
      python "$SCRIPTS_DIR/crossref_gov_kakao.py" \
        --out-dir "$DATA_DIR" \
        && echo "    ✓ crossref: OK" \
        || echo "    ✗ crossref: FAILED (script may not exist yet)"

      aws s3 cp "$DATA_DIR/crossref_report_$(date -u +%Y%m%d).csv" \
        "s3://${S3_BUCKET}/${DATE}/reports/" \
        --region "$REGION" 2>/dev/null || true
    fi
  fi
fi

# ── TIER 4b — GitHub mining (weekly, Sundays) ─────────────────────────────────
if [[ ( -z "$ONLY" && "$(date -u +%u)" == "7" ) || "$ONLY" == "github" ]]; then
  echo ""
  echo ">>> TIER 4b — GitHub yoga profile mine..."
  [[ -z "${GITHUB_TOKEN:-}" ]] \
    && echo "    NOTE: GITHUB_TOKEN not set — rate-limited to 60 req/hr"
  python "$SCRIPTS_DIR/scrape_github_yoga.py" \
    --out-dir "$DATA_DIR/github" \
    $DRY_FLAG \
    && echo "    ✓ github: OK" \
    || echo "    ✗ github: FAILED (continuing)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=============================="
echo "Pipeline complete: $RUN_ID"

if ! $DRY_RUN; then
  _count() { python3 -c "
import json, pathlib
f = pathlib.Path('$1')
print(len(json.loads(f.read_text())) if f.exists() else 0)
" 2>/dev/null || echo 0; }

  echo "  Studios      : $(_count data/studios/studios_raw.json)"
  echo "  Instructors  : $(_count data/instructors/instructors_raw.json)"
  echo "  Associations : $(_count data/associations/associations_raw.json)"
  echo ""
  echo "  Logs in      : $LOG_DIR/  (*-${RUN_ID}.log)"
  echo "  S3 bucket    : s3://${S3_BUCKET}/${DATE}/"
fi
echo "=============================="
