#!/usr/bin/env python3
"""
Export crawl database tables to versioned JSON and CSV snapshots.

Default behavior:
- exports `studios`, `instructors`, and `classes`
- writes both JSON and CSV under `data/exports/<stamp>/`
- writes `latest/` copies for stable Git-tracked paths
- emits a manifest with row counts and file paths

Environment:
- DATABASE_URL=postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl

Usage:
  python scripts/export_crawl_snapshots.py
  python scripts/export_crawl_snapshots.py --tables classes instructors
  python scripts/export_crawl_snapshots.py --stamp 2026-05-22
  python scripts/export_crawl_snapshots.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT_ROOT = REPO_ROOT / "data" / "exports"
DEFAULT_TABLES = ["studios", "instructors", "classes"]


def get_conn():
    url = os.environ.get("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "yogacrawl"),
        user=os.environ.get("PGUSER", "yogacrawl"),
        password=os.environ.get("PGPASSWORD", "yogacrawl"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export crawl DB snapshots to JSON and CSV")
    parser.add_argument(
        "--tables",
        nargs="+",
        default=DEFAULT_TABLES,
        help=f"Tables to export (default: {' '.join(DEFAULT_TABLES)})",
    )
    parser.add_argument(
        "--out-dir",
        default=str(EXPORT_ROOT),
        help="Output directory for snapshots (default: data/exports)",
    )
    parser.add_argument(
        "--stamp",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
        help="Snapshot directory name (default: current UTC timestamp)",
    )
    parser.add_argument(
        "--latest-dir",
        default="latest",
        help="Directory name for stable latest copies (default: latest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be exported without writing files",
    )
    return parser.parse_args()


def get_columns(cur, table: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [row["column_name"] for row in cur.fetchall()]


def choose_order(columns: list[str]) -> str | None:
    for candidate in ("id", "crawled_at", "updated_at", "created_at", "name", "title"):
        if candidate in columns:
            return candidate
    return None


STREAM_BATCH_SIZE = 2000  # rows fetched per round-trip from server-side cursor


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    return value


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: normalize_value(v) for k, v in row.items()}


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: normalize_value(v) for k, v in row.items()} for row in rows]


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            serialized = {}
            for column in columns:
                value = row.get(column)
                if isinstance(value, (dict, list)):
                    serialized[column] = json.dumps(value, ensure_ascii=False)
                elif value is None:
                    serialized[column] = ""
                else:
                    serialized[column] = str(value)
            writer.writerow(serialized)


def export_table(conn, table: str, snapshot_dir: Path, latest_dir: Path, dry_run: bool) -> dict[str, Any]:
    """Stream-export a table using a server-side cursor to avoid loading all rows into RAM."""
    meta_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    columns = get_columns(meta_cur, table)
    meta_cur.close()
    if not columns:
        raise RuntimeError(f"Table '{table}' does not exist or has no columns")

    quoted_columns = ", ".join(f'"{c}"' for c in columns)
    order_by = choose_order(columns)
    sql = f'SELECT {quoted_columns} FROM "{table}"'
    if order_by:
        sql += f' ORDER BY "{order_by}"'

    snapshot_json = snapshot_dir / f"{table}.json"
    snapshot_csv = snapshot_dir / f"{table}.csv"
    latest_json = latest_dir / f"{table}.json"
    latest_csv = latest_dir / f"{table}.csv"

    if dry_run:
        count_cur = conn.cursor()
        count_cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        count = count_cur.fetchone()[0]
        count_cur.close()
        log.info("[DRY-RUN] %s: %d rows -> %s and %s", table, count, snapshot_json, snapshot_csv)
        return {
            "table": table,
            "row_count": count,
            "columns": columns,
            "snapshot_json": str(snapshot_json.relative_to(REPO_ROOT)),
            "snapshot_csv": str(snapshot_csv.relative_to(REPO_ROOT)),
            "latest_json": str(latest_json.relative_to(REPO_ROOT)),
            "latest_csv": str(latest_csv.relative_to(REPO_ROOT)),
        }

    row_count = 0
    # Named server-side cursor: DB sends STREAM_BATCH_SIZE rows per round-trip
    with conn.cursor(f"export_{table}", cursor_factory=psycopg2.extras.RealDictCursor) as stream_cur:
        stream_cur.itersize = STREAM_BATCH_SIZE
        stream_cur.execute(sql)

        with (
            snapshot_json.open("w", encoding="utf-8") as sjf,
            snapshot_csv.open("w", encoding="utf-8", newline="") as scf,
            latest_json.open("w", encoding="utf-8") as ljf,
            latest_csv.open("w", encoding="utf-8", newline="") as lcf,
        ):
            csv_writers = [csv.DictWriter(f, fieldnames=columns) for f in (scf, lcf)]
            for w in csv_writers:
                w.writeheader()

            for f in (sjf, ljf):
                f.write("[\n")

            first_row = True
            for raw_row in stream_cur:
                row = normalize_row(dict(raw_row))
                row_json = json.dumps(row, ensure_ascii=False)

                for f in (sjf, ljf):
                    if not first_row:
                        f.write(",\n")
                    f.write(row_json)

                serialized: dict[str, str] = {}
                for col in columns:
                    val = row.get(col)
                    if isinstance(val, (dict, list)):
                        serialized[col] = json.dumps(val, ensure_ascii=False)
                    elif val is None:
                        serialized[col] = ""
                    else:
                        serialized[col] = str(val)
                for w in csv_writers:
                    w.writerow(serialized)

                first_row = False
                row_count += 1

            for f in (sjf, ljf):
                f.write("\n]")

    log.info("Exported %s: %d rows", table, row_count)
    return {
        "table": table,
        "row_count": row_count,
        "columns": columns,
        "snapshot_json": str(snapshot_json.relative_to(REPO_ROOT)),
        "snapshot_csv": str(snapshot_csv.relative_to(REPO_ROOT)),
        "latest_json": str(latest_json.relative_to(REPO_ROOT)),
        "latest_csv": str(latest_csv.relative_to(REPO_ROOT)),
    }


def dry_run_plan(table: str, snapshot_dir: Path, latest_dir: Path) -> dict[str, Any]:
    snapshot_json = snapshot_dir / f"{table}.json"
    snapshot_csv = snapshot_dir / f"{table}.csv"
    latest_json = latest_dir / f"{table}.json"
    latest_csv = latest_dir / f"{table}.csv"
    log.info("[DRY-RUN] %s: unresolved row count -> %s and %s", table, snapshot_json, snapshot_csv)
    return {
        "table": table,
        "row_count": None,
        "columns": [],
        "snapshot_json": str(snapshot_json.relative_to(REPO_ROOT)),
        "snapshot_csv": str(snapshot_csv.relative_to(REPO_ROOT)),
        "latest_json": str(latest_json.relative_to(REPO_ROOT)),
        "latest_csv": str(latest_csv.relative_to(REPO_ROOT)),
    }


def main() -> None:
    args = parse_args()

    out_dir = Path(args.out_dir)
    snapshot_dir = out_dir / args.stamp
    latest_dir = out_dir / args.latest_dir

    if not args.dry_run:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        latest_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": args.stamp,
        "tables": [],
    }

    log.info("Connecting to PostgreSQL...")
    try:
        conn = get_conn()
    except psycopg2.OperationalError as exc:
        if not args.dry_run:
            raise
        log.warning("[DRY-RUN] DB connection unavailable: %s", exc)
        for table in args.tables:
            manifest["tables"].append(dry_run_plan(table, snapshot_dir, latest_dir))
        log.info("[DRY-RUN] Manifest would be written to %s and %s", snapshot_dir / "manifest.json", latest_dir / "manifest.json")
        return

    try:
        for table in args.tables:
            manifest["tables"].append(export_table(conn, table, snapshot_dir, latest_dir, args.dry_run))
    finally:
        conn.close()

    if args.dry_run:
        log.info("[DRY-RUN] Manifest would be written to %s and %s", snapshot_dir / "manifest.json", latest_dir / "manifest.json")
        return

    write_json(snapshot_dir / "manifest.json", manifest)
    write_json(latest_dir / "manifest.json", manifest)
    log.info("Done. Snapshot: %s", snapshot_dir.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()