#!/usr/bin/env python3
"""
scrape_instructor_profiles.py — Crawl structured yoga instructor profiles.

Sources (in priority order)
───────────────────────────
1. 탈잉 (taling.me)        — private lesson marketplace; best structured profiles
   https://taling.me/search/talent?keyword={city}+요가강사
   Fields: name, bio, certifications (from bio), style, price/hr, location, rating

2. 레슨올 (lessonall.com)  — dedicated private lesson booking platform
   https://lessonall.com/teacher/list?q=요가&location={city}
   Fields: name, subjects, certifications, hourly_rate, area, availability

3. 크몽 (kmong.com)         — freelance marketplace; many yoga/pilates instructors
   https://kmong.com/search?keyword=요가강사&category=141
   Fields: name, description, price/session, reviews, level

4. 사람인 (saramin.co.kr)   — job portal; instructor job postings reveal profiles
   https://www.saramin.co.kr/zf_user/search?searchword=요가강사
   Fields: name, certifications, career_years, specialty, location

All records are upserted into the `instructors` table.

Usage
─────
  python scripts/scrape_instructor_profiles.py                   # all sources
  python scripts/scrape_instructor_profiles.py --source taling
  python scripts/scrape_instructor_profiles.py --source lessonall
  python scripts/scrape_instructor_profiles.py --source kmong
  python scripts/scrape_instructor_profiles.py --city 서울 --delay 1.2
  python scripts/scrape_instructor_profiles.py --dry-run

Environment
───────────
  CRAWL_DB_URL   postgresql://yogacrawl:yogacrawl@localhost/yogacrawl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import random
from datetime import datetime, timezone
from typing import Any

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

DB_URL = os.getenv("CRAWL_DB_URL", "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _chromium_executable() -> str | None:
    """Return path to playwright-managed or manually installed Chromium binary."""
    import glob
    patterns = [
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for pat in patterns:
        hits = glob.glob(pat) if "*" in pat else ([pat] if os.path.exists(pat) else [])
        if hits:
            return hits[0]
    return None

KR_CITIES = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산",
    "수원", "고양", "창원", "성남", "청주", "전주", "안산",
]

# Korean yoga certification bodies — used to extract cert level from bio text
CERT_PATTERNS = [
    (r"RYT[-\s]?(\d+)", lambda m: f"RYT-{m.group(1)}"),
    (r"YTT[-\s]?(\d+)", lambda m: f"YTT-{m.group(1)}"),
    (r"E-RYT[-\s]?(\d+)", lambda m: f"E-RYT-{m.group(1)}"),
    (r"YACEP",            lambda m: "YACEP"),
    (r"KYA|한국요가협회",  lambda m: "KYA"),
    (r"KYF|한국요가연맹",  lambda m: "KYF"),
    (r"대한요가회",        lambda m: "KYC"),
    (r"한국요가지도사협회", lambda m: "KYIA"),
    (r"아헹가",            lambda m: "Iyengar"),
    (r"쿤달리니",          lambda m: "Kundalini"),
]

STYLE_KEYWORDS = {
    "하타": "hatha", "아쉬탕가": "ashtanga", "빈야사": "vinyasa",
    "핫요가": "hot_yoga", "음요가": "yin", "쿤달리니": "kundalini",
    "아헹가": "iyengar", "명상": "meditation", "필라테스": "pilates",
    "코어": "core", "산전": "prenatal", "산후": "postnatal",
    "시니어": "senior", "어린이": "kids", "임산부": "prenatal",
}


# ── Certification extraction ───────────────────────────────────────────────
def extract_certs(text: str) -> list[str]:
    certs = []
    for pattern, formatter in CERT_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            certs.append(formatter(m))
    return list(dict.fromkeys(certs))  # deduplicate, preserve order


def extract_specialties(text: str) -> list[str]:
    return [en for kr, en in STYLE_KEYWORDS.items() if kr in text]


def extract_price(text: str) -> float | None:
    nums = re.sub(r"[^\d]", "", text)
    if nums:
        val = int(nums)
        if 5000 <= val <= 500_000:
            return float(val)
    return None


# ── 탈잉 (taling.me) ────────────────────────────────────────────────────────
def scrape_taling_instructors(city: str, client: httpx.Client, delay: float) -> list[dict]:
    """Scrape yoga instructor profiles from taling.me."""
    results = []
    seen: set[str] = set()
    queries = [f"{city} 요가강사", f"{city} 필라테스강사", f"{city} 요가 개인레슨"]

    for q in queries:
        for page in range(1, 4):  # up to 3 pages per query
            try:
                r = client.get(
                    "https://www.taling.me/search",
                    params={"keyword": q, "page": page},
                    headers={"User-Agent": UA},
                    timeout=15,
                    follow_redirects=True,
                )
                soup = BeautifulSoup(r.text, "html.parser")
                cards = soup.select(
                    ".talent-item, .card-talent, [data-talent-id], "
                    "article.item, .tutor-card, [class*='talent']"
                )
                if not cards:
                    break  # no more pages

                for card in cards:
                    name_el = card.select_one(
                        ".tutor-name, .instructor-name, [class*='tutor'], "
                        "[class*='name'], h3, h4"
                    )
                    bio_el = card.select_one(
                        ".description, .bio, [class*='desc'], p"
                    )
                    price_el = card.select_one(".price, [class*='price']")
                    area_el  = card.select_one(".area, .location, [class*='area']")
                    link_el  = card.select_one("a[href]")
                    rating_el = card.select_one(".rating, .score, [class*='rating']")

                    name = name_el.get_text(strip=True) if name_el else ""
                    bio  = bio_el.get_text(strip=True)  if bio_el  else ""
                    if not name:
                        continue

                    uid = hashlib.md5(f"taling_{name}_{city}".encode()).hexdigest()[:12]
                    if uid in seen:
                        continue
                    seen.add(uid)

                    results.append({
                        "source":         "taling",
                        "source_id":      uid,
                        "full_name":      name,
                        "city":           city,
                        "bio":            bio,
                        "certifications": extract_certs(bio),
                        "specialties":    extract_specialties(bio + " " + name),
                        "website":        "https://taling.me" + link_el["href"] if link_el else None,
                        "price_per_hour": extract_price(price_el.get_text() if price_el else ""),
                        "area":           area_el.get_text(strip=True) if area_el else city,
                        "rating":         _parse_rating(rating_el.get_text() if rating_el else ""),
                        "data_source":    "taling",
                        "crawled_at":     datetime.now(timezone.utc).isoformat(),
                    })

                log.info(f"  taling '{q}' page {page}: {len(results)} profiles so far")
                time.sleep(delay + random.uniform(0, 0.5))

                # Stop paging if we got fewer cards than expected
                if len(cards) < 10:
                    break

            except Exception as e:
                log.warning(f"  taling '{q}' page {page}: {e}")
                break

    return results


# ── 레슨올 (lessonall.com) ──────────────────────────────────────────────────
def scrape_lessonall_instructors(city: str, client: httpx.Client, delay: float) -> list[dict]:
    """Scrape yoga instructor profiles from lessonall.com."""
    results = []
    seen: set[str] = set()
    params_list = [
        {"q": "요가", "location": city},
        {"q": "필라테스", "location": city},
    ]

    for params in params_list:
        for page in range(1, 4):
            try:
                r = client.get(
                    "https://lessonall.com/teacher/list",
                    params={**params, "page": page},
                    headers={"User-Agent": UA},
                    timeout=15,
                    follow_redirects=True,
                )
                soup = BeautifulSoup(r.text, "html.parser")
                cards = soup.select(
                    ".teacher-card, .teacher-item, .instructor-card, "
                    "article, [class*='teacher'], [class*='instructor']"
                )
                if not cards:
                    break

                for card in cards:
                    name_el    = card.select_one("h2, h3, h4, .name, [class*='name']")
                    subject_el = card.select_one(".subject, .category, [class*='subject'], .tag")
                    bio_el     = card.select_one(".desc, .intro, .bio, p")
                    price_el   = card.select_one(".price, [class*='price'], .rate")
                    area_el    = card.select_one(".area, .location, [class*='area']")
                    cert_el    = card.select_one(".cert, .certification, [class*='cert']")
                    link_el    = card.select_one("a[href]")

                    name = name_el.get_text(strip=True) if name_el else ""
                    bio  = " ".join([
                        (bio_el.get_text(strip=True)     if bio_el     else ""),
                        (subject_el.get_text(strip=True) if subject_el else ""),
                        (cert_el.get_text(strip=True)    if cert_el    else ""),
                    ])
                    if not name:
                        continue

                    uid = hashlib.md5(f"lessonall_{name}_{city}".encode()).hexdigest()[:12]
                    if uid in seen:
                        continue
                    seen.add(uid)

                    results.append({
                        "source":         "lessonall",
                        "source_id":      uid,
                        "full_name":      name,
                        "city":           city,
                        "bio":            bio.strip(),
                        "certifications": extract_certs(bio),
                        "specialties":    extract_specialties(bio),
                        "website":        "https://lessonall.com" + link_el["href"] if link_el else None,
                        "price_per_hour": extract_price(price_el.get_text() if price_el else ""),
                        "area":           area_el.get_text(strip=True) if area_el else city,
                        "data_source":    "lessonall",
                        "crawled_at":     datetime.now(timezone.utc).isoformat(),
                    })

                log.info(f"  lessonall '{params['q']}' {city} page {page}: {len(results)} so far")
                time.sleep(delay + random.uniform(0, 0.5))

                if len(cards) < 10:
                    break

            except Exception as e:
                log.warning(f"  lessonall {params} page {page}: {e}")
                break

    return results


# ── 크몽 (kmong.com) ────────────────────────────────────────────────────────
def scrape_kmong_instructors(city: str, client: httpx.Client, delay: float) -> list[dict]:
    """Scrape yoga instructor gig listings from kmong.com."""
    results = []
    seen: set[str] = set()

    for keyword in [f"{city} 요가강사", f"{city} 요가 개인레슨", "필라테스 강사"]:
        try:
            r = client.get(
                "https://kmong.com/search",
                params={"keyword": keyword},
                headers={"User-Agent": UA},
                timeout=15,
                follow_redirects=True,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(
                ".gig-card, .product-card, article, "
                "[class*='gig'], [class*='product']"
            )

            for card in cards:
                title_el  = card.select_one("h3, h4, .title, [class*='title']")
                seller_el = card.select_one(".seller, .expert, [class*='seller'], [class*='expert']")
                price_el  = card.select_one(".price, [class*='price']")
                link_el   = card.select_one("a[href]")

                title  = title_el.get_text(strip=True)  if title_el  else ""
                seller = seller_el.get_text(strip=True) if seller_el else ""
                if not title or not any(kw in title for kw in ["요가", "필라테스", "yoga"]):
                    continue

                uid = hashlib.md5(f"kmong_{title}_{seller}".encode()).hexdigest()[:12]
                if uid in seen:
                    continue
                seen.add(uid)

                results.append({
                    "source":         "kmong",
                    "source_id":      uid,
                    "full_name":      seller or title,
                    "city":           city,
                    "bio":            title,
                    "certifications": extract_certs(title),
                    "specialties":    extract_specialties(title),
                    "website":        "https://kmong.com" + link_el["href"] if link_el else None,
                    "price_per_hour": extract_price(price_el.get_text() if price_el else ""),
                    "data_source":    "kmong",
                    "crawled_at":     datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"  kmong '{keyword}': {len(results)} profiles so far")
        except Exception as e:
            log.warning(f"  kmong '{keyword}': {e}")
        finally:
            time.sleep(delay + random.uniform(0, 0.8))

    return results


# ── Helpers ────────────────────────────────────────────────────────────────
def _pw_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def scrape_taling_instructors_playwright(city: str, delay: float) -> list[dict]:
    """Playwright version of taling instructor scraper — intercepts API calls."""
    from playwright.sync_api import sync_playwright

    results = []
    seen: set[str] = set()
    queries = [f"{city} 요가강사", f"{city} 필라테스강사", f"{city} 요가 개인레슨"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=_chromium_executable(),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1280, "height": 900})

        for q in queries:
            captured: list[dict] = []

            def handle_resp(response, _q=q):
                if response.status == 200:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct and any(
                            kw in response.url for kw in ["search", "talent", "tutor", "teacher"]
                        ):
                            captured.append(response.json())
                    except Exception:
                        pass

            page = ctx.new_page()
            page.on("response", handle_resp)

            try:
                page.goto(
                    f"https://www.taling.me/search?keyword={q}",
                    wait_until="load", timeout=30000,
                )
                page.wait_for_timeout(4000)

                # Parse captured API responses
                for data in captured:
                    if isinstance(data, list):
                        items = data
                    else:
                        items = (
                            data.get("items") or data.get("data") or
                            data.get("result", {}).get("items") or
                            data.get("list") or []
                        )
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        name = (
                            item.get("tutorName") or item.get("instructorName") or
                            item.get("sellerName") or
                            (item.get("tutor", {}).get("name") if isinstance(item.get("tutor"), dict) else None) or ""
                        )
                        bio = (
                            item.get("introduce") or item.get("description") or
                            item.get("talentTitle") or item.get("title") or ""
                        )
                        if not name:
                            continue
                        uid = hashlib.md5(f"taling_{name}_{city}".encode()).hexdigest()[:12]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        link = item.get("url") or item.get("link") or item.get("talentUrl") or ""
                        results.append({
                            "source":         "taling",
                            "source_id":      uid,
                            "full_name":      name,
                            "city":           city,
                            "bio":            bio,
                            "certifications": extract_certs(bio),
                            "specialties":    extract_specialties(bio + " " + name),
                            "website":        link if link.startswith("http") else f"https://www.taling.me{link}",
                            "price_per_hour": extract_price(str(item.get("price") or item.get("startPrice") or "")),
                            "data_source":    "taling",
                            "crawled_at":     datetime.now(timezone.utc).isoformat(),
                        })

                # DOM fallback
                if not captured:
                    cards = page.query_selector_all(
                        "[class*='TalentCard'],[class*='talentCard'],[class*='tutor-card'],"
                        "[data-talent-id],[data-tutor-id]"
                    )
                    for card in cards:
                        text = card.inner_text().strip()[:200]
                        if not text or not any(kw in text for kw in ["요가", "필라테스", "yoga"]):
                            continue
                        uid = hashlib.md5(f"taling_dom_{text}_{city}".encode()).hexdigest()[:12]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        results.append({
                            "source": "taling", "source_id": uid, "full_name": text[:60],
                            "city": city, "bio": text, "certifications": extract_certs(text),
                            "specialties": extract_specialties(text), "data_source": "taling",
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                log.info(f"  taling_pw '{q}': {len(results)} total")
            except Exception as e:
                log.warning(f"  taling_pw '{q}': {e}")
            finally:
                page.close()
                import time as _t; _t.sleep(delay + random.uniform(0, 0.5))

        ctx.close()
        browser.close()
    return results


def scrape_lessonall_instructors_playwright(city: str, delay: float) -> list[dict]:
    """Playwright version of lessonall instructor scraper."""
    from playwright.sync_api import sync_playwright

    results = []
    seen: set[str] = set()
    queries = [
        {"url": f"https://lessonall.com/teacher/list?q=요가&location={city}", "q": "요가"},
        {"url": f"https://lessonall.com/teacher/list?q=필라테스&location={city}", "q": "필라테스"},
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=_chromium_executable(),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1280, "height": 900})

        for entry in queries:
            captured: list[dict] = []

            def handle_resp(response):
                if response.status == 200:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct and any(
                            kw in response.url for kw in ["teacher", "lesson", "search"]
                        ):
                            captured.append(response.json())
                    except Exception:
                        pass

            page = ctx.new_page()
            page.on("response", handle_resp)

            try:
                page.goto(entry["url"], wait_until="load", timeout=30000)
                page.wait_for_timeout(3000)

                for data in captured:
                    if isinstance(data, list):
                        items = data
                    else:
                        items = data.get("items") or data.get("data") or data.get("list") or []
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        name = item.get("name") or item.get("teacherName") or item.get("nickName") or ""
                        subj = item.get("subject") or item.get("category") or ""
                        bio = " ".join([name, subj, item.get("introduce") or ""])
                        if not any(kw in bio for kw in ["요가", "필라테스", "yoga"]):
                            continue
                        uid = hashlib.md5(f"lessonall_{name}_{city}".encode()).hexdigest()[:12]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        link = item.get("url") or item.get("link") or ""
                        results.append({
                            "source": "lessonall", "source_id": uid, "full_name": name,
                            "city": city, "bio": bio.strip(),
                            "certifications": extract_certs(bio),
                            "specialties": extract_specialties(bio),
                            "website": link if link.startswith("http") else f"https://lessonall.com{link}",
                            "price_per_hour": extract_price(str(item.get("price") or "")),
                            "data_source": "lessonall",
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                # DOM fallback
                if not captured:
                    cards = page.query_selector_all(
                        ".teacher-card,.lesson-card,[class*='TeacherCard'],[class*='teacher-card']"
                    )
                    for card in cards:
                        text = card.inner_text().strip()[:200]
                        if not text or not any(kw in text for kw in ["요가", "필라테스", "yoga"]):
                            continue
                        uid = hashlib.md5(f"lessonall_dom_{text}_{city}".encode()).hexdigest()[:12]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        results.append({
                            "source": "lessonall", "source_id": uid, "full_name": text[:60],
                            "city": city, "bio": text, "certifications": extract_certs(text),
                            "specialties": extract_specialties(text), "data_source": "lessonall",
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                log.info(f"  lessonall_pw '{entry['q']}' {city}: {len(results)} total")
            except Exception as e:
                log.warning(f"  lessonall_pw {entry['q']}: {e}")
            finally:
                page.close()
                import time as _t; _t.sleep(delay + random.uniform(0, 0.5))

        ctx.close()
        browser.close()
    return results


def _parse_rating(text: str) -> float | None:
    m = re.search(r"(\d+\.?\d*)", text)
    if m:
        v = float(m.group(1))
        if 0.0 <= v <= 5.0:
            return v
    return None


# ── DB upsert ──────────────────────────────────────────────────────────────
UPSERT_SQL = """
INSERT INTO instructors
  (source, source_id, name, city, certifications, specialties,
   website, crawled_at)
VALUES
  (%(source)s, %(source_id)s, %(name)s, %(city)s,
   %(certifications)s, %(specialties)s, %(website)s, %(crawled_at)s)
ON CONFLICT (source, source_id) DO UPDATE SET
  name           = EXCLUDED.name,
  city           = COALESCE(EXCLUDED.city, instructors.city),
  certifications = COALESCE(EXCLUDED.certifications, instructors.certifications),
  specialties    = COALESCE(EXCLUDED.specialties, instructors.specialties),
  website        = COALESCE(EXCLUDED.website, instructors.website),
  crawled_at     = EXCLUDED.crawled_at
"""


def upsert_instructors(rows: list[dict], conn) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    normalised = []
    for r in rows:
        normalised.append({
            "source":         r.get("data_source") or r.get("source") or "unknown",
            "source_id":      r.get("source_id") or hashlib.md5(str(r).encode()).hexdigest()[:12],
            "name":           r.get("full_name") or r.get("name") or "",
            "city":           r.get("city"),
            "certifications": r.get("certifications") or [],
            "specialties":    r.get("specialties") or [],
            "website":        r.get("website"),
            "crawled_at":     r.get("crawled_at") or datetime.now(timezone.utc).isoformat(),
        })
    psycopg2.extras.execute_batch(cur, UPSERT_SQL, normalised, page_size=100)
    conn.commit()
    return len(normalised)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["taling", "lessonall", "kmong", "all"], default="all")
    ap.add_argument("--city",   help="Single city (default: all KR_CITIES)")
    ap.add_argument("--delay",  type=float, default=1.2, help="Base delay between requests (s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db-url",  default=DB_URL)
    args = ap.parse_args()

    cities = [args.city] if args.city else KR_CITIES
    conn = None if args.dry_run else psycopg2.connect(args.db_url)
    total = 0

    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        use_pw = _pw_available()
        if not use_pw:
            log.warning("Playwright not installed — falling back to static HTTP (likely 0 results for CSR sites)")

        for city in cities:
            city_rows: list[dict] = []

            if args.source in ("taling", "all"):
                city_rows += (
                    scrape_taling_instructors_playwright(city, args.delay) if use_pw
                    else scrape_taling_instructors(city, client, args.delay)
                )

            if args.source in ("lessonall", "all"):
                city_rows += (
                    scrape_lessonall_instructors_playwright(city, args.delay) if use_pw
                    else scrape_lessonall_instructors(city, client, args.delay)
                )

            if args.source in ("kmong", "all"):
                city_rows += scrape_kmong_instructors(city, client, args.delay)

            if city_rows:
                if not args.dry_run:
                    n = upsert_instructors(city_rows, conn)
                    total += n
                    log.info(f"  {city}: upserted {n} instructor profiles")
                else:
                    log.info(f"  [dry-run] {city}: {len(city_rows)} profiles found")

    log.info(f"Done — {total} instructor profiles upserted total")
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
