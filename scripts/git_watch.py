#!/usr/bin/env python3
"""
git_watch.py — Poll for repo changes and auto-commit/push in granular groups.

How it works
------------
Every --interval seconds the watcher checks `git status`. If changes are
found and they have been *stable* for --settle seconds (no new modifications),
the files are grouped into logical commits and pushed.

Groups (committed in order)
---------------------------
  scrapers   scripts/scrape_*.py
  db         scripts/db_*.sql  scripts/db_*.py
  pipeline   pipeline.sh  scripts/pipeline.sh  scripts/git_*.sh
  config     requirements.txt  .env.example  .gitignore
  docs       *.md
  other      everything else (except data/ which is gitignored)

Usage
-----
  # Run in foreground (Ctrl-C to stop)
  python scripts/git_watch.py

  # Background via nohup
  nohup python scripts/git_watch.py >> logs/git_watch.log 2>&1 &

  # Custom timing
  python scripts/git_watch.py --interval 120 --settle 30

  # Dry-run (no actual commits or pushes)
  python scripts/git_watch.py --dry-run

  # Push once immediately and exit
  python scripts/git_watch.py --once
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("git_watch")

# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_KB = 500   # block files larger than this

# Commit groups: (label, list of glob patterns)
COMMIT_GROUPS: list[tuple[str, list[str]]] = [
    ("scraper: update crawl scripts",         ["scripts/scrape_*.py"]),
    ("db: schema and data loader",            ["scripts/db_*.sql", "scripts/db_*.py"]),
    ("pipeline: orchestration and watcher",   ["pipeline.sh", "scripts/pipeline.sh",
                                               "scripts/git_*.sh", "scripts/git_*.py"]),
    ("config: dependencies and environment",  ["requirements.txt", ".env.example",
                                               ".gitignore"]),
    ("docs: README and project docs",         ["*.md", "docs/**"]),
]
CATCH_ALL_LABEL = "chore: miscellaneous updates"

# Patterns to never commit (on top of .gitignore)
NEVER_COMMIT: list[str] = [
    "data/**",
    "logs/**",
    "*.log",
    "*.pyc",
    "__pycache__/**",
    ".env",
]

# ── Git helpers ───────────────────────────────────────────────────────────────

def git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT)] + args,
        capture_output=True, text=True, check=check
    )


def changed_files() -> list[str]:
    """Return list of modified/untracked files (porcelain format)."""
    result = git(["status", "--porcelain"])
    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip()
        path = line[3:].strip().strip('"')
        if status in ("??", "M", "MM", "A", "AM", "R", "RM"):
            files.append(path)
    return files


def is_blocked(path: str) -> bool:
    """Return True if the file should never be committed."""
    for pattern in NEVER_COMMIT:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def file_size_kb(path: str) -> int:
    full = REPO_ROOT / path
    if full.is_file():
        return full.stat().st_size // 1024
    return 0


def match_group(path: str, patterns: list[str]) -> bool:
    for p in patterns:
        if fnmatch.fnmatch(path, p):
            return True
    return False


def commit_files(files: list[str], message: str, dry_run: bool) -> bool:
    """Stage files and commit. Returns True if a commit was made."""
    if not files:
        return False

    # Size guard
    blocked = [f for f in files if file_size_kb(f) > MAX_FILE_KB]
    if blocked:
        for b in blocked:
            log.error("BLOCKED large file (%dKB): %s", file_size_kb(b), b)
        log.error("Add these to .gitignore or increase MAX_FILE_KB.")
        return False

    if dry_run:
        log.info("  [DRY-RUN] Would commit %d file(s): %s", len(files), message)
        for f in files:
            log.info("    %s", f)
        return False

    git(["add", "--"] + files)

    # Check if there is actually something staged
    staged = git(["diff", "--cached", "--name-only"])
    if not staged.stdout.strip():
        return False

    result = git(["commit", "-m", message], check=False)
    if result.returncode == 0:
        log.info("  Committed: %s (%d file(s))", message, len(files))
        return True
    else:
        log.warning("  Commit failed: %s", result.stderr.strip())
        return False


def push(dry_run: bool) -> None:
    if dry_run:
        log.info("[DRY-RUN] Would push to origin master")
        return

    log.info("Pushing to origin...")
    result = git(["push", "origin", "master"], check=False)
    if result.returncode == 0:
        log.info("Push OK")
    else:
        log.error("Push failed: %s", result.stderr.strip())


def run_once(dry_run: bool) -> int:
    """
    Detect changes, commit in groups, push.
    Returns number of commits made.
    """
    all_changed = changed_files()
    eligible = [f for f in all_changed if not is_blocked(f)]

    if not eligible:
        log.debug("No eligible changes.")
        return 0

    log.info("Detected %d changed file(s)", len(eligible))

    committed = 0
    assigned: set[str] = set()

    for label, patterns in COMMIT_GROUPS:
        group = [f for f in eligible if f not in assigned and match_group(f, patterns)]
        if group:
            if commit_files(group, label, dry_run):
                committed += 1
            assigned.update(group)

    # Catch-all for anything not matched
    remaining = [f for f in eligible if f not in assigned]
    if remaining:
        if commit_files(remaining, CATCH_ALL_LABEL, dry_run):
            committed += 1

    if committed > 0:
        push(dry_run)
    else:
        log.info("No new commits to push.")

    return committed


# ── Watch loop ─────────────────────────────────────────────────────────────────

class Watcher:
    def __init__(self, interval: int, settle: int, dry_run: bool):
        self.interval = interval
        self.settle   = settle
        self.dry_run  = dry_run
        self._stop    = False
        self._last_snapshot: set[str] = set()
        self._stable_since: float = 0.0

        signal.signal(signal.SIGINT,  self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

    def _handle_stop(self, *_):
        log.info("Shutting down...")
        self._stop = True

    def _snapshot(self) -> set[str]:
        return set(changed_files())

    def run(self):
        log.info("git_watch started (interval=%ds, settle=%ds, dry_run=%s)",
                 self.interval, self.settle, self.dry_run)
        log.info("Repo: %s", REPO_ROOT)
        log.info("Press Ctrl-C to stop.")

        while not self._stop:
            now = time.monotonic()
            current = self._snapshot()

            if current != self._last_snapshot:
                # Changes detected — reset stable clock
                self._last_snapshot = current
                self._stable_since  = now
                if current:
                    log.info("Changes detected (%d file(s)) — waiting %ds for stability...",
                             len(current), self.settle)

            elif current and (now - self._stable_since) >= self.settle:
                # Files have settled — commit now
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                log.info("Files stable since %ds ago — committing... [%s]",
                         int(now - self._stable_since), ts)
                run_once(self.dry_run)
                self._last_snapshot = set()   # reset after commit
                self._stable_since  = 0.0

            # Sleep in short ticks so SIGINT is responsive
            for _ in range(min(self.interval, 10)):
                if self._stop:
                    break
                time.sleep(1)

        log.info("git_watch stopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Poll git repo and auto-commit/push changes")
    p.add_argument("--interval", type=int, default=60,
                   help="Poll interval in seconds (default: 60)")
    p.add_argument("--settle", type=int, default=20,
                   help="Seconds of no-change before committing (default: 20)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be committed without writing")
    p.add_argument("--once", action="store_true",
                   help="Commit and push any current changes once, then exit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if args.once:
        log.info("--once mode: committing current changes and exiting")
        n = run_once(args.dry_run)
        log.info("Done — %d commit(s) made", n)
        sys.exit(0)

    watcher = Watcher(
        interval=args.interval,
        settle=args.settle,
        dry_run=args.dry_run,
    )
    watcher.run()


if __name__ == "__main__":
    main()
