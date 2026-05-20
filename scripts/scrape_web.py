#!/usr/bin/env python3
"""
scrape_web.py — Crawl yoga studio / instructor websites for structured data.

For each studio/instructor record that has a `website` URL:
  1. Fetch the landing page (and optional sub-pages: /schedule, /classes, /prices)
  2. Extract: class schedules, price info, teacher bios, booking links, social handles
  3. Upsert enriched fields into PostgreSQL

Output schema additions (stored in facility_props / rag_payload):
  facility_props.web_classes      list of {type, time, teacher, level}
  facility_props.web_prices       list of {label, amount_krw, currency}
  facility_props.web_teachers     list of {name, bio, photo_url}
  facility_props.booking_url      primary booking/reservation URL
  rag_payload.raw_chunk           full clean text from page (for vector search)

Usage:
  python scripts/scrape_web.py                         # all studios with website
  python scripts/scrape_web.py --source gov_sangga     # gov studios only
  python scripts/scrape_web.py --limit 50              # first 50 unenriched
  python scripts/scrape_web.py --studio-id 1234        # single studio
  python scripts/scrape_web.py --dry-run               # print without writing
  python scripts/scrape_web.py --discover              # also Google-discover missing URLs

Environment:
  DATABASE_URL            postgresql://yogacrawl:yogacrawl@localhost/yogacrawl
  GOOGLE_PLACES_API_KEY   optional — used for URL discovery (--discover flag)
  USER_AGENT              custom User-Agent (optional)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import psycopg2
import psycopg2.extras
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl",
)
GOOGLE_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

UA = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# Sub-pages worth crawling for class / price info
SUB_PAGE_SLUGS = [
    "schedule", "schedules", "class", "classes", "timetable",
    "price", "prices", "pricing", "membership", "멤버십",
    "수업", "스케줄", "클래스", "시간표", "요금", "가격",
    "teacher", "teachers", "instructor", "instructors",
    "about", "소개", "강사",
]

# Patterns for booking platforms
BOOKING_PATTERNS = [
    r"mindbodyonline\.com",
    r"naverplace\.naver\.com",
    r"booking\.naver\.com",
    r"pple\.co\.kr",
    r"fresha\.com",
    r"classbento\.com",
    r"classninja",
    r"wix\.com/book",
    r"yogaconnect\.com",
    r"클래스101",
]

# Price extraction: look for patterns like ₩50,000 or 50000원
_PRICE_RE = re.compile(
    r"(?:₩|￦|KRW|원)?\s*"
    r"(\d{1,3}(?:[,.]?\d{3})*)"
    r"(?:\s*(?:₩|원|KRW|won))?",
    re.I,
)

# Social handles
_IG_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]+)', re.I)
_YT_RE = re.compile(r'youtube\.com/(?:channel|@|user)/([A-Za-z0-9_\-]+)', re.I)
_FB_RE = re.compile(r'facebook\.com/([A-Za-z0-9_./-]+)', re.I)

# Korean class types
YOGA_STYLE_KW = [
    "하타", "빈야사", "아쉬탕가", "핫요가", "음요가", "인요가", "쿤달리니",
    "플라잉요가", "아이앵거", "음성요가", "모성요가", "임산부요가",
    "hatha", "vinyasa", "ashtanga", "hot yoga", "yin yoga", "kundalini",
    "restorative", "prenatal", "aerial", "iyengar", "flow",
    "필라테스", "pilates",
]


# ── Utilities ─────────────────────────────────────────────────────────────────

def jitter(lo: float = 1.0, hi: float = 3.0) -> None:
    time.sleep(lo + random.random() * (hi - lo))


def clean_text(soup: BeautifulSoup) -> str:
    """Extract readable text, stripping nav/footer/scripts."""
    for tag in soup(["script", "style", "nav", "footer", "header",
                      "noscript", "iframe", "aside"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # Collapse whitespace
    text = re.sub(r"\s{2,}", " ", text)
    return text[:12_000]  # cap at 12 k chars for RAG chunk


def canonical_url(raw: str) -> str | None:
    """Ensure https scheme, strip trailing slash."""
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw.rstrip("/")


def fetch(url: str, client: httpx.Client, timeout: int = 15) -> BeautifulSoup | None:
    try:
        resp = client.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        log.debug("FETCH %s → %s", url, exc)
        return None


def discover_sub_pages(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Find internal links that look like schedule / price / teacher pages."""
    found = []
    base_host = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(base_url, href)
        if urlparse(full).netloc != base_host:
            continue
        path = urlparse(full).path.lower()
        if any(slug in path for slug in SUB_PAGE_SLUGS):
            found.append(full)
    return list(dict.fromkeys(found))[:6]  # deduplicate, max 6 sub-pages


# ── Extraction ─────────────────────────────────────────────────────────────────

def extract_social(full_text: str) -> dict[str, str]:
    social: dict[str, str] = {}
    m = _IG_RE.search(full_text)
    if m and m.group(1) not in ("p", "explore", "stories", "reel"):
        social["instagram"] = m.group(1)
    m = _YT_RE.search(full_text)
    if m:
        social["youtube"] = m.group(2)
    m = _FB_RE.search(full_text)
    if m:
        slug = m.group(1).strip("/")
        if slug not in ("sharer", "share", "dialog", "login"):
            social["facebook"] = slug
    return social


def extract_prices(text: str) -> list[dict]:
    """Look for price blocks near keywords like 1개월, 월정액, 1회권, 등록."""
    price_kw = ["1개월", "3개월", "6개월", "1회", "월정액", "주1회", "주2회",
                 "회원권", "등록", "수강료", "월", "membership", "drop-in",
                 "class pack", "unlimited"]
    results = []
    for kw in price_kw:
        # Find kw context windows
        idx = text.lower().find(kw.lower())
        while idx != -1:
            window = text[max(0, idx - 20):idx + 80]
            for m in _PRICE_RE.finditer(window):
                amount_str = m.group(1).replace(",", "").replace(".", "")
                amount = int(amount_str)
                if 1_000 <= amount <= 2_000_000:  # plausible KRW range
                    results.append({"label": kw, "amount_krw": amount})
            idx = text.lower().find(kw.lower(), idx + 1)
    # Deduplicate by label+amount
    seen = set()
    unique = []
    for r in results:
        k = (r["label"], r["amount_krw"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    return unique[:10]


def extract_classes(text: str) -> list[dict]:
    """Look for yoga style mentions near time patterns."""
    time_re = re.compile(r"(\d{1,2})[:\uf03a](\d{2})\s*(?:AM|PM|오전|오후)?", re.I)
    results = []
    for style in YOGA_STYLE_KW:
        idx = text.lower().find(style.lower())
        if idx == -1:
            continue
        window = text[max(0, idx - 100):idx + 200]
        times = time_re.findall(window)
        results.append({
            "style": style,
            "times": [f"{h}:{m}" for h, m in times[:4]],
        })
    return results[:15]


def extract_teachers(soup: BeautifulSoup) -> list[dict]:
    """Find teacher/instructor profile cards in the HTML."""
    teachers = []
    # Look for headings near bio-like text
    for tag in soup.find_all(["h2", "h3", "h4", "strong"]):
        text = tag.get_text(strip=True)
        # Korean name pattern or "Teacher Name" style
        if re.match(r"[가-힣]{2,5}$|[A-Z][a-z]+ [A-Z][a-z]+", text):
            bio = ""
            sib = tag.find_next_sibling(["p", "div"])
            if sib:
                bio = sib.get_text(strip=True)[:300]
            photo = None
            img = tag.find_next("img")
            if img and img.get("src"):
                photo = img["src"]
            teachers.append({"name": text, "bio": bio, "photo_url": photo})
    return teachers[:10]


def extract_booking_url(text: str, all_links: list[str]) -> str | None:
    for link in all_links:
        if any(re.search(p, link, re.I) for p in BOOKING_PATTERNS):
            return link
    return None


# ── Google Places URL discovery ───────────────────────────────────────────────

def google_discover_website(name: str, address: str) -> str | None:
    if not GOOGLE_KEY:
        return None
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": f"{name} {address}", "key": GOOGLE_KEY, "language": "ko"},
            timeout=10,
        )
        data = resp.json()
        if data.get("results"):
            place_id = data["results"][0]["place_id"]
            detail = httpx.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={"place_id": place_id, "fields": "website", "key": GOOGLE_KEY},
                timeout=10,
            ).json()
            return detail.get("result", {}).get("website")
    except Exception as e:
        log.debug("Google discover failed: %s", e)
    return None


# ── Main crawl logic ──────────────────────────────────────────────────────────

def crawl_studio(record: dict, client: httpx.Client, discover: bool = False) -> dict | None:
    """
    Crawl a studio's website. Returns enrichment dict or None if unreachable.
    """
    url = canonical_url(record.get("website") or "")

    # Optionally discover via Google Places
    if not url and discover:
        url = google_discover_website(record["name"], record.get("road_address") or "")
        if url:
            log.info("  Google-discovered URL: %s → %s", record["name"], url)

    if not url:
        return None

    log.info("Crawling %s → %s", record["name"], url)

    soup = fetch(url, client)
    if not soup:
        log.warning("  Unreachable: %s", url)
        return None

    full_text = clean_text(soup)
    all_links = [a["href"] for a in soup.find_all("a", href=True)]

    # Crawl sub-pages (schedule, prices, teachers)
    sub_pages = discover_sub_pages(url, soup)
    for sub_url in sub_pages:
        jitter(0.5, 1.5)
        sub_soup = fetch(sub_url, client)
        if sub_soup:
            full_text += " " + clean_text(sub_soup)
            all_links += [a["href"] for a in sub_soup.find_all("a", href=True)]
        if len(full_text) > 24_000:
            break  # enough content

    # Resolve relative booking links
    abs_links = [urljoin(url, lnk) for lnk in all_links]

    result: dict[str, Any] = {
        "website": url,
        "web_crawled_at": datetime.now(timezone.utc).isoformat(),
        "web_text_len": len(full_text),
    }

    # Social handles
    social = extract_social(full_text)
    result.update(social)

    # Structured extraction
    result["web_classes"] = extract_classes(full_text)
    result["web_prices"] = extract_prices(full_text)
    result["web_teachers"] = extract_teachers(soup)
    result["booking_url"] = extract_booking_url(full_text, abs_links)
    result["rag_chunk"] = full_text[:8_000]  # first 8k for RAG

    return result


def upsert_enrichment(conn, studio_id: int, data: dict) -> None:
    """Merge crawled web data back into the studios table."""
    cur = conn.cursor()

    # Extract top-level columns
    updates: dict[str, Any] = {}
    if data.get("website"):
        updates["website"] = data["website"]
    if data.get("instagram"):
        updates["instagram"] = data["instagram"]
    if data.get("youtube"):
        updates["youtube"] = data["youtube"]
    if data.get("facebook"):
        updates["facebook"] = data["facebook"]

    # Merge into facility_props jsonb
    fp_patch = {
        k: data[k]
        for k in ("web_classes", "web_prices", "web_teachers",
                  "booking_url", "web_crawled_at", "web_text_len")
        if data.get(k) is not None
    }

    # RAG payload
    rag_patch = {}
    if data.get("rag_chunk"):
        rag_patch["raw_chunk"] = data["rag_chunk"]
        rag_patch["source_url"] = data.get("website")
        rag_patch["crawled_at"] = data.get("web_crawled_at")

    set_parts = []
    values: list[Any] = []

    for col, val in updates.items():
        set_parts.append(f"{col} = %s")
        values.append(val)

    if fp_patch:
        set_parts.append(
            "facility_props = COALESCE(facility_props, '{}'::jsonb) || %s::jsonb"
        )
        values.append(json.dumps(fp_patch, ensure_ascii=False))

    if rag_patch:
        set_parts.append(
            "rag_payload = COALESCE(rag_payload, '{}'::jsonb) || %s::jsonb"
        )
        values.append(json.dumps(rag_patch, ensure_ascii=False))

    if not set_parts:
        return

    set_parts.append("enriched_at = NOW()")
    values.append(studio_id)

    cur.execute(
        f"UPDATE studios SET {', '.join(set_parts)} WHERE id = %s",
        values,
    )
    conn.commit()
    cur.close()


def load_targets(conn, source: str | None, limit: int, studio_id: int | None,
                 discover: bool) -> list[dict]:
    """Load studios to crawl from DB."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    where = []
    params: list[Any] = []

    if studio_id:
        where.append("id = %s")
        params.append(studio_id)
    else:
        if source:
            where.append("source = %s")
            params.append(source)
        if not discover:
            # Only crawl if already has website URL
            where.append("website IS NOT NULL AND website != ''")
        # Skip recently enriched (last 7 days)
        where.append(
            "(enriched_at IS NULL OR enriched_at < NOW() - INTERVAL '7 days')"
        )

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ORDER BY enriched_at ASC NULLS FIRST"
    limit_clause = f"LIMIT {limit}"

    cur.execute(
        f"SELECT id, name, website, road_address, address, source "
        f"FROM studios {where_clause} {order} {limit_clause}",
        params,
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    log.info("Loaded %d studios to crawl", len(rows))
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Crawl yoga studio websites")
    p.add_argument("--source", default=None,
                   help="Filter by source (kakao/naver/gov_sangga)")
    p.add_argument("--limit", type=int, default=200,
                   help="Max studios to process (default 200)")
    p.add_argument("--studio-id", type=int, default=None,
                   help="Crawl a single studio by DB id")
    p.add_argument("--discover", action="store_true",
                   help="Use Google Places to discover missing website URLs")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Seconds between requests (default 1.5)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print results without writing to DB")
    args = p.parse_args()

    conn = psycopg2.connect(DB_URL)

    targets = load_targets(
        conn, args.source, args.limit, args.studio_id, args.discover
    )

    client = httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"},
        timeout=20,
        follow_redirects=True,
    )

    ok = skip = err = 0
    for rec in targets:
        try:
            data = crawl_studio(rec, client, discover=args.discover)
            if data is None:
                skip += 1
                continue
            if args.dry_run:
                print(json.dumps(
                    {"id": rec["id"], "name": rec["name"], **data},
                    ensure_ascii=False, indent=2
                ))
            else:
                upsert_enrichment(conn, rec["id"], data)
            ok += 1
            jitter(args.delay * 0.8, args.delay * 1.4)
        except Exception as exc:
            log.warning("Error crawling %s: %s", rec.get("name"), exc)
            err += 1

    client.close()
    conn.close()

    log.info("Done. crawled=%d  skipped=%d  errors=%d", ok, skip, err)


if __name__ == "__main__":
    main()
