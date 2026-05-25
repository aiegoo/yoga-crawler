#!/usr/bin/env python3
"""
scrape_classes.py — Crawl GX timetables and private class listings.

Sources (in priority order)
───────────────────────────
1. Kakao Place pages          (1,508 studios with known place_url)
   - https://place.map.kakao.com/{source_id}
   - Extracts: class name, day/time, teacher, duration, price

2. Naver Smart Place API      (studios without kakao ID — discovered by name)
   - https://openapi.naver.com/v1/search/local.json?query={name}
   - Gets Naver place ID → detail page with reservation/timetable section

3. 탈잉 (taling.me)           (private yoga lessons marketplace)
   - https://taling.me/search/talent?keyword={city}+요가
   - Extracts: class title, style, price/hr, instructor name, area

4. 레슨올 (lessonall.com)     (private lesson booking platform)
   - https://lessonall.com/teacher/list?q=요가&location={city}
   - Extracts: class title, price, duration, instructor, area

All records are upserted into the `classes` table.
studio_id is resolved by matching name+address against studios table.

Usage
─────
  python scripts/scrape_classes.py                       # all sources
  python scripts/scrape_classes.py --source kakao        # Kakao Place only
  python scripts/scrape_classes.py --source naver        # Naver only
  python scripts/scrape_classes.py --source taling       # 탈잉 only
  python scripts/scrape_classes.py --source lessonall    # 레슨올 only
  python scripts/scrape_classes.py --city 서울 --limit 50
  python scripts/scrape_classes.py --dry-run

Environment
───────────
  CRAWL_DB_URL          postgresql://yogacrawl:yogacrawl@localhost/yogacrawl
  NAVER_CLIENT_ID       Naver Open API key
  NAVER_CLIENT_SECRET
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
from urllib.parse import urlparse

import httpx
import psycopg2
import psycopg2.extras
from bs4 import BeautifulSoup

try:
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore
    from io import BytesIO
    _OCR_AVAILABLE = True
except Exception:
    _OCR_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_URL = os.getenv("CRAWL_DB_URL", "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl")
NAVER_ID     = os.getenv("NAVER_CLIENT_ID", "")


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
    return None  # let playwright find its own
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# GX class keywords to confirm a listing is a yoga/fitness class
GX_KEYWORDS = [
    "요가", "yoga", "필라테스", "pilates", "명상", "meditation",
    "핫요가", "hot yoga", "아쉬탕가", "빈야사", "하타", "이음",
    "코어", "HIIT", "줌바", "바디펌프", "스피닝", "복싱",
    "크로스핏", "TRX", "GX", "그룹", "에어로빅",
]

KR_CITIES = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산",
    "수원", "고양", "창원", "성남", "청주", "전주", "안산",
    "안양", "남양주", "화성", "평택", "용인",
]


def _is_schedule_image_text(raw_text: str) -> bool:
    low = (raw_text or "").lower()
    keywords = [
        "시간표", "스케줄", "timetable", "schedule", "class schedule",
        "월", "화", "수", "목", "금", "토", "일", "monday", "tuesday",
    ]
    has_time = bool(re.search(r"\d{1,2}:\d{2}", low))
    return has_time and any(k in low for k in keywords)


def _ocr_raw_text(image_bytes: bytes) -> str:
    if not _OCR_AVAILABLE:
        return ""
    img = Image.open(BytesIO(image_bytes))
    return pytesseract.image_to_string(img, lang="kor+eng")


def _normalize_day(tok: str) -> str | None:
    t = tok.strip().lower()
    mapping = {
        "월": "Mon", "화": "Tue", "수": "Wed", "목": "Thu", "금": "Fri", "토": "Sat", "일": "Sun",
        "mon": "Mon", "monday": "Mon", "tue": "Tue", "tuesday": "Tue", "wed": "Wed", "wednesday": "Wed",
        "thu": "Thu", "thursday": "Thu", "fri": "Fri", "friday": "Fri", "sat": "Sat", "saturday": "Sat",
        "sun": "Sun", "sunday": "Sun",
    }
    return mapping.get(t)


def _parse_timetable_from_text(raw_text: str) -> dict | None:
    """Parse OCR text lines into a simple timetable shape.

    Expected forms include:
      월 화 수 목 금
      09:00~10:00 하타 빈야사 ...
    """
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    if not lines:
        return None

    day_header: list[str] = []
    time_re = re.compile(r"\d{1,2}:\d{2}")
    slots: list[dict[str, Any]] = []

    for line in lines:
        pieces = [p for p in re.split(r"[\s|/\\]+", line) if p]
        day_hits = [_normalize_day(p) for p in pieces]
        day_hits = [d for d in day_hits if d]
        if len(day_hits) >= 3 and not time_re.search(line):
            day_header = day_hits
            continue

        if not time_re.search(line):
            continue

        m = re.search(r"(\d{1,2}:\d{2})\s*[~\-–]\s*(\d{1,2}:\d{2})", line)
        if not m:
            continue
        start, end = m.group(1), m.group(2)
        rest = line[m.end():].strip()
        if not rest or not day_header:
            continue
        cells = [c.strip() for c in re.split(r"\s{2,}|\t|\|", rest) if c.strip()]
        if not cells:
            continue
        class_map = {day: cells[i] for i, day in enumerate(day_header) if i < len(cells)}
        if class_map:
            slots.append({
                "time": f"{start}-{end}",
                "start": start,
                "end": end,
                "classes": class_map,
            })

    if not slots:
        return None
    return {"days": day_header, "slots": slots}


def _extract_kakao_candidate_image_urls(soup: BeautifulSoup, html_text: str) -> list[str]:
    urls: list[str] = []

    for meta in soup.select("meta[property='og:image']"):
        content = (meta.get("content") or "").strip()
        if content.startswith("http"):
            urls.append(content)

    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src.startswith("http"):
            continue
        cls = " ".join(img.get("class") or []).lower()
        alt = (img.get("alt") or "").lower()
        if any(k in cls for k in ("photo", "news", "thumb", "img")) or any(k in alt for k in ("시간표", "스케줄", "schedule", "timetable")):
            urls.append(src)

    for pattern in [
        r'https?://[^"\\\']+\\.(?:jpg|jpeg|png|webp)',
        r'https?:\\/\\/[^"\\\']+\\.(?:jpg|jpeg|png|webp)',
    ]:
        for m in re.finditer(pattern, html_text, re.I):
            urls.append(m.group(0).replace("\\/", "/"))

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        host = (urlparse(u).netloc or "").lower()
        if any(h in host for h in ("kakaocdn.net", "kakao.com", "daumcdn.net", "dn-")):
            out.append(u)
    return out


def scrape_kakao_schedule_images(source_id: str, studio_id: int, studio_name: str,
                                 client: httpx.Client, delay: float) -> list[dict[str, Any]]:
    """OCR schedule-like images from a Kakao place page.

    Emits class rows with source `kakao_place_image`.
    """
    if not _OCR_AVAILABLE:
        return []

    url = f"https://place.map.kakao.com/{source_id}"
    rows: list[dict[str, Any]] = []
    try:
        r = client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        image_urls = _extract_kakao_candidate_image_urls(soup, r.text)
        if not image_urls:
            return []

        for idx, img_url in enumerate(image_urls[:6]):
            try:
                img = client.get(img_url, timeout=15)
                img.raise_for_status()
                raw = _ocr_raw_text(img.content)
                if not _is_schedule_image_text(raw):
                    continue

                timetable = _parse_timetable_from_text(raw)
                if not timetable or not timetable.get("slots"):
                    continue

                class_occ: dict[str, list[dict[str, str]]] = {}
                for slot in timetable.get("slots", []):
                    for day, class_name in (slot.get("classes") or {}).items():
                        title = (class_name or "").strip()
                        if len(title) < 2:
                            continue
                        if re.search(r"\bbreak\b|휴식|휴게", title, re.I):
                            continue
                        class_occ.setdefault(title, []).append({
                            "day": day,
                            "start": slot.get("start", ""),
                            "end": slot.get("end", ""),
                        })

                for title, occ in class_occ.items():
                    sid = hashlib.md5(f"kakao_img_{source_id}_{idx}_{title}".encode()).hexdigest()[:16]
                    rows.append({
                        "source": "kakao_place_image",
                        "source_id": sid,
                        "studio_id": studio_id,
                        "title": title,
                        "style": _infer_style(title),
                        "schedule": {
                            "type": "weekly_timetable",
                            "source_page": url,
                            "source_image_url": img_url,
                            "source_image_index": idx,
                            "days": timetable.get("days", []),
                            "occurrences": occ,
                            "ocr_preview": raw[:600],
                        },
                        "crawled_at": datetime.now(timezone.utc).isoformat(),
                    })
            except Exception as e:
                log.debug(f"  kakao image parse failed ({studio_name}) {img_url}: {e}")
                continue
    except Exception as e:
        log.debug(f"  kakao image schedule scrape failed {source_id}: {e}")
    finally:
        time.sleep(delay + random.uniform(0, 0.2))

    if rows:
        log.info(f"  kakao image OCR {source_id} ({studio_name}): {len(rows)} classes")
    return rows


# ── Kakao Place scraper ────────────────────────────────────────────────────
def scrape_kakao_place(source_id: str, studio_id: int, studio_name: str,
                       client: httpx.Client, delay: float) -> list[dict]:
    """Scrape GX timetable from a Kakao Place page."""
    url = f"https://place.map.kakao.com/{source_id}"
    classes = []
    try:
        r = client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Kakao Place stores class/program info in a JSON blob in the page
        # Look for script tags containing "programList" or "classList"
        for script in soup.find_all("script"):
            text = script.string or ""
            if "programList" in text or "classList" in text or "수업" in text:
                # Try extracting JSON from __DATA__ or window.__INITIAL_STATE__
                m = re.search(r'"programList"\s*:\s*(\[.*?\])', text, re.S)
                if m:
                    try:
                        programs = json.loads(m.group(1))
                        for p in programs:
                            classes.append(_kakao_program_to_class(p, studio_id, source_id))
                    except json.JSONDecodeError:
                        pass

        # Also look for structured schedule table in HTML
        tbl = soup.find("table", class_=re.compile(r"schedule|timetable|program", re.I))
        if tbl:
            for row in tbl.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cells) >= 2 and any(kw in " ".join(cells) for kw in GX_KEYWORDS):
                    classes.append({
                        "source": "kakao_place",
                        "source_id": hashlib.md5(f"kakao_{source_id}_{cells[0]}".encode()).hexdigest()[:16],
                        "studio_id": studio_id,
                        "title": cells[0],
                        "schedule": {"raw": cells},
                        "crawled_at": datetime.now(timezone.utc).isoformat(),
                    })

        log.debug(f"  kakao {source_id} ({studio_name}): {len(classes)} classes found")
    except Exception as e:
        log.warning(f"  kakao {source_id}: {e}")
    finally:
        time.sleep(delay + random.uniform(0, 0.3))
    return classes


def _kakao_program_to_class(p: dict, studio_id: int, source_id: str) -> dict:
    title = p.get("programName") or p.get("name") or p.get("title") or ""
    return {
        "source": "kakao_place",
        "source_id": hashlib.md5(f"kakao_{source_id}_{title}".encode()).hexdigest()[:16],
        "studio_id": studio_id,
        "title": title,
        "style": _infer_style(title),
        "duration_min": p.get("duration") or p.get("durationMin"),
        "price": _parse_price(p.get("price") or p.get("amount")),
        "schedule": {
            "days": p.get("days") or p.get("dayOfWeek"),
            "time": p.get("startTime") or p.get("time"),
            "raw": p,
        },
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Naver Smart Place scraper ──────────────────────────────────────────────
def search_naver_place(name: str, client: httpx.Client) -> str | None:
    """Search Naver Local API for a place ID by name, returns placeId string."""
    if not NAVER_ID:
        return None
    try:
        r = client.get(
            "https://openapi.naver.com/v1/search/local.json",
            params={"query": name, "display": 1},
            headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET},
            timeout=8,
        )
        items = r.json().get("items", [])
        if items:
            link = items[0].get("link", "")
            m = re.search(r"place/(\d+)", link)
            if m:
                return m.group(1)
    except Exception as e:
        log.debug(f"Naver place search failed for '{name}': {e}")
    return None


def scrape_naver_timetable(place_id: str, studio_id: int,
                           client: httpx.Client, delay: float) -> list[dict]:
    """Scrape class schedule from Naver Smart Place detail."""
    url = f"https://place.map.naver.com/place/v1/detail?placeId={place_id}"
    classes = []
    try:
        r = client.get(url, timeout=12,
                       headers={"Referer": "https://map.naver.com/"})
        data = r.json()
        programs = (
            data.get("result", {}).get("programList")
            or data.get("result", {}).get("classList")
            or []
        )
        for p in programs:
            title = p.get("name") or p.get("programName") or ""
            if not title:
                continue
            classes.append({
                "source": "naver_place",
                "source_id": hashlib.md5(f"naver_{place_id}_{title}".encode()).hexdigest()[:16],
                "studio_id": studio_id,
                "title": title,
                "style": _infer_style(title),
                "duration_min": p.get("durationMin") or p.get("duration"),
                "price": _parse_price(p.get("price") or p.get("amount")),
                "schedule": {
                    "days": p.get("days"),
                    "time": p.get("startTime"),
                    "capacity": p.get("capacity"),
                    "raw": p,
                },
                "crawled_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        log.warning(f"  naver place {place_id}: {e}")
    finally:
        time.sleep(delay + random.uniform(0, 0.3))
    return classes


# ── 탈잉 (taling.me) scraper ───────────────────────────────────────────────
def scrape_taling(city: str, client: httpx.Client, delay: float) -> list[dict]:
    """Scrape yoga/fitness private class listings from taling.me."""
    classes = []
    queries = [f"{city} 요가", f"{city} 필라테스", f"{city} 요가 개인레슨"]
    seen = set()

    for q in queries:
        try:
            r = client.get(
                "https://www.taling.me/search",
                params={"keyword": q, "page": 1},
                headers={"User-Agent": UA},
                timeout=12,
                follow_redirects=True,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".talent-item, .card-talent, [data-talent-id], article.item")

            for card in cards:
                title = (
                    card.select_one(".talent-title, .title, h3, h4") or
                    card.select_one("[class*='title']")
                )
                title_text = title.get_text(strip=True) if title else ""
                if not title_text or not any(kw in title_text for kw in GX_KEYWORDS):
                    continue

                price_el = card.select_one(".price, [class*='price']")
                instructor_el = card.select_one(".tutor-name, .instructor, [class*='tutor']")
                link_el = card.select_one("a[href]")

                uid = hashlib.md5(f"taling_{q}_{title_text}".encode()).hexdigest()[:16]
                if uid in seen:
                    continue
                seen.add(uid)

                classes.append({
                    "source": "taling",
                    "source_id": uid,
                    "title": title_text,
                    "style": _infer_style(title_text),
                    "price": _parse_price(price_el.get_text() if price_el else ""),
                    "description": city,  # store city in description for now
                    "schedule": {
                        "type": "private",
                        "city": city,
                        "platform_url": "https://taling.me" + (link_el["href"] if link_el else ""),
                        "instructor": instructor_el.get_text(strip=True) if instructor_el else None,
                    },
                    "crawled_at": datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"  taling '{q}': {len(classes)} classes so far")
        except Exception as e:
            log.warning(f"  taling '{q}': {e}")
        finally:
            time.sleep(delay + random.uniform(0, 0.5))

    return classes


# ── 레슨올 (lessonall.com) scraper ─────────────────────────────────────────
def scrape_lessonall(city: str, client: httpx.Client, delay: float) -> list[dict]:
    """Scrape yoga lesson listings from lessonall.com."""
    classes = []
    seen = set()
    params_list = [
        {"q": "요가", "location": city},
        {"q": "필라테스", "location": city},
        {"q": "요가 개인레슨", "location": city},
    ]

    for params in params_list:
        try:
            r = client.get(
                "https://lessonall.com/teacher/list",
                params=params,
                headers={"User-Agent": UA},
                timeout=12,
                follow_redirects=True,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".teacher-card, .item, article, .lesson-item, [class*='teacher']")

            for card in cards:
                title_el = card.select_one("h2, h3, h4, .name, .title, [class*='name']")
                title_text = title_el.get_text(strip=True) if title_el else ""
                if not title_text:
                    continue

                subject_el = card.select_one(".subject, .category, [class*='subject']")
                price_el   = card.select_one(".price, [class*='price']")
                area_el    = card.select_one(".area, .location, [class*='area']")
                link_el    = card.select_one("a[href]")

                uid = hashlib.md5(f"lessonall_{params['q']}_{title_text}_{city}".encode()).hexdigest()[:16]
                if uid in seen:
                    continue
                seen.add(uid)

                classes.append({
                    "source": "lessonall",
                    "source_id": uid,
                    "title": title_text,
                    "style": _infer_style(subject_el.get_text() if subject_el else title_text),
                    "price": _parse_price(price_el.get_text() if price_el else ""),
                    "schedule": {
                        "type": "private",
                        "city": city,
                        "area": area_el.get_text(strip=True) if area_el else city,
                        "platform_url": "https://lessonall.com" + (link_el["href"] if link_el else ""),
                    },
                    "crawled_at": datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"  lessonall '{params['q']}' {city}: {len(classes)} so far")
        except Exception as e:
            log.warning(f"  lessonall {params}: {e}")
        finally:
            time.sleep(delay + random.uniform(0, 0.5))

    return classes


# ── Playwright helpers ────────────────────────────────────────────────────
def _pw_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def scrape_taling_playwright(city: str, delay: float) -> list[dict]:
    """Scrape taling.me using Playwright — intercepts internal API calls."""
    from playwright.sync_api import sync_playwright, Route, Request

    classes = []
    seen: set[str] = set()
    queries = [f"{city} 요가", f"{city} 필라테스", f"{city} 요가 개인레슨"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=_chromium_executable(),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            viewport={"width": 1280, "height": 900},
        )

        for q in queries:
            captured: list[dict] = []

            def handle_response(response):
                url = response.url
                # Capture any JSON responses that look like search results
                if response.status == 200 and any(
                    kw in url for kw in ["search", "talent", "tutor", "class", "lesson"]
                ):
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            data = response.json()
                            captured.append({"url": url, "data": data})
                    except Exception:
                        pass

            page = ctx.new_page()
            page.on("response", handle_response)

            try:
                page.goto(
                    f"https://www.taling.me/search?keyword={q}",
                    wait_until="load",
                    timeout=30000,
                )
                # Give JS time to fire API calls after initial load
                page.wait_for_timeout(4000)

                # Parse from captured API responses first
                for cap in captured:
                    data = cap["data"]
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
                        title = (
                            item.get("title") or item.get("name") or
                            item.get("talentTitle") or item.get("className") or ""
                        )
                        if not title or not any(kw in title for kw in GX_KEYWORDS):
                            continue
                        uid = hashlib.md5(f"taling_pw_{title}_{city}".encode()).hexdigest()[:16]
                        if uid in seen:
                            continue
                        seen.add(uid)

                        instructor = (
                            item.get("tutorName") or item.get("instructorName") or
                            item.get("tutor", {}).get("name") if isinstance(item.get("tutor"), dict) else None
                        )
                        price_raw = item.get("price") or item.get("amount") or item.get("startPrice")
                        link = item.get("url") or item.get("link") or item.get("talentUrl") or ""
                        classes.append({
                            "source": "taling",
                            "source_id": uid,
                            "title": title,
                            "style": _infer_style(title),
                            "price": _parse_price(str(price_raw)) if price_raw else None,
                            "schedule": {
                                "type": "private",
                                "city": city,
                                "platform_url": link if link.startswith("http") else f"https://www.taling.me{link}",
                                "instructor": instructor,
                            },
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                # Fallback: parse rendered DOM if API capture was empty
                if not captured:
                    page.wait_for_selector(
                        "[class*='talent'],[class*='tutor'],[class*='card'],[class*='item']",
                        timeout=10000,
                    )
                    cards = page.query_selector_all(
                        "[class*='TalentCard'],[class*='talentCard'],[class*='talent-card'],"
                        "[class*='tutor-card'],[data-talent-id],[data-tutor-id]"
                    )
                    for card in cards:
                        title = card.inner_text().strip()[:120]
                        if not title or not any(kw in title for kw in GX_KEYWORDS):
                            continue
                        uid = hashlib.md5(f"taling_dom_{title}_{city}".encode()).hexdigest()[:16]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        classes.append({
                            "source": "taling",
                            "source_id": uid,
                            "title": title,
                            "style": _infer_style(title),
                            "schedule": {"type": "private", "city": city},
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                log.info(f"  taling_pw '{q}': {len(classes)} total")
            except Exception as e:
                log.warning(f"  taling_pw '{q}': {e}")
            finally:
                page.close()
                time.sleep(delay + random.uniform(0, 0.5))

        ctx.close()
        browser.close()

    return classes


def scrape_lessonall_playwright(city: str, delay: float) -> list[dict]:
    """Scrape lessonall.com using Playwright — intercepts API + parses DOM."""
    from playwright.sync_api import sync_playwright

    classes = []
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

            def handle_resp(response, _entry=entry):
                if response.status == 200:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct and any(
                            kw in response.url for kw in ["teacher", "lesson", "search", "list"]
                        ):
                            captured.append(response.json())
                    except Exception:
                        pass

            page = ctx.new_page()
            page.on("response", handle_resp)

            try:
                page.goto(entry["url"], wait_until="load", timeout=30000)
                page.wait_for_timeout(3000)
                time.sleep(1)

                # Try captured JSON first
                for data in captured:
                    if isinstance(data, list):
                        items = data
                    else:
                        items = data.get("items") or data.get("data") or data.get("list") or []
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        name = item.get("name") or item.get("teacherName") or item.get("title") or ""
                        subj = item.get("subject") or item.get("category") or ""
                        full = f"{name} {subj}"
                        if not any(kw in full for kw in GX_KEYWORDS):
                            continue
                        uid = hashlib.md5(f"lessonall_{name}_{city}".encode()).hexdigest()[:16]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        link = item.get("url") or item.get("link") or ""
                        classes.append({
                            "source": "lessonall",
                            "source_id": uid,
                            "title": f"{name} ({subj})" if subj else name,
                            "style": _infer_style(full),
                            "price": _parse_price(str(item.get("price", ""))),
                            "schedule": {
                                "type": "private",
                                "city": city,
                                "platform_url": link if link.startswith("http") else f"https://lessonall.com{link}",
                            },
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                # DOM fallback
                if not captured:
                    cards = page.query_selector_all(
                        ".teacher-card,.lesson-card,[class*='TeacherCard'],[class*='teacher-card']"
                    )
                    for card in cards:
                        title = card.inner_text().strip()[:120]
                        if not title or not any(kw in title for kw in GX_KEYWORDS):
                            continue
                        uid = hashlib.md5(f"lessonall_dom_{title}_{city}".encode()).hexdigest()[:16]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        classes.append({
                            "source": "lessonall",
                            "source_id": uid,
                            "title": title,
                            "style": _infer_style(title),
                            "schedule": {"type": "private", "city": city},
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        })

                log.info(f"  lessonall_pw '{entry['q']}' {city}: {len(classes)} total")
            except Exception as e:
                log.warning(f"  lessonall_pw {entry}: {e}")
            finally:
                page.close()
                time.sleep(delay + random.uniform(0, 0.5))

        ctx.close()
        browser.close()

    return classes


# ── Helpers ────────────────────────────────────────────────────────────────
STYLE_MAP = {
    "하타": "hatha", "아쉬탕가": "ashtanga", "빈야사": "vinyasa",
    "핫요가": "hot_yoga", "음요가": "yin", "음": "yin",
    "쿤달리니": "kundalini", "명상": "meditation",
    "필라테스": "pilates", "코어": "core", "GX": "gx",
    "HIIT": "hiit", "줌바": "zumba", "스피닝": "spinning",
    "복싱": "boxing", "크로스핏": "crossfit",
}


def _infer_style(text: str) -> str | None:
    for kr, en in STYLE_MAP.items():
        if kr in text:
            return en
    if "요가" in text or "yoga" in text.lower():
        return "yoga"
    if "필라테스" in text or "pilates" in text.lower():
        return "pilates"
    return None


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    nums = re.sub(r"[^\d]", "", text)
    if nums:
        val = int(nums)
        # sanity: 1,000 ~ 1,000,000 KRW
        if 1000 <= val <= 1_000_000:
            return float(val)
    return None


# ── DB upsert ──────────────────────────────────────────────────────────────
UPSERT_SQL = """
INSERT INTO classes
  (source, source_id, studio_id, instructor_id, title, style,
   duration_min, price, schedule, crawled_at)
VALUES
  (%(source)s, %(source_id)s, %(studio_id)s, %(instructor_id)s,
   %(title)s, %(style)s, %(duration_min)s, %(price)s,
   %(schedule)s::jsonb, %(crawled_at)s)
ON CONFLICT (source, source_id) DO UPDATE SET
  title        = EXCLUDED.title,
  style        = COALESCE(EXCLUDED.style, classes.style),
  duration_min = COALESCE(EXCLUDED.duration_min, classes.duration_min),
  price        = COALESCE(EXCLUDED.price, classes.price),
  schedule     = EXCLUDED.schedule,
  crawled_at   = EXCLUDED.crawled_at
"""


def upsert_classes(rows: list[dict], conn) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    normalised = []
    for r in rows:
        normalised.append({
            "source":       r.get("source", "unknown"),
            "source_id":    r.get("source_id") or hashlib.md5(str(r).encode()).hexdigest()[:16],
            "studio_id":    r.get("studio_id"),
            "instructor_id": r.get("instructor_id"),
            "title":        r.get("title", ""),
            "style":        r.get("style"),
            "duration_min": r.get("duration_min"),
            "price":        r.get("price"),
            "schedule":     json.dumps(r.get("schedule") or {}),
            "crawled_at":   r.get("crawled_at") or datetime.now(timezone.utc).isoformat(),
        })
    psycopg2.extras.execute_batch(cur, UPSERT_SQL, normalised, page_size=100)
    conn.commit()
    return len(normalised)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["kakao", "naver", "taling", "lessonall", "all"], default="all")
    ap.add_argument("--city", help="Single city to target (default: all KR_CITIES)")
    ap.add_argument("--limit", type=int, default=0, help="Max studios to process (0 = all)")
    ap.add_argument("--delay", type=float, default=1.0, help="Base delay between requests (s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--kakao-image-ocr",
        action="store_true",
        help="Enable Kakao schedule image OCR fallback",
    )
    ap.add_argument("--db-url", default=DB_URL)
    args = ap.parse_args()

    cities = [args.city] if args.city else KR_CITIES

    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    total_upserted = 0

    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:

        # ── Source 1: Kakao Place ─────────────────────────────────────────
        if args.source in ("kakao", "all"):
            log.info("=== Kakao Place timetables ===")
            if args.kakao_image_ocr and not _OCR_AVAILABLE:
                log.warning("Kakao image OCR requested but pytesseract/Pillow are unavailable")
            cur.execute("""
                SELECT id, source_id, name FROM studios
                WHERE source='kakao' AND source_id IS NOT NULL
                ORDER BY id
                LIMIT %s
            """, (args.limit or 99999,))
            studios = cur.fetchall()
            log.info(f"Processing {len(studios)} kakao studios…")

            for s in studios:
                rows = scrape_kakao_place(s["source_id"], s["id"], s["name"], client, args.delay)
                if args.kakao_image_ocr and (not rows or len(rows) < 2):
                    rows.extend(
                        scrape_kakao_schedule_images(
                            s["source_id"],
                            s["id"],
                            s["name"],
                            client,
                            args.delay,
                        )
                    )
                if rows and not args.dry_run:
                    total_upserted += upsert_classes(rows, conn)
                elif rows:
                    log.info(f"  [dry-run] {s['name']}: {len(rows)} classes")

        # ── Source 2: Naver Smart Place ───────────────────────────────────
        if args.source in ("naver", "all") and NAVER_ID:
            log.info("=== Naver Smart Place timetables ===")
            cur.execute("""
                SELECT id, name FROM studios
                WHERE source IN ('gov_sangga','naver')
                  AND id NOT IN (SELECT DISTINCT studio_id FROM classes WHERE studio_id IS NOT NULL)
                ORDER BY id
                LIMIT %s
            """, (args.limit or 500,))
            studios = cur.fetchall()
            log.info(f"Discovering Naver place IDs for {len(studios)} studios…")

            for s in studios:
                place_id = search_naver_place(s["name"], client)
                if place_id:
                    rows = scrape_naver_timetable(place_id, s["id"], client, args.delay)
                    if rows and not args.dry_run:
                        total_upserted += upsert_classes(rows, conn)
                time.sleep(args.delay)

        # ── Source 3: 탈잉 ────────────────────────────────────────────────
        if args.source in ("taling", "all"):
            log.info("=== 탈잉 (taling.me) private classes ===")
            use_pw = _pw_available()
            if not use_pw:
                log.warning("  Playwright not installed — falling back to static HTTP (likely 0 results)")
            for city in cities:
                rows = scrape_taling_playwright(city, args.delay) if use_pw else scrape_taling(city, client, args.delay)
                if rows and not args.dry_run:
                    total_upserted += upsert_classes(rows, conn)
                elif rows:
                    log.info(f"  [dry-run] {city}: {len(rows)} classes")

        # ── Source 4: 레슨올 ──────────────────────────────────────────────
        if args.source in ("lessonall", "all"):
            log.info("=== 레슨올 (lessonall.com) private lessons ===")
            use_pw = _pw_available()
            if not use_pw:
                log.warning("  Playwright not installed — falling back to static HTTP (likely 0 results)")
            for city in cities:
                rows = scrape_lessonall_playwright(city, args.delay) if use_pw else scrape_lessonall(city, client, args.delay)
                if rows and not args.dry_run:
                    total_upserted += upsert_classes(rows, conn)
                elif rows:
                    log.info(f"  [dry-run] {city}: {len(rows)} classes")

    log.info(f"Done — upserted {total_upserted} class records total")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
