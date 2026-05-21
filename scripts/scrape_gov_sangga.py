#!/usr/bin/env python3
"""
Scrape 소상공인마당 상가(상권)정보 API for live yoga/gym data.

Replaces manual ZIP download from data.go.kr with a REST API call.
Requires a free API key from https://www.data.go.kr/data/15012005/openapi.do

Usage:
    export SANGGA_API_KEY="your_decoded_api_key_here"
    python scripts/scrape_gov_sangga.py                   # fetch all target categories
    python scripts/scrape_gov_sangga.py --category P10603  # yoga only
    python scripts/scrape_gov_sangga.py --sido 서울특별시    # one province
    python scripts/scrape_gov_sangga.py --load-db          # fetch + upsert to DB

API docs: https://www.data.go.kr/data/15012005/openapi.do
Base URL: https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInUpjong
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "studios"

# Get API key from environment — register free at https://www.data.go.kr
API_KEY = os.environ.get("SANGGA_API_KEY", "")

# 소상공인 상가정보 API endpoint (storeListInUpjong = search by 업종코드)
BASE_URL = "https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInUpjong"

# Target subcategory codes (상권업종소분류코드)
TARGET_CODES: dict[str, str] = {
    "P10603": "요가/필라테스 학원",
    "R10307": "헬스장",
    "P10605": "레크리에이션 교육기관",
    "P10613": "기타 예술/스포츠 교육기관",
    "R10306": "종합 스포츠시설",
    "R10314": "기타 스포츠시설 운영업",
}

# All Korean provinces (시도명) — used to paginate region by region
ALL_SIDO = [
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원특별자치도",
    "충청북도", "충청남도", "전라북도", "전라남도", "경상북도",
    "경상남도", "제주특별자치도",
]

PAGE_SIZE = 1000  # max allowed by API


def fetch_page(
    upjong_cd: str,
    sido: str,
    page_no: int,
    delay: float = 0.3,
) -> dict:
    """Fetch one page from the API. Returns parsed JSON response."""
    if not API_KEY:
        raise RuntimeError(
            "SANGGA_API_KEY env var not set.\n"
            "Register free at https://www.data.go.kr/data/15012005/openapi.do\n"
            "then: export SANGGA_API_KEY='your_decoded_key'"
        )

    params = {
        "serviceKey": API_KEY,
        "pageNo": page_no,
        "numOfRows": PAGE_SIZE,
        "divId": "U",           # U = 업종코드 기준
        "key": upjong_cd,
        "indsLclsCd": "",
        "indsMclsCd": "",
        "indsSclsCd": upjong_cd,
        "type": "json",
    }
    # Filter by sido if provided
    if sido:
        params["signguCd"] = ""   # will use sido name filter below
        params["dongCd"] = ""

    resp = httpx.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    time.sleep(delay)
    return resp.json()


def fetch_all(
    codes: dict[str, str] | None = None,
    sido_list: list[str] | None = None,
    delay: float = 0.3,
) -> list[dict]:
    """Fetch all records for given codes, optionally filtered by sido."""
    codes = codes or TARGET_CODES
    results: list[dict] = []

    for code, label in codes.items():
        log.info("Fetching %s (%s)...", label, code)
        page = 1
        while True:
            data = fetch_page(code, "", page, delay=delay)
            body = data.get("body", {})
            items = body.get("items", [])
            if not items:
                break
            # Filter by sido client-side if requested
            if sido_list:
                items = [i for i in items if i.get("ctprvnCd", "") in sido_list
                         or i.get("ctprvnNm", "") in sido_list]
            results.extend(items)
            total = int(body.get("totalCount", 0))
            log.info("  page %d: +%d items (total so far: %d / %d)",
                     page, len(items), len(results), total)
            if page * PAGE_SIZE >= total:
                break
            page += 1

    return results


def api_item_to_csv_row(item: dict) -> dict:
    """Map API response item to the same column format as the ZIP CSVs."""
    return {
        "상가업소번호":         item.get("bizesId", ""),
        "상호명":              item.get("bizesNm", ""),
        "지점명":              item.get("brchNm", ""),
        "상권업종소분류코드":    item.get("indsSclsCd", ""),
        "상권업종소분류명":      item.get("indsSclsNm", ""),
        "표준산업분류코드":      item.get("ksicCd", ""),
        "표준산업분류명":       item.get("ksicNm", ""),
        "시도명":              item.get("ctprvnNm", ""),
        "시군구명":            item.get("signguNm", ""),
        "행정동명":            item.get("adongNm", ""),
        "법정동명":            item.get("lnmDongNm", ""),
        "도로명주소":           item.get("rdnwhlAddr", ""),
        "신우편번호":           item.get("newZipCd", ""),
        "경도":               item.get("lon", ""),
        "위도":               item.get("lat", ""),
    }


def sangga_to_studio(row: dict) -> dict | None:
    """Convert a 소상공인 CSV row to the studios_raw.json schema.

    Returns None for rows with no business name.
    """
    name = row.get("상호명", "").strip()
    branch = row.get("지점명", "").strip()
    if not name:
        return None

    full_name = f"{name} {branch}".strip() if branch else name

    try:
        x = str(float(row.get("경도") or 0)) if row.get("경도") else ""
        y = str(float(row.get("위도") or 0)) if row.get("위도") else ""
        # Treat 0.0 coords as missing
        if x == "0.0":
            x = ""
        if y == "0.0":
            y = ""
    except (ValueError, TypeError):
        x = y = ""

    sido = row.get("시도명", "").strip()
    sigungu = row.get("시군구명", "").strip()
    adong = row.get("행정동명", "").strip()
    address = " ".join(p for p in [sido, sigungu, adong] if p) or None
    road_address = row.get("도로명주소", "").strip() or None

    return {
        "source":       "gov_sangga",
        "source_id":    row.get("상가업소번호", "").strip() or None,
        "name":         full_name,
        "category":     row.get("상권업종소분류명", "").strip() or None,
        "phone":        "",
        "address":      address or road_address or "",
        "road_address": road_address or "",
        "x":            x,
        "y":            y,
        "place_url":    "",
        "crawled_at":   datetime.now(timezone.utc).isoformat(),
        # Gov-specific metadata preserved for enrichment and provenance
        "gov_업종코드":  row.get("상권업종소분류코드", "").strip(),
        "gov_시군구":   sigungu,
        "gov_우편번호":  row.get("신우편번호", "").strip(),
    }


def merge_into_studios_json(rows: list[dict], studios_json: Path) -> int:
    """Convert gov rows to studio schema and merge with studios_raw.json.

    Returns the number of studios after deduplication.
    """
    # Import dedup logic from the studios scraper
    import importlib.util
    scraper_path = Path(__file__).parent / "scrape_studios.py"
    spec = importlib.util.spec_from_file_location("scrape_studios", scraper_path)
    scraper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scraper)

    new_studios = [s for row in rows if (s := sangga_to_studio(row))]
    log.info("Converted %d/%d gov rows to studio records", len(new_studios), len(rows))

    existing: list[dict] = []
    if studios_json.exists():
        try:
            existing = json.loads(studios_json.read_text(encoding="utf-8"))
            log.info("Loaded %d existing studios from %s", len(existing), studios_json)
        except Exception as exc:
            log.warning("Could not read existing JSON (%s) — starting fresh", exc)

    combined = existing + new_studios
    deduped = scraper.deduplicate(combined)
    log.info("After dedup: %d studios (%d new from gov)", len(deduped), len(new_studios))

    studios_json.write_text(
        json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Updated %s", studios_json)
    return len(deduped)


def save_csv(rows: list[dict], path: Path) -> None:
    COLS = [
        "상가업소번호", "상호명", "지점명",
        "상권업종소분류코드", "상권업종소분류명",
        "표준산업분류코드", "표준산업분류명",
        "시도명", "시군구명", "행정동명", "법정동명",
        "도로명주소", "신우편번호", "경도", "위도",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    log.info("Saved %d rows → %s", len(rows), path)


def load_to_db(rows: list[dict]) -> None:
    """Upsert rows into studios table via load_gov_sangga.py logic."""
    # Re-use the loader module
    loader_path = Path(__file__).parent / "load_gov_sangga.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("load_gov_sangga", loader_path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)

    # Convert to CSV rows format expected by loader.row_to_record
    records = []
    for row in rows:
        rec = loader.row_to_record(row)
        if rec:
            records.append(rec)

    import json as _json
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(loader.DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    # Ensure unique constraint
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

    for rec in records:
        rec["facility_props"] = _json.dumps(rec["facility_props"], ensure_ascii=False)

    psycopg2.extras.execute_batch(cur, loader.UPSERT_SQL, records, page_size=500)
    conn.commit()
    cur.close()
    conn.close()
    log.info("Upserted %d records into studios table.", len(records))


def main() -> None:
    p = argparse.ArgumentParser(description="Live scrape 소상공인 상가정보 API")
    p.add_argument("--category", choices=list(TARGET_CODES), default=None,
                   help="Fetch single category code only")
    p.add_argument("--sido", default=None,
                   help="Filter by province, e.g. '서울특별시' (comma-separated for multiple)")
    p.add_argument("--delay", type=float, default=0.3,
                   help="Seconds between API requests (default 0.3)")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR,
                   help="Directory to save output CSV")
    p.add_argument("--load-db", action="store_true",
                   help="Upsert results into PostgreSQL after fetching")
    p.add_argument("--merge-json", action="store_true",
                   help="Merge results into studios_raw.json (runs enrichment pipeline next)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print row counts only, do not save or insert")
    args = p.parse_args()

    codes = {args.category: TARGET_CODES[args.category]} if args.category else TARGET_CODES
    sido_filter = [s.strip() for s in args.sido.split(",")] if args.sido else None

    items = fetch_all(codes=codes, sido_list=sido_filter, delay=args.delay)
    rows = [api_item_to_csv_row(i) for i in items]

    from collections import Counter
    log.info("=== Results by category ===")
    for cat, cnt in Counter(r["상권업종소분류명"] for r in rows).most_common():
        log.info("  %s: %d", cat, cnt)

    if args.dry_run:
        log.info("DRY RUN — %d total records (not saved)", len(rows))
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    save_csv(rows, args.out_dir / f"gov_sangga_{ts}.csv")

    if args.merge_json:
        studios_json = args.out_dir / "studios_raw.json"
        merge_into_studios_json(rows, studios_json)

    if args.load_db:
        load_to_db(rows)


if __name__ == "__main__":
    main()
