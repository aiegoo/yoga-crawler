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
                city_el = card.select_one(".city, .location, .address")
                cert_el = card.select_one(".credential, .cert, .registration-type")
                url_el  = card.select_one("a[href]")

                results.append({
                    "source":       "yogaalliance",
                    "type":         "school",
                    "name":         name_el.get_text(strip=True) if name_el else "",
                    "location":     city_el.get_text(strip=True) if city_el else "",
                    "cert_level":   cert_el.get_text(strip=True) if cert_el else "",
                    "country":      "KR",
                    "website":      url_el["href"] if url_el else "",
                    "member_count": None,
                    "crawled_at":   datetime.now(timezone.utc).isoformat(),
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
                city_el = card.select_one(".city, .location")
                url_el  = card.select_one("a[href]")

                results.append({
                    "source":       "yogaalliance",
                    "type":         "yacep",
                    "name":         name_el.get_text(strip=True) if name_el else "",
                    "location":     city_el.get_text(strip=True) if city_el else "",
                    "cert_level":   "YACEP",
                    "country":      "KR",
                    "website":      url_el["href"] if url_el else "",
                    "member_count": None,
                    "crawled_at":   datetime.now(timezone.utc).isoformat(),
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
                if text:
                    results.append({
                        "source":       "koreayoga",
                        "type":         "federation_member",
                        "name":         text[:120],
                        "location":     "Korea",
                        "cert_level":   "",
                        "country":      "KR",
                        "website":      url,
                        "member_count": None,
                        "crawled_at":   datetime.now(timezone.utc).isoformat(),
                    })

            if results:
                log.info("대한요가회: found %d entries at %s", len(results), url)
                break

            time.sleep(delay)

    # Always include the umbrella org itself
    results.insert(0, {
        "source":       "koreayoga",
        "type":         "national_federation",
        "name":         "대한요가회",
        "location":     "Seoul, Korea",
        "cert_level":   "국가공인",
        "country":      "KR",
        "website":      "http://www.koreayoga.or.kr",
        "member_count": None,
        "crawled_at":   datetime.now(timezone.utc).isoformat(),
    })

    return results


# ── Static known associations (seed data) ─────────────────────────────────────

KNOWN_ASSOCIATIONS: list[dict] = [
    {
        "source": "static", "type": "national_federation",
        "name": "대한요가회", "location": "Seoul",
        "cert_level": "국가공인", "country": "KR",
        "website": "http://www.koreayoga.or.kr", "member_count": None,
    },
    {
        "source": "static", "type": "national_association",
        "name": "한국요가협회", "location": "Seoul",
        "cert_level": "민간", "country": "KR",
        "website": "https://www.koreayogaassociation.com", "member_count": None,
    },
    {
        "source": "static", "type": "international_alliance",
        "name": "Yoga Alliance (International)", "location": "USA",
        "cert_level": "RYT-200/500/E-RYT", "country": "US",
        "website": "https://www.yogaalliance.org", "member_count": None,
    },
    {
        "source": "static", "type": "national_federation",
        "name": "생활체육요가연합회", "location": "Seoul",
        "cert_level": "생활체육", "country": "KR",
        "website": "", "member_count": None,
    },
    {
        "source": "static", "type": "national_association",
        "name": "한국아쉬탕가요가협회 (KAYA)", "location": "Seoul",
        "cert_level": "아쉬탕가", "country": "KR",
        "website": "", "member_count": None,
    },
    {
        "source": "static", "type": "international_alliance",
        "name": "Iyengar Yoga Association of Korea", "location": "Seoul",
        "cert_level": "Iyengar", "country": "KR",
        "website": "", "member_count": None,
    },
    {
        "source": "static", "type": "certification_body",
        "name": "YACEP (Yoga Alliance Continuing Education Provider)", "location": "USA",
        "cert_level": "YACEP", "country": "US",
        "website": "https://www.yogaalliance.org/yacep", "member_count": None,
    },
]


# ── SQL generator ─────────────────────────────────────────────────────────────

def to_sql(associations: list[dict]) -> str:
    def esc(v: Any) -> str:
        if v is None or v == "":
            return "NULL"
        return "'" + str(v).replace("'", "''") + "'"

    lines = [
        "-- Auto-generated by scrape_associations.py",
        f"-- {datetime.now(timezone.utc).isoformat()}",
        "",
        "CREATE TABLE IF NOT EXISTS associations (",
        "  id           SERIAL PRIMARY KEY,",
        "  source       TEXT NOT NULL,",
        "  type         TEXT,",
        "  name         TEXT NOT NULL,",
        "  location     TEXT,",
        "  cert_level   TEXT,",
        "  country      CHAR(2),",
        "  website      TEXT,",
        "  member_count INTEGER,",
        "  crawled_at   TIMESTAMPTZ,",
        "  UNIQUE (source, name)",
        ");",
        "",
    ]
    for a in associations:
        ts = a.get("crawled_at") or datetime.now(timezone.utc).isoformat()
        lines.append(
            f"INSERT INTO associations (source,type,name,location,cert_level,country,"
            f"website,member_count,crawled_at) VALUES ("
            f"{esc(a.get('source'))},{esc(a.get('type'))},{esc(a.get('name'))},"
            f"{esc(a.get('location'))},{esc(a.get('cert_level'))},{esc(a.get('country'))},"
            f"{esc(a.get('website'))},{esc(a.get('member_count'))},{esc(ts)}"
            f") ON CONFLICT (source,name) DO UPDATE SET "
            f"website=EXCLUDED.website, crawled_at=EXCLUDED.crawled_at;"
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
    return p.parse_args()


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

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

    OUT_JSON.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %s", OUT_JSON)

    OUT_SQL.write_text(to_sql(deduped), encoding="utf-8")
    log.info("Wrote %s", OUT_SQL)

    if args.s3_sync:
        s3_sync(OUT_DIR, S3_BUCKET)

    log.info("Done — %d associations saved", len(deduped))


if __name__ == "__main__":
    main()
