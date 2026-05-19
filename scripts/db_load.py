#!/usr/bin/env python3
"""
db_load.py — Load crawled JSON data into PostgreSQL yogacrawl database.

Usage
-----
  # Load all tables from default data/ dirs
  python scripts/db_load.py

  # Load specific table
  python scripts/db_load.py --tables studios

  # Load from a specific date's S3 snapshot (downloads first)
  python scripts/db_load.py --s3-date 2026-05-19

  # Dry-run: show counts without writing
  python scripts/db_load.py --dry-run

Environment
-----------
  DATABASE_URL=postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl
  (or PGHOST / PGUSER / PGPASSWORD / PGDATABASE individually)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
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

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "data"

S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "yogaq-crawl-raw-ap2")

# ── DB connection ─────────────────────────────────────────────────────────────

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


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_studios(conn, json_path: Path, dry_run: bool = False) -> int:
    if not json_path.exists():
        log.warning("Studios JSON not found: %s", json_path)
        return 0

    studios: list[dict] = json.loads(json_path.read_text(encoding="utf-8"))
    if dry_run:
        log.info("[DRY-RUN] Would upsert %d studios", len(studios))
        return len(studios)

    sql = """
        INSERT INTO studios
          (source, source_id, name, category, phone, address, road_address,
           lng, lat, place_url, crawled_at)
        VALUES
          (%(source)s, %(source_id)s, %(name)s, %(category)s, %(phone)s,
           %(address)s, %(road_address)s, %(lng)s, %(lat)s, %(place_url)s,
           %(crawled_at)s)
        ON CONFLICT (source, source_id) DO UPDATE SET
          name         = EXCLUDED.name,
          category     = EXCLUDED.category,
          phone        = EXCLUDED.phone,
          address      = EXCLUDED.address,
          road_address = EXCLUDED.road_address,
          lng          = EXCLUDED.lng,
          lat          = EXCLUDED.lat,
          place_url    = EXCLUDED.place_url,
          crawled_at   = EXCLUDED.crawled_at
    """

    rows = []
    for s in studios:
        try:
            lng = float(s.get("x") or s.get("lng") or 0) or None
            lat = float(s.get("y") or s.get("lat") or 0) or None
        except (ValueError, TypeError):
            lng = lat = None

        rows.append({
            "source":       s.get("source", "unknown"),
            "source_id":    s.get("source_id") or None,
            "name":         s.get("name", ""),
            "category":     s.get("category") or None,
            "phone":        s.get("phone") or None,
            "address":      s.get("address") or None,
            "road_address": s.get("road_address") or None,
            "lng":          lng,
            "lat":          lat,
            "place_url":    s.get("place_url") or None,
            "crawled_at":   s.get("crawled_at") or datetime.now(timezone.utc).isoformat(),
        })

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
    conn.commit()
    log.info("Upserted %d studios", len(rows))
    return len(rows)


def load_instructors(conn, json_path: Path, dry_run: bool = False) -> int:
    if not json_path.exists():
        log.warning("Instructors JSON not found: %s", json_path)
        return 0

    instructors: list[dict] = json.loads(json_path.read_text(encoding="utf-8"))
    if dry_run:
        log.info("[DRY-RUN] Would upsert %d instructors", len(instructors))
        return len(instructors)

    sql = """
        INSERT INTO instructors
          (source, source_id, name, city, certifications, studio_name,
           website, instagram, specialties, crawled_at)
        VALUES
          (%(source)s, %(source_id)s, %(name)s, %(city)s, %(certifications)s,
           %(studio_name)s, %(website)s, %(instagram)s, %(specialties)s,
           %(crawled_at)s)
        ON CONFLICT (source, source_id) DO UPDATE SET
          name           = EXCLUDED.name,
          city           = EXCLUDED.city,
          certifications = EXCLUDED.certifications,
          studio_name    = EXCLUDED.studio_name,
          website        = EXCLUDED.website,
          instagram      = EXCLUDED.instagram,
          specialties    = EXCLUDED.specialties,
          crawled_at     = EXCLUDED.crawled_at
    """

    rows = []
    for i in instructors:
        certs = i.get("certifications") or []
        if isinstance(certs, str):
            certs = [c.strip() for c in certs.split(",") if c.strip()]

        specs = i.get("specialties") or []
        if isinstance(specs, str):
            specs = [s.strip() for s in specs.split(",") if s.strip()]

        rows.append({
            "source":         i.get("source", "unknown"),
            "source_id":      i.get("source_id") or None,
            "name":           i.get("name", ""),
            "city":           i.get("city") or None,
            "certifications": certs or None,
            "studio_name":    i.get("studio_name") or i.get("studio") or None,
            "website":        i.get("website") or None,
            "instagram":      i.get("instagram") or None,
            "specialties":    specs or None,
            "crawled_at":     i.get("crawled_at") or datetime.now(timezone.utc).isoformat(),
        })

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
    conn.commit()
    log.info("Upserted %d instructors", len(rows))
    return len(rows)


def load_associations(conn, json_path: Path, dry_run: bool = False) -> int:
    if not json_path.exists():
        log.warning("Associations JSON not found: %s", json_path)
        return 0

    associations: list[dict] = json.loads(json_path.read_text(encoding="utf-8"))
    if dry_run:
        log.info("[DRY-RUN] Would upsert %d associations", len(associations))
        return len(associations)

    sql = """
        INSERT INTO associations
          (source, source_id, name, name_en, org_type, website,
           registration_id, member_count, cert_levels, crawled_at)
        VALUES
          (%(source)s, %(source_id)s, %(name)s, %(name_en)s, %(org_type)s,
           %(website)s, %(registration_id)s, %(member_count)s,
           %(cert_levels)s, %(crawled_at)s)
        ON CONFLICT (source, source_id) DO UPDATE SET
          name            = EXCLUDED.name,
          name_en         = EXCLUDED.name_en,
          org_type        = EXCLUDED.org_type,
          website         = EXCLUDED.website,
          registration_id = EXCLUDED.registration_id,
          member_count    = EXCLUDED.member_count,
          cert_levels     = EXCLUDED.cert_levels,
          crawled_at      = EXCLUDED.crawled_at
    """

    rows = []
    for a in associations:
        levels = a.get("cert_levels") or []
        if isinstance(levels, str):
            levels = [l.strip() for l in levels.split(",") if l.strip()]

        try:
            member_count = int(a.get("member_count") or 0) or None
        except (ValueError, TypeError):
            member_count = None

        rows.append({
            "source":          a.get("source", "static"),
            "source_id":       a.get("source_id") or a.get("name", ""),
            "name":            a.get("name", ""),
            "name_en":         a.get("name_en") or None,
            "org_type":        a.get("org_type") or None,
            "website":         a.get("website") or None,
            "registration_id": a.get("registration_id") or None,
            "member_count":    member_count,
            "cert_levels":     levels or None,
            "crawled_at":      a.get("crawled_at") or datetime.now(timezone.utc).isoformat(),
        })

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
    conn.commit()
    log.info("Upserted %d associations", len(rows))
    return len(rows)



def load_classes(conn, json_path: Path, dry_run: bool = False) -> int:
    if not json_path.exists():
        log.warning("Classes JSON not found: %s", json_path)
        return 0

    classes: list[dict] = json.loads(json_path.read_text(encoding="utf-8"))
    if dry_run:
        log.info("[DRY-RUN] Would upsert %d classes", len(classes))
        return len(classes)

    # Resolve instructor DB ids by instagram_handle or instructor_id
    handle_to_db: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, instagram FROM instructors WHERE instagram IS NOT NULL")
        for db_id, handle in cur.fetchall():
            handle_to_db[handle.lstrip("@")] = db_id

    sql = """
        INSERT INTO classes
          (instructor_id, title, style, price, schedule,
           contraindications, description, scraped_at)
        VALUES
          (%(instructor_id)s, %(title)s, %(style)s, %(price)s,
           %(schedule)s, %(contraindications)s, %(description)s,
           %(scraped_at)s)
        ON CONFLICT DO NOTHING
    """

    rows = []
    for c in classes:
        handle = (c.get("instructor_handle") or "").lstrip("@")
        inst_db_id = handle_to_db.get(handle)  # None if instructor not yet in DB

        style = c.get("style") or (
            c.get("styles", [None])[0] if c.get("styles") else None
        )
        price = c.get("price_krw")
        if price is not None:
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None

        schedule = c.get("schedule")
        if not isinstance(schedule, str):
            schedule = json.dumps(schedule, ensure_ascii=False) if schedule else None

        contraindications = c.get("contraindications") or []
        if isinstance(contraindications, str):
            contraindications = [k.strip() for k in contraindications.split(",") if k.strip()]

        rows.append({
            "instructor_id":    inst_db_id,
            "title":            c.get("title") or (f"{handle} 요가 클래스"),
            "style":            style,
            "price":            price,
            "schedule":         schedule,
            "contraindications": contraindications or None,
            "description":      c.get("caption_snippet") or None,
            "scraped_at":       c.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        })

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
    conn.commit()
    log.info("Upserted %d classes", len(rows))
    return len(rows)


# ── S3 download ───────────────────────────────────────────────────────────────

def download_from_s3(date_str: str, local_root: Path) -> None:
    s3_prefix = f"s3://{S3_BUCKET}/{date_str}/"
    log.info("Downloading from %s → %s", s3_prefix, local_root)
    result = subprocess.run(
        ["aws", "s3", "sync", s3_prefix, str(local_root),
         "--region", "ap-northeast-2"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log.error("S3 download failed: %s", result.stderr)
        sys.exit(1)
    log.info("Download complete")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load crawled JSON data into PostgreSQL")
    p.add_argument(
        "--tables", nargs="+",
        choices=["studios", "instructors", "associations", "classes"],
        default=["studios", "instructors", "associations", "classes"],
    )
    p.add_argument(
        "--data-dir", type=Path, default=DATA_ROOT,
        help="Root data directory (default: ./data)",
    )
    p.add_argument(
        "--s3-date", metavar="YYYY-MM-DD",
        help="Download from S3 snapshot before loading",
    )
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.s3_date:
        download_from_s3(args.s3_date, args.data_dir)

    if args.dry_run:
        conn = None
        log.info("DRY-RUN mode — no DB writes")
    else:
        try:
            conn = get_conn()
            log.info("Connected to PostgreSQL")
        except Exception as exc:
            log.error("Cannot connect to DB: %s", exc)
            log.error("Set DATABASE_URL or PGHOST/PGUSER/PGPASSWORD/PGDATABASE env vars")
            sys.exit(1)

    totals: dict[str, int] = {}

    if "studios" in args.tables:
        path = args.data_dir / "studios" / "studios_raw.json"
        totals["studios"] = load_studios(conn, path, args.dry_run)

    if "instructors" in args.tables:
        path = args.data_dir / "instructors" / "instructors_raw.json"
        totals["instructors"] = load_instructors(conn, path, args.dry_run)

    if "associations" in args.tables:
        path = args.data_dir / "associations" / "associations_raw.json"
        totals["associations"] = load_associations(conn, path, args.dry_run)

    if "classes" in args.tables:
        path = args.data_dir / "classes" / "classes_raw.json"
        totals["classes"] = load_classes(conn, path, args.dry_run)

    if conn:
        # Print summary
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM data_summary ORDER BY \"table\"")
            rows = cur.fetchall()
            log.info("── DB Summary ──────────────────")
            for table, count in rows:
                log.info("  %-15s %d rows", table, count)
        conn.close()

    log.info("Load complete: %s", totals)


if __name__ == "__main__":
    main()
