#!/usr/bin/env python3
"""
studios_dedup.py — clean and deduplicate the yogacrawl studios table.

Strategy
--------
A studio row has *zero weight* (contributes nothing new) when:
  1. It is an exact duplicate within its source (same name + same road_address).
     e.g. gov_sangga re-inserts the same shop under multiple category codes.
  2. It is a cross-source duplicate where a richer record (kakao/naver) already
     covers the same business.  gov_sangga only carries address + coords; kakao
     adds phone numbers and naver adds websites/instagram.  When both exist, the
     gov_sangga copy adds nothing.

Weight score per row (0–7):
  +1 phone            +1 website      +1 instagram
  +1 road_address     +1 lat          +1 opening_hours
  +1 review_count > 0

Usage
-----
  python scripts/studios_dedup.py [--dry-run] [--db-url URL]

  --dry-run   Print counts only; make no changes.
  --db-url    Override DB URL (default: env CRAWL_DB_URL or localhost).
"""
from __future__ import annotations

import argparse
import logging
import os
from textwrap import dedent

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def connect(db_url: str):
    return psycopg2.connect(db_url)


def count(cur, sql: str, params=None) -> int:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def run(db_url: str, dry_run: bool) -> None:
    conn = connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()

    mode = "[DRY RUN]" if dry_run else "[LIVE]"
    log.info(f"{mode} Connected — auditing studios table…")

    # ── Baseline ──────────────────────────────────────────────────────────────
    total_before = count(cur, "SELECT count(*) FROM studios")
    log.info(f"Total rows before:  {total_before:>8,}")
    for src in ("gov_sangga", "kakao", "naver"):
        n = count(cur, "SELECT count(*) FROM studios WHERE source=%s", (src,))
        log.info(f"  {src:14s}  {n:>8,}")

    # ── Step 1: within-source exact duplicates (same name + road_address) ────
    #   For each (source, name, road_address) group keep the row whose category
    #   is most yoga-relevant (요가 > 필라테스 > other), breaking ties by lowest id.
    dup_within_sql = dedent("""\
        SELECT count(*) FROM studios
        WHERE id NOT IN (
            SELECT DISTINCT ON (source, name, COALESCE(road_address, address))
                   id
            FROM   studios
            ORDER  BY source, name, COALESCE(road_address, address),
                      CASE
                        WHEN category ILIKE '%요가%'    THEN 0
                        WHEN category ILIKE '%필라테스%' THEN 1
                        ELSE 2
                      END,
                      id
        )
    """)
    dupes_within = count(cur, dup_within_sql)
    log.info(f"Within-source duplicates (same name+address): {dupes_within:,}")

    # ── Step 2: cross-source duplicates ──────────────────────────────────────
    #   gov_sangga rows whose name exactly matches a kakao or naver row.
    #   The richer kakao/naver record is preferred; gov_sangga copy adds nothing.
    dup_cross_sql = dedent("""\
        SELECT count(*) FROM studios g
        WHERE  g.source = 'gov_sangga'
          AND  EXISTS (
              SELECT 1 FROM studios k
              WHERE  k.source IN ('kakao','naver')
                AND  k.name = g.name
          )
    """)
    dupes_cross = count(cur, dup_cross_sql)
    log.info(f"Cross-source duplicates (gov covered by kakao/naver): {dupes_cross:,}")

    # ── Step 3: entries with no geolocation (can't be used in matching at all) ─
    no_coords_sql = "SELECT count(*) FROM studios WHERE lat IS NULL AND lng IS NULL"
    no_coords = count(cur, no_coords_sql)
    log.info(f"Entries with no coordinates (unusable for matching): {no_coords:,}")

    total_to_remove = dupes_within + dupes_cross + no_coords
    log.info(f"Total removals planned: {total_to_remove:,}  →  ~{total_before - total_to_remove:,} rows remaining")

    if dry_run:
        log.info("Dry-run mode — no changes made. Re-run without --dry-run to apply.")
        conn.rollback()
        return

    log.info("Applying removals…")

    # Step 1 — within-source dedup
    cur.execute(dedent("""\
        DELETE FROM studios
        WHERE id NOT IN (
            SELECT DISTINCT ON (source, name, COALESCE(road_address, address))
                   id
            FROM   studios
            ORDER  BY source, name, COALESCE(road_address, address),
                      CASE
                        WHEN category ILIKE '%요가%'    THEN 0
                        WHEN category ILIKE '%필라테스%' THEN 1
                        ELSE 2
                      END,
                      id
        )
    """))
    step1_removed = cur.rowcount
    log.info(f"  Step 1 (within-source dupes removed):   {step1_removed:>6,}")

    # Step 2 — cross-source dedup: remove gov_sangga where kakao/naver exists
    cur.execute(dedent("""\
        DELETE FROM studios g
        USING  studios k
        WHERE  g.source  = 'gov_sangga'
          AND  k.source IN ('kakao','naver')
          AND  k.name    = g.name
    """))
    step2_removed = cur.rowcount
    log.info(f"  Step 2 (cross-source gov_sangga dupes):  {step2_removed:>6,}")

    # Step 3 — no coordinates
    cur.execute("DELETE FROM studios WHERE lat IS NULL AND lng IS NULL")
    step3_removed = cur.rowcount
    log.info(f"  Step 3 (no-coordinates removed):         {step3_removed:>6,}")

    conn.commit()

    # ── Final counts ──────────────────────────────────────────────────────────
    total_after = count(cur, "SELECT count(*) FROM studios")
    log.info(f"Total rows after:   {total_after:>8,}  (removed {total_before - total_after:,})")
    log.info("Breakdown by source:")
    cur.execute("SELECT source, count(*) FROM studios GROUP BY source ORDER BY count DESC")
    for src, n in cur.fetchall():
        log.info(f"  {src:14s}  {n:>8,}")

    # Quality score summary
    log.info("Quality score breakdown (fields populated per row):")
    cur.execute(dedent("""\
        SELECT source,
               round(avg(
                 (phone IS NOT NULL)::int +
                 (road_address IS NOT NULL)::int +
                 (lat IS NOT NULL)::int +
                 (website IS NOT NULL)::int +
                 (instagram IS NOT NULL)::int +
                 (opening_hours IS NOT NULL)::int +
                 (review_count IS NOT NULL AND review_count > 0)::int
               ), 2) AS avg_score,
               min(
                 (phone IS NOT NULL)::int +
                 (road_address IS NOT NULL)::int +
                 (lat IS NOT NULL)::int +
                 (website IS NOT NULL)::int +
                 (instagram IS NOT NULL)::int +
                 (opening_hours IS NOT NULL)::int +
                 (review_count IS NOT NULL AND review_count > 0)::int
               ) AS min_score,
               max(
                 (phone IS NOT NULL)::int +
                 (road_address IS NOT NULL)::int +
                 (lat IS NOT NULL)::int +
                 (website IS NOT NULL)::int +
                 (instagram IS NOT NULL)::int +
                 (opening_hours IS NOT NULL)::int +
                 (review_count IS NOT NULL AND review_count > 0)::int
               ) AS max_score
        FROM studios GROUP BY source ORDER BY avg_score DESC
    """))
    for row in cur.fetchall():
        log.info(f"  {row[0]:14s}  avg={row[1]}  min={row[2]}  max={row[3]}")

    cur.close()
    conn.close()
    log.info("Done.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report only, make no changes")
    ap.add_argument("--db-url", default=os.getenv(
        "CRAWL_DB_URL",
        "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl"
    ))
    args = ap.parse_args()
    run(args.db_url, args.dry_run)


if __name__ == "__main__":
    main()
