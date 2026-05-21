#!/usr/bin/env python3
"""
load_gov_sangga.py — Convert 소상공인 CSV rows to studio records and upsert to DB.

Used by scrape_gov_sangga.py --load-db via dynamic import.
Can also be called directly:
    python scripts/load_gov_sangga.py data/studios/gov_sangga_*.csv
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl",
)

UPSERT_SQL = """
    INSERT INTO studios
      (source, source_id, name, category, phone, address, road_address,
       lng, lat, place_url, crawled_at, facility_props)
    VALUES
      (%(source)s, %(source_id)s, %(name)s, %(category)s, %(phone)s,
       %(address)s, %(road_address)s, %(lng)s, %(lat)s, %(place_url)s,
       %(crawled_at)s, %(facility_props)s)
    ON CONFLICT (source, source_id) DO UPDATE SET
      name           = EXCLUDED.name,
      category       = EXCLUDED.category,
      address        = EXCLUDED.address,
      road_address   = EXCLUDED.road_address,
      lng            = EXCLUDED.lng,
      lat            = EXCLUDED.lat,
      crawled_at     = EXCLUDED.crawled_at,
      facility_props = EXCLUDED.facility_props
"""


def row_to_record(row: dict) -> dict | None:
    """Convert one 소상공인 CSV row to a studios-table record dict.

    Returns None for rows missing a business name.
    """
    biz_id = row.get("상가업소번호", "").strip()
    name = row.get("상호명", "").strip()
    branch = row.get("지점명", "").strip()
    if not name:
        return None

    full_name = f"{name} {branch}".strip() if branch else name

    try:
        lng = float(row.get("경도") or 0) or None
        lat = float(row.get("위도") or 0) or None
    except (ValueError, TypeError):
        lng = lat = None

    sido = row.get("시도명", "").strip()
    sigungu = row.get("시군구명", "").strip()
    adong = row.get("행정동명", "").strip()
    address = " ".join(p for p in [sido, sigungu, adong] if p) or None
    road_address = row.get("도로명주소", "").strip() or None

    facility_props = {
        "gov_업종코드":   row.get("상권업종소분류코드", "").strip(),
        "gov_표준산업코드": row.get("표준산업분류코드", "").strip(),
        "gov_표준산업명":  row.get("표준산업분류명", "").strip(),
        "gov_시도":      sido,
        "gov_시군구":    sigungu,
        "gov_행정동":    adong,
        "gov_법정동":    row.get("법정동명", "").strip(),
        "gov_우편번호":   row.get("신우편번호", "").strip(),
    }

    return {
        "source":        "gov_sangga",
        "source_id":     biz_id or None,
        "name":          full_name,
        "category":      row.get("상권업종소분류명", "").strip() or None,
        "phone":         None,
        "address":       address,
        "road_address":  road_address,
        "lng":           lng,
        "lat":           lat,
        "place_url":     None,
        "crawled_at":    datetime.now(timezone.utc).isoformat(),
        "facility_props": facility_props,
    }


def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def upsert_to_db(records: list[dict]) -> None:
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'studios_source_source_id_key'
            ) THEN
                ALTER TABLE studios ADD CONSTRAINT studios_source_source_id_key
                    UNIQUE (source, source_id);
            END IF;
        END $$;
    """)

    serialised = [
        {**r, "facility_props": json.dumps(r["facility_props"], ensure_ascii=False)}
        for r in records
    ]
    psycopg2.extras.execute_batch(cur, UPSERT_SQL, serialised, page_size=500)
    conn.commit()
    cur.close()
    conn.close()
    log.info("Upserted %d records into studios table.", len(records))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print("Usage: python load_gov_sangga.py data/studios/gov_sangga_*.csv", file=sys.stderr)
        sys.exit(1)

    all_rows: list[dict] = []
    for path in paths:
        rows = load_csv(path)
        log.info("Loaded %d rows from %s", len(rows), path)
        all_rows.extend(rows)

    records = [r for row in all_rows if (r := row_to_record(row))]
    log.info("Converted %d/%d rows to studio records", len(records), len(all_rows))
    upsert_to_db(records)


if __name__ == "__main__":
    main()
