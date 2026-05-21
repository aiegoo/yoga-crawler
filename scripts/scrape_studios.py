#!/usr/bin/env python3
"""
Yoga Studio Scraper — Kakao Local API + Naver Search API
=========================================================

Sources
-------
1. Kakao Local API  /v2/local/search/keyword.json   (up to 45 pages × 15 = 675/keyword)
2. Naver Search API /v1/search/local.json            (up to 100 items/keyword, 1000 start)

Output
------
  data/studios/studios_raw.json      — deduplicated, merged results
  data/studios/studios_seed.sql      — INSERT statements for PostgreSQL

Usage
-----
  # Seoul only, default keywords
  python scripts/scrape_studios.py --cities Seoul

  # Nationwide across 25 cities
  python scripts/scrape_studios.py --all-cities

  # Single source test
  python scripts/scrape_studios.py --source kakao --cities Seoul --dry-run

  # Sync output to S3 after crawl
  python scripts/scrape_studios.py --all-cities --s3-sync

Environment (set in /etc/environment on EC2)
--------------------------------------------
  KAKAO_REST_API_KEY=...
  NAVER_CLIENT_ID=...
  NAVER_CLIENT_SECRET=...
  AWS_S3_BUCKET=yogaq-crawl-raw-ap2        (optional, defaults to this)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR   = REPO_ROOT / "data" / "studios"
OUT_JSON  = OUT_DIR / "studios_raw.json"
OUT_SQL   = OUT_DIR / "studios_seed.sql"

# ── API config ────────────────────────────────────────────────────────────────
KAKAO_KEY    = os.environ.get("KAKAO_REST_API_KEY", "")
NAVER_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
S3_BUCKET    = os.environ.get("AWS_S3_BUCKET", "yogaq-crawl-raw-ap2")

KAKAO_URL  = "https://dapi.kakao.com/v2/local/search/keyword.json"
NAVER_URL  = "https://openapi.naver.com/v1/search/local.json"

# ── Search keywords ───────────────────────────────────────────────────────────
YOGA_KEYWORDS = [
    "요가스튜디오",
    "요가원",
    "요가학원",
    "필라테스요가",
    "아쉬탕가요가",
    "핫요가",
    "빈야사요가",
    "음요가",
    "남성요가",
]

# 25 major Korean cities with their Korean names for search queries
CITIES: dict[str, str] = {
    "Seoul":     "서울",
    "Busan":     "부산",
    "Daegu":     "대구",
    "Incheon":   "인천",
    "Gwangju":   "광주",
    "Daejeon":   "대전",
    "Ulsan":     "울산",
    "Suwon":     "수원",
    "Changwon":  "창원",
    "Seongnam":  "성남",
    "Goyang":    "고양",
    "Yongin":    "용인",
    "Bucheon":   "부천",
    "Cheongju":  "청주",
    "Ansan":     "안산",
    "Jeonju":    "전주",
    "Anyang":    "안양",
    "Cheonan":   "천안",
    "Namyangju": "남양주",
    "Hwaseong":  "화성",
    "Jeju":      "제주",
    "Gimhae":    "김해",
    "Hanam":     "하남",
    "Uijeongbu": "의정부",
    "Siheung":   "시흥",
}

SEOUL_DISTRICTS = [
    "강남", "서초", "송파", "마포", "용산", "성수", "홍대", "연남", "합정",
    "이태원", "종로", "중구", "강서", "강동", "노원", "도봉", "은평", "서대문",
    "동작", "관악", "금천", "구로", "양천", "영등포", "동대문", "성북", "중랑",
]


# ── Deduplication ─────────────────────────────────────────────────────────────

_NAME_NOISE = re.compile(r"[\s\-_·•()\[\]【】（）]")


def _coord_key(x: str, y: str, precision: int = 4) -> str:
    """Round coordinates to ~11m grid for deduplication."""
    try:
        return f"{round(float(x), precision)},{round(float(y), precision)}"
    except (ValueError, TypeError):
        return f"{x},{y}"


def _normalize_name(name: str) -> str:
    """Strip whitespace and punctuation for cross-source name matching.

    Intentionally keeps suffixes like 요가원/스튜디오 so that '강남요가원' and
    '강남요가스튜디오' are treated as distinct businesses (they might be), while
    '강남요가스튜디오' appearing in both Kakao and gov_sangga still collapses.
    """
    return _NAME_NOISE.sub("", name.strip()).lower()


def _extract_district(s: dict) -> str:
    """Return the 시군구 token from address fields (or gov_시군구 if present)."""
    if s.get("gov_시군구"):
        return s["gov_시군구"]
    addr = s.get("road_address") or s.get("address") or ""
    # Korean addresses: 서울특별시 종로구 … → take the second space-delimited token
    parts = addr.split()
    return parts[1] if len(parts) > 1 else ""


def _field_score(s: dict) -> int:
    """Count non-empty fields as a proxy for record richness."""
    return sum(1 for v in s.values() if v and v not in ("", [], {}))


def deduplicate(studios: list[dict]) -> list[dict]:
    """Deduplicate studios by coordinate grid, then by normalised name+district.

    Two passes:
    1. Coordinate key at ~11 m precision — catches repeated API results.
    2. Normalised name + 시군구 — catches same business from different sources
       (e.g. Kakao vs 소상공인) when coordinates differ by more than 11 m.
       Only applied when the normalised name is ≥ 4 chars to avoid false merges
       on generic names like '요가원'.
    """
    by_coord: dict[str, dict] = {}
    # maps (norm_name, district) -> coord_key of the winning record
    by_name_district: dict[tuple[str, str], str] = {}

    for s in studios:
        coord_key = _coord_key(s.get("x", ""), s.get("y", ""))
        norm = _normalize_name(s.get("name", ""))
        district = _extract_district(s)
        name_key: tuple[str, str] | None = (norm, district) if len(norm) >= 4 else None

        if coord_key in by_coord:
            existing = by_coord[coord_key]
            if _field_score(s) > _field_score(existing):
                by_coord[coord_key] = s
            continue

        if name_key and name_key in by_name_district:
            existing_coord = by_name_district[name_key]
            existing = by_coord.get(existing_coord)
            if existing and _field_score(s) > _field_score(existing):
                # Replace the old record with the richer one at the new coord key
                del by_coord[existing_coord]
                by_coord[coord_key] = s
                by_name_district[name_key] = coord_key
            continue

        by_coord[coord_key] = s
        if name_key:
            by_name_district[name_key] = coord_key

    return list(by_coord.values())


# ── Kakao Local API ───────────────────────────────────────────────────────────

def kakao_search(query: str, delay: float = 1.0, dry_run: bool = False) -> list[dict]:
    """
    Fetch all pages for a keyword from Kakao Local API.
    Max 45 pages × 15 results = 675 results per keyword.
    """
    if not KAKAO_KEY:
        log.warning("KAKAO_REST_API_KEY not set — skipping Kakao")
        return []

    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    results: list[dict] = []
    page = 1

    with httpx.Client(timeout=15) as client:
        while True:
            params = {
                "query": query,
                "size": 15,
                "page": page,
            }
            if dry_run:
                log.info("[DRY-RUN] Kakao query=%r page=%d", query, page)
                break

            try:
                resp = client.get(KAKAO_URL, params=params, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Kakao HTTP error page=%d: %s", page, exc)
                break

            data = resp.json()
            documents = data.get("documents", [])
            if not documents:
                break

            for doc in documents:
                results.append({
                    "source":          "kakao",
                    "source_id":       doc.get("id", ""),
                    "name":            doc.get("place_name", ""),
                    "category":        doc.get("category_name", ""),
                    "phone":           doc.get("phone", ""),
                    "address":         doc.get("address_name", ""),
                    "road_address":    doc.get("road_address_name", ""),
                    "x":               doc.get("x", ""),
                    "y":               doc.get("y", ""),
                    "place_url":       doc.get("place_url", ""),
                    "crawled_at":      datetime.now(timezone.utc).isoformat(),
                })

            meta = data.get("meta", {})
            log.info("Kakao %r page %d → %d items (total_count=%s)",
                     query, page, len(documents), meta.get("total_count"))

            if meta.get("is_end", True):
                break
            page += 1
            time.sleep(delay + random.uniform(0, 0.5))

    return results


# ── Naver Local Search API ────────────────────────────────────────────────────

def naver_search(query: str, delay: float = 1.0, dry_run: bool = False) -> list[dict]:
    """
    Fetch results from Naver Local Search API.
    Max display=5, start up to 1000 → up to 1000 results per keyword (in 5 batches of 200).
    """
    if not NAVER_ID or not NAVER_SECRET:
        log.warning("NAVER_CLIENT_ID/SECRET not set — skipping Naver")
        return []

    headers = {
        "X-Naver-Client-Id":     NAVER_ID,
        "X-Naver-Client-Secret": NAVER_SECRET,
    }
    results: list[dict] = []
    start = 1
    display = 5  # Naver max per request

    with httpx.Client(timeout=15) as client:
        while start <= 1000:
            params = {
                "query":   query,
                "display": display,
                "start":   start,
                "sort":    "comment",
            }
            if dry_run:
                log.info("[DRY-RUN] Naver query=%r start=%d", query, start)
                break

            try:
                resp = client.get(NAVER_URL, params=params, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Naver HTTP error start=%d: %s", start, exc)
                break

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                # Naver coordinates are in a custom system; convert to WGS84
                mx = item.get("mapx", "")
                my = item.get("mapy", "")
                x, y = _naver_to_wgs84(mx, my)

                # Strip HTML bold tags from title
                name = item.get("title", "").replace("<b>", "").replace("</b>", "")
                results.append({
                    "source":       "naver",
                    "source_id":    f"naver_{mx}_{my}",
                    "name":         name,
                    "category":     item.get("category", ""),
                    "phone":        item.get("telephone", ""),
                    "address":      item.get("address", ""),
                    "road_address": item.get("roadAddress", ""),
                    "x":            x,
                    "y":            y,
                    "place_url":    item.get("link", ""),
                    "crawled_at":   datetime.now(timezone.utc).isoformat(),
                })

            total = data.get("total", 0)
            log.info("Naver %r start=%d → %d items (total=%s)",
                     query, start, len(items), total)

            if start + display > min(total, 1000):
                break
            start += display
            time.sleep(delay + random.uniform(0, 0.5))

    return results


def _naver_to_wgs84(mapx: str, mapy: str) -> tuple[str, str]:
    """
    Naver map coordinates are integers scaled by 1e7 from WGS84 degrees.
    e.g. mapx=1269677285 → lng=126.9677285
    """
    try:
        x = str(int(mapx) / 1e7)
        y = str(int(mapy) / 1e7)
        return x, y
    except (ValueError, TypeError):
        return mapx, mapy


# ── SQL generator ─────────────────────────────────────────────────────────────

def to_sql(studios: list[dict]) -> str:
    lines = [
        "-- Auto-generated by scrape_studios.py",
        f"-- {datetime.now(timezone.utc).isoformat()}",
        "",
        "CREATE TABLE IF NOT EXISTS studios (",
        "  id           SERIAL PRIMARY KEY,",
        "  source       TEXT NOT NULL,",
        "  source_id    TEXT,",
        "  name         TEXT NOT NULL,",
        "  category     TEXT,",
        "  phone        TEXT,",
        "  address      TEXT,",
        "  road_address TEXT,",
        "  lng          DOUBLE PRECISION,",
        "  lat          DOUBLE PRECISION,",
        "  place_url    TEXT,",
        "  crawled_at   TIMESTAMPTZ,",
        "  UNIQUE (source, source_id)",
        ");",
        "",
    ]
    for s in studios:
        def esc(v: Any) -> str:
            if v is None or v == "":
                return "NULL"
            return "'" + str(v).replace("'", "''") + "'"

        lines.append(
            f"INSERT INTO studios (source,source_id,name,category,phone,address,"
            f"road_address,lng,lat,place_url,crawled_at) VALUES ("
            f"{esc(s.get('source'))},{esc(s.get('source_id'))},{esc(s.get('name'))},"
            f"{esc(s.get('category'))},{esc(s.get('phone'))},{esc(s.get('address'))},"
            f"{esc(s.get('road_address'))},{esc(s.get('x'))},{esc(s.get('y'))},"
            f"{esc(s.get('place_url'))},{esc(s.get('crawled_at'))}"
            f") ON CONFLICT (source,source_id) DO UPDATE SET "
            f"name=EXCLUDED.name, phone=EXCLUDED.phone, crawled_at=EXCLUDED.crawled_at;"
        )
    return "\n".join(lines)


# ── S3 sync ───────────────────────────────────────────────────────────────────

def s3_sync(local_dir: Path, bucket: str) -> None:
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s3_path = f"s3://{bucket}/{date_prefix}/studios/"
    log.info("Syncing %s → %s", local_dir, s3_path)
    result = subprocess.run(
        ["aws", "s3", "sync", str(local_dir), s3_path,
         "--exclude", "*.sql",  # keep SQL local only
         "--region", "ap-northeast-2"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log.info("S3 sync complete")
    else:
        log.error("S3 sync failed: %s", result.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Yoga studio scraper — Kakao + Naver")
    p.add_argument("--source", choices=["kakao", "naver", "both"], default="both")
    p.add_argument("--cities", nargs="+", default=["Seoul"],
                   help="English city names from the CITIES dict")
    p.add_argument("--all-cities", action="store_true",
                   help="Crawl all 25 cities (overrides --cities)")
    p.add_argument("--seoul-districts", action="store_true",
                   help="Use Seoul district names as search queries (finer coverage)")
    p.add_argument("--keywords", nargs="+", default=YOGA_KEYWORDS,
                   help="Custom search keywords (Korean)")
    p.add_argument("--delay", type=float, default=1.2,
                   help="Base delay between requests in seconds (default 1.2)")
    p.add_argument("--dry-run", action="store_true",
                   help="Log requests without calling APIs")
    p.add_argument("--s3-sync", action="store_true",
                   help="Upload results to S3 after crawl")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    # Allow --out-dir to override the module-level defaults
    out_dir  = args.out_dir
    out_json = out_dir / "studios_raw.json"
    out_sql  = out_dir / "studios_seed.sql"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build query list: keyword × city (or district for Seoul)
    cities = list(CITIES.keys()) if args.all_cities else args.cities
    queries: list[str] = []

    if args.seoul_districts and "Seoul" in cities:
        for district in SEOUL_DISTRICTS:
            for kw in args.keywords:
                queries.append(f"{district} {kw}")
        cities = [c for c in cities if c != "Seoul"]

    for city in cities:
        city_kr = CITIES.get(city, city)
        for kw in args.keywords:
            queries.append(f"{city_kr} {kw}")

    log.info("Total queries: %d", len(queries))

    all_studios: list[dict] = []

    for q in queries:
        if args.source in ("kakao", "both"):
            all_studios.extend(kakao_search(q, delay=args.delay, dry_run=args.dry_run))
        if args.source in ("naver", "both"):
            all_studios.extend(naver_search(q, delay=args.delay, dry_run=args.dry_run))

    log.info("Raw results: %d", len(all_studios))
    # Merge with existing JSON so city batches accumulate rather than overwrite
    if out_json.exists():
        try:
            existing = json.loads(out_json.read_text(encoding="utf-8"))
            log.info("Merging with %d existing studios", len(existing))
            all_studios = existing + all_studios
        except Exception as exc:
            log.warning("Could not read existing JSON (%s) — starting fresh", exc)
    deduped = deduplicate(all_studios)
    log.info("After dedup: %d", len(deduped))

    # Write JSON
    out_json.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %s", out_json)

    # Write SQL
    out_sql.write_text(to_sql(deduped), encoding="utf-8")
    log.info("Wrote %s", out_sql)

    # Optional S3 sync
    if args.s3_sync and not args.dry_run:
        s3_sync(out_dir, S3_BUCKET)

    log.info("Done — %d studios saved", len(deduped))


if __name__ == "__main__":
    main()
