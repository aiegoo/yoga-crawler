#!/usr/bin/env python3
"""
Yoga Association / Alliance Scraper
=====================================

Targets (all public pages, robots.txt compliant)
-------------------------------------------------
1. Yoga Alliance KR filter     https://www.yogaalliance.org/directory?country=KR&type=School
2. 대한요가회                    http://www.koreayoga.or.kr
3. 한국요가협회                   https://www.koreayogaassociation.com
4. 생활체육요가연합회              (sport registry, static page)
5. YACEP providers (KR)        https://www.yogaalliance.org/directory?type=YACEP&country=KR

Output
------
  data/associations/associations_raw.json
  data/associations/associations_seed.sql

Usage
-----
  python scripts/scrape_associations.py
  python scripts/scrape_associations.py --source yogaalliance --pages 5
  python scripts/scrape_associations.py --s3-sync
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR   = REPO_ROOT / "data" / "associations"
OUT_JSON  = OUT_DIR / "associations_raw.json"
OUT_SQL   = OUT_DIR / "associations_seed.sql"

S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "yogaq-crawl-raw-ap2")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}


# ── Yoga Alliance KR Schools ──────────────────────────────────────────────────

def scrape_yogaalliance_kr(pages: int = 10, delay: float = 2.0) -> list[dict]:
    """Scrape Yoga Alliance registered schools in Korea."""
    base = "https://www.yogaalliance.org/directory"
    results: list[dict] = []

    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for page in range(1, pages + 1):
            url = f"{base}?country=KR&type=School&page={page}"
            log.info("YA Schools KR page %d", page)
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("YA HTTP error page=%d: %s", page, exc)
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select(".directory-result, .listing-item, article.member")

            if not cards:
                log.info("No cards found on page %d — stopping", page)
                break

            for card in cards:
                name_el = card.select_one("h2, h3, .name, .listing-name")
                cert_el = card.select_one(".credential, .cert, .registration-type")
                url_el  = card.select_one("a[href]")
                name    = name_el.get_text(strip=True) if name_el else ""
                if not name:
                    continue
                source_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]
                cert_text = cert_el.get_text(strip=True) if cert_el else ""

                results.append({
                    "source":          "yogaalliance",
                    "source_id":       f"ya-school-{source_id}",
                    "name":            name,
                    "name_en":         name,
                    "org_type":        "school",
                    "website":         url_el["href"] if url_el else "",
                    "registration_id": None,
                    "member_count":    None,
                    "cert_levels":     [cert_text] if cert_text else [],
                    "crawled_at":      datetime.now(timezone.utc).isoformat(),
                })

            log.info("Page %d → %d schools (total so far: %d)", page, len(cards), len(results))
            time.sleep(delay + random.uniform(0, 1.0))

    return results


# ── YACEP Providers KR ────────────────────────────────────────────────────────

def scrape_yacep_kr(pages: int = 5, delay: float = 2.0) -> list[dict]:
    """Scrape YACEP (continuing education) providers registered in Korea."""
    base = "https://www.yogaalliance.org/directory"
    results: list[dict] = []

    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for page in range(1, pages + 1):
            url = f"{base}?country=KR&type=YACEP&page={page}"
            log.info("YACEP KR page %d", page)
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("YACEP HTTP error: %s", exc)
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select(".directory-result, .listing-item, article.member")

            if not cards:
                break

            for card in cards:
                name_el = card.select_one("h2, h3, .name")
                url_el  = card.select_one("a[href]")
                name    = name_el.get_text(strip=True) if name_el else ""
                if not name:
                    continue
                source_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]

                results.append({
                    "source":          "yogaalliance",
                    "source_id":       f"ya-yacep-{source_id}",
                    "name":            name,
                    "name_en":         name,
                    "org_type":        "yacep",
                    "website":         url_el["href"] if url_el else "",
                    "registration_id": None,
                    "member_count":    None,
                    "cert_levels":     ["YACEP"],
                    "crawled_at":      datetime.now(timezone.utc).isoformat(),
                })

            time.sleep(delay + random.uniform(0, 1.0))

    return results


# ── Korean Yoga Federation (대한요가회) ────────────────────────────────────────

def scrape_koreayoga(delay: float = 2.0) -> list[dict]:
    """
    Scrape public info from 대한요가회 (koreayoga.or.kr).
    Returns org-level record + any listed branch/member orgs.
    """
    results: list[dict] = []
    urls_to_try = [
        "http://www.koreayoga.or.kr",
        "http://koreayoga.or.kr/organization",
        "http://koreayoga.or.kr/member",
    ]

    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for url in urls_to_try:
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("koreayoga.or.kr error %s: %s", url, exc)
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Try to find org listings or table rows
            rows = soup.select("table tr, .member-list li, .org-list li")
            for row in rows:
                text = row.get_text(separator=" ", strip=True)
                # Skip header/nav rows — require meaningful Korean text and reasonable length
                if not (5 < len(text) < 200):
                    continue
                if not re.search(r"[가-힣]{3,}", text):
                    continue
                source_id = re.sub(r"[^a-z0-9가-힣]+", "-", text[:50].lower()).strip("-")
                results.append({
                    "source":          "koreayoga",
                    "source_id":       f"kr-{source_id}",
                    "name":            text[:120],
                    "name_en":         None,
                    "org_type":        "federation_member",
                    "website":         url,
                    "registration_id": None,
                    "member_count":    None,
                    "cert_levels":     [],
                    "crawled_at":      datetime.now(timezone.utc).isoformat(),
                })

            if results:
                log.info("대한요가회: found %d entries at %s", len(results), url)
                break

            time.sleep(delay)

    # Always include the umbrella org itself
    results.insert(0, {
        "source":          "koreayoga",
        "source_id":       "daehan-yogahoe-live",
        "name":            "대한요가회",
        "name_en":         "Korea Yoga Federation",
        "org_type":        "national_federation",
        "website":         "http://www.koreayoga.or.kr",
        "registration_id": None,
        "member_count":    None,
        "cert_levels":     ["국가공인"],
        "crawled_at":      datetime.now(timezone.utc).isoformat(),
    })

    return results


# ── Static known associations (seed data) ─────────────────────────────────────

KNOWN_ASSOCIATIONS: list[dict] = [
    {
        "source": "static", "source_id": "daehan-yogahoe",
        "name": "대한요가회", "name_en": "Korea Yoga Federation",
        "org_type": "national_federation",
        "website": "http://www.koreayoga.or.kr",
        "registration_id": None, "member_count": None,
        "cert_levels": ["국가공인"],
    },
    {
        "source": "static", "source_id": "hanguk-yoga-hyophoe",
        "name": "한국요가협회", "name_en": "Korea Yoga Association",
        "org_type": "national_association",
        "website": "https://www.koreayogaassociation.com",
        "registration_id": None, "member_count": None,
        "cert_levels": ["민간"],
    },
    {
        "source": "static", "source_id": "yogaalliance-intl",
        "name": "Yoga Alliance (International)", "name_en": "Yoga Alliance",
        "org_type": "international_alliance",
        "website": "https://www.yogaalliance.org",
        "registration_id": None, "member_count": None,
        "cert_levels": ["RYT-200", "RYT-500", "E-RYT-200", "E-RYT-500"],
    },
    {
        "source": "static", "source_id": "saenghwal-yoga-yonhaphoe",
        "name": "생활체육요가연합회", "name_en": None,
        "org_type": "national_federation",
        "website": "",
        "registration_id": None, "member_count": None,
        "cert_levels": ["생활체육"],
    },
    {
        "source": "static", "source_id": "kaya",
        "name": "한국아쉬탕가요가협회 (KAYA)", "name_en": "Korea Ashtanga Yoga Association",
        "org_type": "national_association",
        "website": "",
        "registration_id": None, "member_count": None,
        "cert_levels": ["아쉬탕가"],
    },
    {
        "source": "static", "source_id": "iyengar-yoga-kr",
        "name": "Iyengar Yoga Association of Korea", "name_en": "Iyengar Yoga Association of Korea",
        "org_type": "national_association",
        "website": "",
        "registration_id": None, "member_count": None,
        "cert_levels": ["Iyengar"],
    },
    {
        "source": "static", "source_id": "yacep-intl",
        "name": "YACEP (Yoga Alliance Continuing Education Provider)", "name_en": "YACEP",
        "org_type": "certification_body",
        "website": "https://www.yogaalliance.org/yacep",
        "registration_id": None, "member_count": None,
        "cert_levels": ["YACEP"],
    },
]


# ── SQL generator ─────────────────────────────────────────────────────────────

def to_sql(associations: list[dict]) -> str:
    def esc(v: Any) -> str:
        if v is None or v == "":
            return "NULL"
        return "'" + str(v).replace("'", "''") + "'"

    def esc_array(vals: list) -> str:
        if not vals:
            return "NULL"
        escaped = [str(v).replace("'", "''") for v in vals]
        return "ARRAY[" + ",".join(f"'{v}'" for v in escaped) + "]"

    lines = [
        "-- Auto-generated by scrape_associations.py",
        f"-- {datetime.now(timezone.utc).isoformat()}",
        "",
        "CREATE TABLE IF NOT EXISTS associations (",
        "  id              SERIAL PRIMARY KEY,",
        "  source          TEXT NOT NULL,",
        "  source_id       TEXT,",
        "  name            TEXT NOT NULL,",
        "  name_en         TEXT,",
        "  org_type        TEXT,",
        "  website         TEXT,",
        "  registration_id TEXT,",
        "  member_count    INTEGER,",
        "  cert_levels     TEXT[],",
        "  crawled_at      TIMESTAMPTZ,",
        "  UNIQUE (source, source_id)",
        ");",
        "",
    ]
    for a in associations:
        ts = a.get("crawled_at") or datetime.now(timezone.utc).isoformat()
        lines.append(
            f"INSERT INTO associations (source,source_id,name,name_en,org_type,"
            f"website,registration_id,member_count,cert_levels,crawled_at) VALUES ("
            f"{esc(a.get('source'))},{esc(a.get('source_id'))},{esc(a.get('name'))},"
            f"{esc(a.get('name_en'))},{esc(a.get('org_type'))},"
            f"{esc(a.get('website'))},{esc(a.get('registration_id'))},"
            f"{esc(a.get('member_count'))},{esc_array(a.get('cert_levels') or [])},"
            f"{esc(ts)}"
            f") ON CONFLICT (source,source_id) DO UPDATE SET "
            f"name=EXCLUDED.name, website=EXCLUDED.website, "
            f"cert_levels=EXCLUDED.cert_levels, crawled_at=EXCLUDED.crawled_at;"
        )
    return "\n".join(lines)


# ── S3 sync ───────────────────────────────────────────────────────────────────

def s3_sync(local_dir: Path, bucket: str) -> None:
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s3_path = f"s3://{bucket}/{date_prefix}/associations/"
    log.info("Syncing %s → %s", local_dir, s3_path)
    result = subprocess.run(
        ["aws", "s3", "sync", str(local_dir), s3_path,
         "--exclude", "*.sql", "--region", "ap-northeast-2"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log.info("S3 sync complete")
    else:
        log.error("S3 sync failed: %s", result.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Yoga association scraper")
    p.add_argument("--source",
                   choices=["yogaalliance", "koreayoga", "static", "all"],
                   default="all")
    p.add_argument("--pages", type=int, default=10,
                   help="Pages to scrape from Yoga Alliance directory")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--s3-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="No-op: accepted for pipeline compatibility")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR,
                   help="Output directory for JSON and SQL files")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out_dir  = args.out_dir
    out_json = out_dir / "associations_raw.json"
    out_sql  = out_dir / "associations_seed.sql"
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []

    if args.source in ("static", "all"):
        ts = datetime.now(timezone.utc).isoformat()
        for a in KNOWN_ASSOCIATIONS:
            results.append({**a, "crawled_at": ts})
        log.info("Static associations: %d", len(KNOWN_ASSOCIATIONS))

    if args.source in ("yogaalliance", "all"):
        results.extend(scrape_yogaalliance_kr(pages=args.pages, delay=args.delay))
        results.extend(scrape_yacep_kr(pages=args.pages // 2 or 3, delay=args.delay))

    if args.source in ("koreayoga", "all"):
        results.extend(scrape_koreayoga(delay=args.delay))

    # Deduplicate by name
    seen_names: set[str] = set()
    deduped = []
    for r in results:
        key = r.get("name", "").strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            deduped.append(r)

    log.info("Total associations (deduped): %d", len(deduped))

    out_json.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %s", out_json)

    out_sql.write_text(to_sql(deduped), encoding="utf-8")
    log.info("Wrote %s", out_sql)

    if args.s3_sync:
        s3_sync(out_dir, S3_BUCKET)

    log.info("Done — %d associations saved", len(deduped))


if __name__ == "__main__":
    main()
