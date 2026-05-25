#!/usr/bin/env python3
"""report_schedule_kpi.py

Compute schedule coverage KPIs from crawl-data exports.

Usage:
  python scripts/report_schedule_kpi.py
  python scripts/report_schedule_kpi.py --ref origin/crawl-data --path data/exports/latest/classes.json
  python scripts/report_schedule_kpi.py --file data/exports/latest/classes.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _load_rows_from_git(repo: Path, ref: str, path: str) -> list[dict]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(proc.stdout)


def _load_rows_from_file(file_path: Path) -> list[dict]:
    return json.loads(file_path.read_text(encoding="utf-8"))


def _has_schedule(row: dict) -> bool:
    sch = row.get("schedule")
    return isinstance(sch, dict) and bool(sch)


def _fmt(counter: Counter) -> str:
    return "; ".join(f"{k}:{v}" for k, v in counter.most_common())


def main() -> None:
    parser = argparse.ArgumentParser(description="Schedule coverage KPI report")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]), help="Path to yoga-crawler repo")
    parser.add_argument("--ref", default="origin/crawl-data", help="Git ref to read from")
    parser.add_argument("--path", default="data/exports/latest/classes.json", help="Path to classes JSON within ref")
    parser.add_argument("--file", help="Read from local file instead of git ref")
    args = parser.parse_args()

    repo = Path(args.repo)
    if args.file:
        rows = _load_rows_from_file(Path(args.file))
        source_label = args.file
    else:
        rows = _load_rows_from_git(repo, args.ref, args.path)
        source_label = f"{args.ref}:{args.path}"

    total = len(rows)
    with_schedule = [r for r in rows if _has_schedule(r)]
    without_schedule = total - len(with_schedule)

    with_source = Counter((r.get("source") or "unknown") for r in with_schedule)
    schedule_types = Counter(((r.get("schedule") or {}).get("type") or "(none)") for r in with_schedule)

    studio_linked = [r for r in with_schedule if r.get("studio_id") is not None]
    linked_studios = len({r.get("studio_id") for r in studio_linked})

    parsed = []
    for r in rows:
        t = r.get("crawled_at")
        if not t:
            continue
        try:
            parsed.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
        except Exception:
            continue

    print(f"SOURCE={source_label}")
    print(f"TOTAL_CLASS_ROWS={total}")
    print(f"ROWS_WITH_SCHEDULE={len(with_schedule)}")
    print(f"ROWS_WITHOUT_SCHEDULE={without_schedule}")
    print(f"STUDIO_LINKED_SCHEDULE_ROWS={len(studio_linked)}")
    print(f"STUDIOS_WITH_LINKED_SCHEDULE={linked_studios}")
    print(f"WITH_SCHEDULE_BY_SOURCE={_fmt(with_source)}")
    print(f"SCHEDULE_TYPES={_fmt(schedule_types)}")

    if parsed:
        mn, mx = min(parsed), max(parsed)
        now = datetime.now(timezone.utc)
        print(f"CRAWLED_AT_RANGE={mn.isoformat()} -> {mx.isoformat()}")
        print(f"NOW_UTC={now.isoformat()}")

        freshness = defaultdict(int)
        for r in with_schedule:
            t = r.get("crawled_at")
            if not t:
                continue
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                continue
            days = (now - dt).days
            if days <= 1:
                freshness["0-1d"] += 1
            elif days <= 7:
                freshness["2-7d"] += 1
            elif days <= 30:
                freshness["8-30d"] += 1
            else:
                freshness["31d+"] += 1
        print("FRESHNESS=" + "; ".join(f"{k}:{freshness[k]}" for k in ["0-1d", "2-7d", "8-30d", "31d+"]))


if __name__ == "__main__":
    main()
