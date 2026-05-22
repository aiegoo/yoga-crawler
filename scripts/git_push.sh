#!/usr/bin/env bash
# =============================================================================
# git_push.sh — Granular git commit + push for yoga-crawler repo
#
# Splits changes into logical groups to keep commits small and clean.
# Checks file sizes before staging to block accidental large-file commits.
#
# Usage:
#   bash scripts/git_push.sh                        # auto-detect and commit all changes
#   bash scripts/git_push.sh --message "my msg"     # single commit with custom message
#   bash scripts/git_push.sh --dry-run              # show what would be committed
#   bash scripts/git_push.sh --check-size           # only check for large files
#   bash scripts/git_push.sh --branch crawl-data    # push to a dedicated crawl branch
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAX_FILE_KB=500        # warn if any single file > 500 KB
MAX_COMMIT_KB=2048     # warn if total staged size > 2 MB

DRY_RUN=false
CUSTOM_MSG=""
CHECK_ONLY=false
TARGET_BRANCH="${GIT_SYNC_BRANCH:-}"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)     DRY_RUN=true; shift ;;
    --check-size)  CHECK_ONLY=true; shift ;;
    --branch)      TARGET_BRANCH="$2"; shift 2 ;;
    --message|-m)  CUSTOM_MSG="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

cd "$REPO_DIR"

current_branch() {
  git rev-parse --abbrev-ref HEAD 2>/dev/null || echo master
}

if [[ -z "$TARGET_BRANCH" ]]; then
  TARGET_BRANCH="$(current_branch)"
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'

log()  { echo -e "${GREEN}[git_push]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }

ensure_branch() {
  local current
  current="$(current_branch)"
  [[ "$current" == "$TARGET_BRANCH" ]] && return 0

  if $DRY_RUN; then
    log "[DRY-RUN] Would switch from $current to $TARGET_BRANCH"
    return 0
  fi

  if git show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git checkout "$TARGET_BRANCH"
  elif git ls-remote --exit-code --heads origin "$TARGET_BRANCH" >/dev/null 2>&1; then
    git checkout -b "$TARGET_BRANCH" --track "origin/$TARGET_BRANCH"
  else
    git checkout -b "$TARGET_BRANCH"
  fi
}

check_large_files() {
  local staged_files=("$@")
  local blocked=false
  for f in "${staged_files[@]}"; do
    [[ -f "$f" ]] || continue
    local kb
    kb=$(du -k "$f" | cut -f1)
    if (( kb > MAX_FILE_KB )); then
      err "Large file: $f (${kb}KB > ${MAX_FILE_KB}KB limit)"
      err "  Add to .gitignore or use git-lfs if this is intentional."
      blocked=true
    fi
  done
  $blocked && return 1 || return 0
}

check_total_size() {
  local total_kb=0
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    total_kb=$(( total_kb + $(du -k "$f" | cut -f1) ))
  done < <(git diff --cached --name-only)
  if (( total_kb > MAX_COMMIT_KB )); then
    warn "Total staged size: ${total_kb}KB (>${MAX_COMMIT_KB}KB). Consider splitting."
  fi
}

commit_group() {
  local msg="$1"; shift
  local files=("$@")

  # Filter to only files that exist and have changes
  local to_stage=()
  for f in "${files[@]}"; do
    if git ls-files --error-unmatch "$f" &>/dev/null 2>&1; then
      # Tracked file — check if modified
      git diff --quiet "$f" 2>/dev/null && git diff --cached --quiet "$f" 2>/dev/null && continue
    else
      # Untracked — check if it exists
      [[ -f "$f" ]] || continue
    fi
    to_stage+=("$f")
  done

  [[ ${#to_stage[@]} -eq 0 ]] && { log "  (no changes in: $msg)"; return 0; }

  log "Group: $msg"
  log "  Files: ${to_stage[*]}"

  # Size check
  check_large_files "${to_stage[@]}" || return 1

  if $DRY_RUN; then
    log "  [DRY-RUN] Would commit: $msg"
    return 0
  fi

  git add -- "${to_stage[@]}"
  check_total_size
  git commit -m "$msg" || true
  log "  Committed: $msg"
}

push_all() {
  if $DRY_RUN; then
    log "[DRY-RUN] Would push to origin $TARGET_BRANCH"
    return 0
  fi
  log "Pushing to origin/$TARGET_BRANCH..."
  git push -u origin "$TARGET_BRANCH"
  log "Push complete."
}

# ── Size-check-only mode ──────────────────────────────────────────────────────
if $CHECK_ONLY; then
  log "Checking for large files in working tree..."
  found=false
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    kb=$(du -k "$f" | cut -f1)
    if (( kb > MAX_FILE_KB )); then
      warn "  ${kb}KB  $f"
      found=true
    fi
  done < <(git ls-files && git ls-files --others --exclude-standard)
  $found || log "No large files found."
  exit 0
fi

# ── Custom single-message mode ────────────────────────────────────────────────
if [[ -n "$CUSTOM_MSG" ]]; then
  log "Single commit mode: $CUSTOM_MSG"
  mapfile -t all_changed < <(
    git diff --name-only
    git diff --cached --name-only
    git ls-files --others --exclude-standard
  )
  [[ ${#all_changed[@]} -eq 0 ]] && { log "Nothing to commit."; exit 0; }
  check_large_files "${all_changed[@]}" || exit 1
  if ! $DRY_RUN; then
    git add -- "${all_changed[@]}"
    check_total_size
    git commit -m "$CUSTOM_MSG"
    push_all
  else
    log "[DRY-RUN] Would commit ${#all_changed[@]} files: $CUSTOM_MSG"
  fi
  exit 0
fi

# ── Auto granular mode ────────────────────────────────────────────────────────
log "Auto granular commit mode"
log "Repo: $REPO_DIR"
log "Target branch: $TARGET_BRANCH"
echo ""

ensure_branch

# Group 1: Core scraper scripts
commit_group "scraper: update crawl scripts" \
  scripts/scrape_studios.py \
  scripts/scrape_instructors.py \
  scripts/scrape_associations.py

# Group 2: Database scripts
commit_group "db: schema and data loader" \
  scripts/db_setup.sql \
  scripts/db_load.py

# Group 3: Pipeline orchestration
commit_group "pipeline: orchestration and automation" \
  pipeline.sh \
  scripts/pipeline.sh \
  scripts/git_push.sh

# Group 4: Project config / deps
commit_group "config: dependencies and environment" \
  requirements.txt \
  .env.example \
  .gitignore

# Group 5: Docs
commit_group "docs: README and project docs" \
  README.md

# Group 6: Crawl data (JSON + SQL seed files)
mapfile -t data_files < <(
  git -C "$REPO_DIR" ls-files --others --exclude-standard -- "data/**" 2>/dev/null || true
  git -C "$REPO_DIR" diff --name-only -- "data/**" 2>/dev/null || true
)
if [[ ${#data_files[@]} -gt 0 ]]; then
  commit_group "data: crawl output $(date -u +%Y-%m-%d)" "${data_files[@]}"
fi

# Group 7: Everything else not yet staged (catch-all for new files)
mapfile -t remaining < <(
  git status --porcelain | grep -E "^\?\?|^ M|^M " | awk '{print $2}' | grep -v "^data/" || true
)
if [[ ${#remaining[@]} -gt 0 ]]; then
  commit_group "chore: remaining changes" "${remaining[@]}"
fi

# Check if there's anything to push
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$TARGET_BRANCH" 2>/dev/null || echo "none")

if [[ "$LOCAL" == "$REMOTE" ]]; then
  log "Nothing to push — already up to date."
else
  push_all
fi

echo ""
log "Done. Log:"
git log --oneline -5
