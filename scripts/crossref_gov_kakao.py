#!/usr/bin/env python3
"""
Cross-reference gov_sangga records vs Kakao/Naver records in the studios table.

Matching strategy (either condition = match):
  1. Name proximity: trigram similarity ≥ 0.4 (pg_trgm) + same 시군구
  2. Coordinate proximity: ST_DWithin(a.location, b.location, 50)  [50 m]

Outputs:
  - Summary to stdout
  - data/crossref_report_YYYYMMDD.csv  (all gov records + match info)

Run (on crawler or locally if DB reachable):
    python scripts/crossref_gov_kakao.py
    python scripts/crossref_gov_kakao.py --out-dir /tmp
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data"

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl",
)


def get_connection():
    return psycopg2.connect(DB_URL)


def enable_trgm(cur):
    """Enable pg_trgm extension (needed for similarity())."""
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    except Exception:
        pass  # may lack superuser; distance fallback still works


def run_crossref(conn) -> dict:
    """
    Returns stats dict with keys:
      total_gov, total_kakao_naver,
      gov_matched_by_name, gov_matched_by_coord, gov_matched_any,
      gov_unmatched (new records not in kakao/naver at all)
    Also returns rows list for CSV export.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    enable_trgm(cur)
    conn.commit()

    # Count totals
    cur.execute("SELECT COUNT(*) AS n FROM studios WHERE source = 'gov_sangga'")
    total_gov = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM studios WHERE source IN ('kakao', 'naver')")
    total_kn = cur.fetchone()["n"]

    log.info("Total gov_sangga: %d | Total kakao/naver: %d", total_gov, total_kn)

    # ------------------------------------------------------------------
    # Match by coordinate proximity (50 m)
    # ------------------------------------------------------------------
    log.info("Running coordinate proximity match (50 m)...")
    cur.execute("""
        SELECT
            g.id           AS gov_id,
            g.name         AS gov_name,
            g.road_address AS gov_address,
            g.category     AS gov_category,
            kn.id          AS kn_id,
            kn.name        AS kn_name,
            kn.road_address AS kn_address,
            kn.source      AS kn_source,
            ROUND(ST_Distance(
                g.location::geography,
                kn.location::geography
            )::numeric, 1) AS dist_m
        FROM studios g
        CROSS JOIN LATERAL (
            SELECT *
            FROM studios kn
            WHERE kn.source IN ('kakao', 'naver')
              AND g.location IS NOT NULL
              AND kn.location IS NOT NULL
              AND ST_DWithin(
                    g.location::geography,
                    kn.location::geography,
                    50
                )
            ORDER BY ST_Distance(
                g.location::geography,
                kn.location::geography
            )
            LIMIT 1
        ) kn
        WHERE g.source = 'gov_sangga'
    """)
    coord_matches = {row["gov_id"]: dict(row) for row in cur.fetchall()}
    log.info("  → %d coord matches", len(coord_matches))

    # ------------------------------------------------------------------
    # Match by name + 시군구 (trigram similarity ≥ 0.4)
    # Falls back to ILIKE if pg_trgm not available
    # ------------------------------------------------------------------
    log.info("Running name similarity match (trgm ≥ 0.4, same 시군구)...")
    try:
        cur.execute("""
            SELECT
                g.id           AS gov_id,
                g.name         AS gov_name,
                g.road_address AS gov_address,
                g.category     AS gov_category,
                kn.id          AS kn_id,
                kn.name        AS kn_name,
                kn.road_address AS kn_address,
                kn.source      AS kn_source,
                ROUND(similarity(g.name, kn.name)::numeric, 3) AS name_sim
            FROM studios g
            JOIN studios kn
              ON kn.source IN ('kakao', 'naver')
             AND similarity(g.name, kn.name) >= 0.4
             AND (g.address IS NOT NULL AND kn.address IS NOT NULL)
             AND (
                   split_part(g.address, ' ', 2) = split_part(kn.address, ' ', 2)
                   OR split_part(g.road_address, ' ', 2) = split_part(kn.road_address, ' ', 2)
                 )
            WHERE g.source = 'gov_sangga'
        """)
        name_matches = {row["gov_id"]: dict(row) for row in cur.fetchall()}
    except psycopg2.Error as e:
        log.warning("pg_trgm not available, falling back to ILIKE: %s", e)
        conn.rollback()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                g.id           AS gov_id,
                g.name         AS gov_name,
                g.road_address AS gov_address,
                g.category     AS gov_category,
                kn.id          AS kn_id,
                kn.name        AS kn_name,
                kn.road_address AS kn_address,
                kn.source      AS kn_source,
                NULL::float    AS name_sim
            FROM studios g
            JOIN studios kn
              ON kn.source IN ('kakao', 'naver')
             AND (
                   lower(kn.name) LIKE '%' || lower(g.name) || '%'
                   OR lower(g.name) LIKE '%' || lower(kn.name) || '%'
                 )
             AND split_part(g.address, ' ', 2) = split_part(kn.address, ' ', 2)
            WHERE g.source = 'gov_sangga'
        """)
        name_matches = {row["gov_id"]: dict(row) for row in cur.fetchall()}

    log.info("  → %d name matches", len(name_matches))

    # Combined
    all_matched_gov_ids = set(coord_matches) | set(name_matches)
    log.info("  → %d gov records matched by either method", len(all_matched_gov_ids))

    # ------------------------------------------------------------------
    # Build full report rows
    # ------------------------------------------------------------------
    log.info("Fetching all gov records for report...")
    cur.execute("""
        SELECT id, source_id, name, category, road_address, address,
               lng, lat
        FROM studios
        WHERE source = 'gov_sangga'
        ORDER BY id
    """)
    gov_rows = cur.fetchall()

    report_rows = []
    for row in gov_rows:
        gid = row["id"]
        matched_coord = coord_matches.get(gid)
        matched_name = name_matches.get(gid)
        is_matched = gid in all_matched_gov_ids

        report_rows.append({
            "gov_id":        gid,
            "gov_source_id": row["source_id"],
            "gov_name":      row["name"],
            "gov_category":  row["category"],
            "gov_address":   row["road_address"] or row["address"],
            "gov_lat":       row["lat"],
            "gov_lng":       row["lng"],
            "matched":       "Y" if is_matched else "N",
            "match_method":  ("coord+name" if (matched_coord and matched_name)
                              else "coord" if matched_coord
                              else "name" if matched_name
                              else ""),
            "kn_id":         (matched_coord or matched_name or {}).get("kn_id", ""),
            "kn_name":       (matched_coord or matched_name or {}).get("kn_name", ""),
            "kn_source":     (matched_coord or matched_name or {}).get("kn_source", ""),
            "kn_address":    (matched_coord or matched_name or {}).get("kn_address", ""),
            "dist_m":        matched_coord.get("dist_m", "") if matched_coord else "",
            "name_sim":      matched_name.get("name_sim", "") if matched_name else "",
        })

    stats = {
        "total_gov": total_gov,
        "total_kakao_naver": total_kn,
        "gov_matched_by_coord": len(coord_matches),
        "gov_matched_by_name": len(name_matches),
        "gov_matched_any": len(all_matched_gov_ids),
        "gov_unmatched": total_gov - len(all_matched_gov_ids),
        "match_rate_pct": round(len(all_matched_gov_ids) / total_gov * 100, 1),
    }
    cur.close()
    return stats, report_rows


def save_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        log.warning("No rows to save.")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("Saved %d rows → %s", len(rows), path)


def print_summary(stats: dict) -> None:
    print("\n" + "=" * 60)
    print("CROSS-REFERENCE REPORT: gov_sangga vs kakao/naver")
    print("=" * 60)
    print(f"  Gov records:           {stats['total_gov']:>8,}")
    print(f"  Kakao/Naver records:   {stats['total_kakao_naver']:>8,}")
    print(f"  Matched by coord:      {stats['gov_matched_by_coord']:>8,}")
    print(f"  Matched by name:       {stats['gov_matched_by_name']:>8,}")
    print(f"  Matched (either):      {stats['gov_matched_any']:>8,}  ({stats['match_rate_pct']}%)")
    print(f"  NEW (unmatched):       {stats['gov_unmatched']:>8,}  ({100 - stats['match_rate_pct']}%)")
    print("=" * 60)
    new_pct = 100 - stats["match_rate_pct"]
    print(f"\n  → {stats['gov_unmatched']:,} gov records are NOT in kakao/naver")
    print(f"     ({new_pct}% of gov data = newly discovered studios)\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Cross-reference gov_sangga vs kakao/naver studios")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR,
                   help="Directory to write crossref_report_YYYYMMDD.csv (default: data/)")
    p.add_argument("--no-csv", action="store_true",
                   help="Print summary only, do not write CSV")
    args = p.parse_args()

    conn = get_connection()
    try:
        stats, rows = run_crossref(conn)
        conn.commit()
    finally:
        conn.close()

    print_summary(stats)

    if not args.no_csv:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        ts = date.today().strftime("%Y%m%d")
        out_path = args.out_dir / f"crossref_report_{ts}.csv"
        save_csv(rows, out_path)


if __name__ == "__main__":
    main()
