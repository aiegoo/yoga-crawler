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

Also supports a canonical profile-site seed, so a first-party instructor page can
be ingested and used to find similar instructors from the scraped sources.

Usage
─────
  python scripts/scrape_instructor_profiles.py                   # all sources
  python scripts/scrape_instructor_profiles.py --source taling
  python scripts/scrape_instructor_profiles.py --source lessonall
  python scripts/scrape_instructor_profiles.py --source kmong
    python scripts/scrape_instructor_profiles.py --source seed --seed-url https://elbee.yogaman.club --dry-run
    python scripts/scrape_instructor_profiles.py --source all --seed-url https://elbee.yogaman.club --similar-to-seed 10
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
from pathlib import Path
import re
import time
import random
from collections.abc import Iterable
from urllib.parse import urlparse
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
HEADING_TAG_RE = re.compile(r"^h[1-6]$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?82[-\s]?)?0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}")
INSTAGRAM_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.I)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[가-힣]{2,}")
STOPWORDS = {
    "the", "and", "with", "from", "that", "this", "your", "have", "will", "into",
    "yoga", "teacher", "instructor", "class", "classes", "profile", "studio", "center",
    "요가", "강사", "프로필", "수업", "센터", "지도", "운영", "경력", "자격", "사항",
    "전문", "분야", "연락처", "자기소개", "철학", "가능", "시간", "활동", "지역",
}
LINEAGE_KEYWORDS = {
    "ashtanga", "hatha", "vinyasa", "bikram", "iyengar", "kundalini", "yin", "therapy",
    "meditation", "prenatal", "postnatal", "pilates", "hot_yoga",
}
AREA_TO_CITY = {
    "일산": "고양",
    "고양": "고양",
    "홍대": "서울",
    "합정": "서울",
    "상수": "서울",
    "울산": "울산",
    "구미": "구미",
}


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
    (r"EUYA",             lambda m: "EUYA"),
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


def extract_emails(text: str) -> list[str]:
    return sorted(dict.fromkeys(m.group(0) for m in EMAIL_RE.finditer(text)))


def extract_phones(text: str) -> list[str]:
    phones = []
    for m in PHONE_RE.finditer(text):
        phone = re.sub(r"\s+", "", m.group(0))
        phones.append(phone)
    return sorted(dict.fromkeys(phones))


def _extract_instagram_handle(value: str | None) -> str | None:
    if not value:
        return None
    m = INSTAGRAM_URL_RE.search(value)
    if m:
        return m.group(1).lstrip("@").rstrip("/").lower()
    if value.startswith("@"):
        return value.lstrip("@").lower()
    return None


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _normalise_city(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    for area, city in AREA_TO_CITY.items():
        if area in value:
            return city
    for city in KR_CITIES:
        if city in value:
            return city
    return value[:80] if value else None


def _tokenize(texts: Iterable[str | None]) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in WORD_RE.finditer(text.lower()):
            token = match.group(0)
            if token not in STOPWORDS:
                tokens.add(token)
    return tokens


def _extract_section_map(soup: BeautifulSoup) -> dict[str, str]:
    sections: dict[str, str] = {}
    for heading in soup.find_all(HEADING_TAG_RE):
        title = heading.get_text(" ", strip=True)
        if not title:
            continue
        chunks: list[str] = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) and HEADING_TAG_RE.match(sibling.name):
                break
            if getattr(sibling, "get_text", None):
                text = sibling.get_text(" ", strip=True)
                if text:
                    chunks.append(text)
        if chunks:
            sections[title] = "\n".join(chunks)
    return sections


def _pick_section(sections: dict[str, str], *needles: str) -> str:
    lowered = [needle.lower() for needle in needles]
    for key, value in sections.items():
        key_low = key.lower()
        if any(needle in key_low for needle in lowered):
            return value
    return ""


def _collect_links(node: BeautifulSoup | Any) -> dict[str, str]:
    links: dict[str, str] = {}
    for anchor in node.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        text = anchor.get_text(" ", strip=True)
        if not href:
            continue
        key = href
        if "instagram.com/" in href.lower():
            key = "instagram"
        elif href.lower().startswith("mailto:"):
            key = "email"
        elif href.lower().startswith("tel:"):
            key = "phone"
        elif "notion.so" in href.lower():
            key = "notion"
        elif href.lower().endswith(".pdf"):
            key = "profile_pdf"
        elif "docs.google.com/presentation" in href.lower():
            key = "slides"
        elif "github.com/" in href.lower():
            key = f"github_{_slugify(text or href)[0:40] or 'asset'}"
        links.setdefault(key, href)
    return links


def _find_text_block(soup: BeautifulSoup, label: str) -> str:
    label = label.strip()
    for el in soup.find_all(["li", "div", "p", "span", "strong"]):
        text = el.get_text(" ", strip=True)
        if label in text:
            return text
    return ""


def _extract_labeled_value(soup: BeautifulSoup, label: str) -> str:
    for strong in soup.find_all(["strong", "b"]):
        strong_text = strong.get_text(" ", strip=True)
        if label not in strong_text:
            continue
        container = strong.parent
        if not getattr(container, "get_text", None):
            continue
        text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
        if label not in text:
            continue
        value = text.split(label, 1)[1].strip(" :\u00a0·")
        value = re.split(r"(?:활동 지역|수업 언어|수업 가능 시간|연락처)", value)[0].strip(" :\u00a0·")
        if value:
            return value
    return ""


def _parse_activity_area(text: str) -> str:
    text = re.sub(r".*활동\s*지역", "", text).strip(" :\u00a0")
    text = re.split(r"(?:수업 언어|수업 가능 시간|연락처)", text)[0].strip(" :\u00a0·")
    return text[:120]


def _section_node_by_title(soup: BeautifulSoup, *needles: str):
    lowered = [needle.lower() for needle in needles]
    for heading in soup.find_all(HEADING_TAG_RE):
        title = heading.get_text(" ", strip=True).lower()
        if any(needle in title for needle in lowered):
            return heading.parent if getattr(heading, "parent", None) else heading
    return None


def _scrape_profile_document(html: str, source_ref: str, canonical_url: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    sections = _extract_section_map(soup)
    links = _collect_links(soup)
    body_text = soup.get_text("\n", strip=True)

    parsed = urlparse(canonical_url or source_ref)
    domain = parsed.netloc.lower() if parsed.netloc else Path(source_ref).name.lower()
    page_name = soup.find("h1")
    full_name = page_name.get_text(" ", strip=True) if page_name else domain
    intro = _pick_section(sections, "자기소개")
    teaching_style = _pick_section(sections, "지도 스타일", "철학")
    operations = _pick_section(sections, "센터 운영 경험")
    career = _pick_section(sections, "경력사항")
    certifications_text = _pick_section(sections, "자격사항")
    contact_text = _pick_section(sections, "연락처")
    specialties_text = _pick_section(sections, "전문 분야")
    activity_line = _extract_labeled_value(soup, "활동 지역") or _find_text_block(soup, "활동 지역")
    area_text = _parse_activity_area(activity_line) if activity_line else ""
    city = _normalise_city(area_text or contact_text or body_text)
    badge_text = " ".join(el.get_text(" ", strip=True) for el in soup.select(".badge, .tag"))
    specialties = extract_specialties(" ".join([body_text, certifications_text, teaching_style, specialties_text, badge_text]))
    lineage = [tag for tag in specialties if tag in LINEAGE_KEYWORDS]
    source_id = domain.replace(".", "_")
    contact_node = _section_node_by_title(soup, "연락처")
    contact_links = _collect_links(contact_node) if contact_node is not None else {}
    instagram = _extract_instagram_handle(contact_links.get("instagram"))
    emails = extract_emails("\n".join([body_text, contact_text]))
    phones = extract_phones("\n".join([body_text, contact_text]))
    bio_text = "\n\n".join(part for part in [intro, teaching_style, operations, career] if part)
    availability_line = _extract_labeled_value(soup, "수업 가능 시간") or _find_text_block(soup, "수업 가능 시간")
    language_line = _extract_labeled_value(soup, "수업 언어") or _find_text_block(soup, "수업 언어")

    return [{
        "source": "profile_site",
        "source_id": source_id,
        "full_name": full_name,
        "city": city,
        "bio": intro or bio_text or body_text[:1000],
        "bio_text": bio_text or body_text[:4000],
        "certifications": extract_certs("\n".join([certifications_text, body_text])),
        "specialties": specialties,
        "lineage": lineage,
        "website": canonical_url or (source_ref if parsed.scheme in {"http", "https"} else None),
        "instagram": instagram,
        "blog_url": links.get("notion"),
        "aliases": [],
        "data_source": "profile_site",
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "rag_payload": {
            "profile_domain": domain,
            "profile_sections": sections,
            "contact": {
                "emails": emails,
                "phones": phones,
            },
            "links": contact_links or links,
            "activity_area": area_text or None,
            "availability": availability_line or None,
            "languages": language_line or None,
            "source_ref": source_ref,
        },
    }]


def scrape_profile_site(seed_url: str, client: httpx.Client) -> list[dict]:
    """Scrape a first-party instructor profile page and map it to instructors schema."""
    r = client.get(seed_url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return _scrape_profile_document(r.text, str(r.url), canonical_url=seed_url)


def scrape_profile_file(seed_file: str | Path, canonical_url: str | None = None) -> list[dict]:
    html = Path(seed_file).read_text(encoding="utf-8")
    return _scrape_profile_document(html, str(seed_file), canonical_url=canonical_url)


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

ENRICHED_COLUMN_ORDER = [
    "source",
    "source_id",
    "name",
    "city",
    "certifications",
    "specialties",
    "website",
    "instagram",
    "bio_text",
    "lineage",
    "aliases",
    "blog_url",
    "rag_payload",
    "crawled_at",
]


def _available_instructor_columns(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'instructors'
        """
    )
    columns = {row[0] for row in cur.fetchall()}
    cur.close()
    return columns


def _build_upsert_sql(columns: set[str]) -> str:
    ordered = [col for col in ENRICHED_COLUMN_ORDER if col in columns]
    placeholders = ", ".join(f"%({col})s" for col in ordered)
    updates = []
    for col in ordered:
        if col in {"source", "source_id"}:
            continue
        if col in {"city", "certifications", "specialties", "website", "instagram", "bio_text", "lineage", "aliases", "blog_url", "rag_payload"}:
            updates.append(f"{col} = COALESCE(EXCLUDED.{col}, instructors.{col})")
        else:
            updates.append(f"{col} = EXCLUDED.{col}")
    return f"""
INSERT INTO instructors
  ({", ".join(ordered)})
VALUES
  ({placeholders})
ON CONFLICT (source, source_id) DO UPDATE SET
  {", ".join(updates)}
"""


def score_similarity(seed_row: dict[str, Any], candidate_row: dict[str, Any]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    seed_specs = set(seed_row.get("specialties") or [])
    cand_specs = set(candidate_row.get("specialties") or [])
    overlap_specs = sorted(seed_specs & cand_specs)
    if overlap_specs:
        score += 3.0 * len(overlap_specs)
        reasons.append(f"specialties={','.join(overlap_specs[:4])}")

    seed_certs = set(seed_row.get("certifications") or [])
    cand_certs = set(candidate_row.get("certifications") or [])
    overlap_certs = sorted(seed_certs & cand_certs)
    if overlap_certs:
        score += 2.0 * len(overlap_certs)
        reasons.append(f"certs={','.join(overlap_certs[:3])}")

    seed_lineage = set(seed_row.get("lineage") or [])
    cand_lineage = set(candidate_row.get("lineage") or [])
    overlap_lineage = sorted(seed_lineage & cand_lineage)
    if overlap_lineage:
        score += 2.0 * len(overlap_lineage)
        reasons.append(f"lineage={','.join(overlap_lineage[:3])}")

    seed_city = (seed_row.get("city") or "").strip()
    cand_city = (candidate_row.get("city") or "").strip()
    if seed_city and cand_city and (seed_city == cand_city or seed_city in cand_city or cand_city in seed_city):
        score += 1.5
        reasons.append(f"city={cand_city}")

    seed_tokens = _tokenize([seed_row.get("bio_text"), seed_row.get("bio"), seed_row.get("full_name")])
    cand_tokens = _tokenize([candidate_row.get("bio_text"), candidate_row.get("bio"), candidate_row.get("full_name")])
    keyword_overlap = sorted(seed_tokens & cand_tokens)
    if keyword_overlap:
        keyword_score = min(3.0, 0.25 * len(keyword_overlap))
        score += keyword_score
        reasons.append(f"keywords={','.join(keyword_overlap[:5])}")

    return score, reasons


def fetch_similar_instructors(conn, seed_row: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    available = _available_instructor_columns(conn)
    selected = [
        col for col in [
            "source", "source_id", "name", "city", "certifications", "specialties",
            "website", "instagram", "bio_text", "lineage", "crawled_at",
        ] if col in available
    ]
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"SELECT {', '.join(selected)} FROM instructors WHERE NOT (source = %s AND source_id = %s)",
        (seed_row.get("source") or seed_row.get("data_source"), seed_row.get("source_id")),
    )
    ranked = []
    for row in cur.fetchall():
        candidate = {
            "source": row.get("source"),
            "source_id": row.get("source_id"),
            "full_name": row.get("name"),
            "city": row.get("city"),
            "certifications": row.get("certifications") or [],
            "specialties": row.get("specialties") or [],
            "website": row.get("website"),
            "instagram": row.get("instagram"),
            "bio_text": row.get("bio_text"),
            "lineage": row.get("lineage") or [],
        }
        score, reasons = score_similarity(seed_row, candidate)
        if score <= 0:
            continue
        ranked.append({
            "score": round(score, 2),
            "reasons": reasons,
            **candidate,
        })
    cur.close()
    ranked.sort(key=lambda item: (-item["score"], item.get("full_name") or ""))
    return ranked[:limit]


def upsert_instructors(rows: list[dict], conn) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    available_columns = _available_instructor_columns(conn)
    upsert_sql = _build_upsert_sql(available_columns)
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
            "instagram":      r.get("instagram"),
            "bio_text":       r.get("bio_text") or r.get("bio"),
            "lineage":        r.get("lineage") or [],
            "aliases":        r.get("aliases") or [],
            "blog_url":       r.get("blog_url"),
            "rag_payload":    json.dumps(r.get("rag_payload"), ensure_ascii=False) if r.get("rag_payload") is not None else None,
            "crawled_at":     r.get("crawled_at") or datetime.now(timezone.utc).isoformat(),
        })
    psycopg2.extras.execute_batch(cur, upsert_sql, normalised, page_size=100)
    conn.commit()
    return len(normalised)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["taling", "lessonall", "kmong", "seed", "all"], default="all")
    ap.add_argument("--city",   help="Single city (default: all KR_CITIES)")
    ap.add_argument("--delay",  type=float, default=1.2, help="Base delay between requests (s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db-url",  default=DB_URL)
    ap.add_argument("--seed-url", default=None, help="Canonical instructor profile URL to ingest as a first-party source")
    ap.add_argument("--seed-file", default=None, help="Local canonical instructor profile HTML/Markdown file to ingest")
    ap.add_argument("--similar-to-seed", type=int, default=0, help="Rank and print the top N similar instructors against the seed profile")
    args = ap.parse_args()

    cities = [args.city] if args.city else KR_CITIES
    conn = None if args.dry_run else psycopg2.connect(args.db_url)
    total = 0
    seed_rows: list[dict[str, Any]] = []
    scraped_rows: list[dict[str, Any]] = []

    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        use_pw = _pw_available()
        if not use_pw:
            log.warning("Playwright not installed — falling back to static HTTP (likely 0 results for CSR sites)")

        if (args.seed_url or args.seed_file) and args.source in ("seed", "all"):
            seed_rows = (
                scrape_profile_file(args.seed_file, canonical_url=args.seed_url)
                if args.seed_file
                else scrape_profile_site(args.seed_url, client)
            )
            scraped_rows.extend(seed_rows)
            if args.dry_run:
                for row in seed_rows:
                    log.info(
                        "  [dry-run] seed: %s | city=%s | certs=%s | specialties=%s | instagram=%s | website=%s",
                        row.get("full_name"),
                        row.get("city"),
                        ",".join(row.get("certifications") or []),
                        ",".join(row.get("specialties") or []),
                        row.get("instagram") or "-",
                        row.get("website") or "-",
                    )
            else:
                n = upsert_instructors(seed_rows, conn)
                total += n
                log.info("  seed: upserted %d canonical profile(s)", n)

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
                scraped_rows.extend(city_rows)
                if not args.dry_run:
                    n = upsert_instructors(city_rows, conn)
                    total += n
                    log.info(f"  {city}: upserted {n} instructor profiles")
                else:
                    log.info(f"  [dry-run] {city}: {len(city_rows)} profiles found")

    if args.similar_to_seed and seed_rows:
        seed_row = seed_rows[0]
        if conn is not None:
            similar = fetch_similar_instructors(conn, seed_row, args.similar_to_seed)
        else:
            similar = []
            for row in scraped_rows:
                if row.get("source_id") == seed_row.get("source_id") and (row.get("source") or row.get("data_source")) == (seed_row.get("source") or seed_row.get("data_source")):
                    continue
                score, reasons = score_similarity(seed_row, row)
                if score <= 0:
                    continue
                similar.append({
                    "score": round(score, 2),
                    "reasons": reasons,
                    "source": row.get("data_source") or row.get("source"),
                    "full_name": row.get("full_name") or row.get("name"),
                    "city": row.get("city"),
                    "website": row.get("website"),
                })
            similar.sort(key=lambda item: (-item["score"], item.get("full_name") or ""))
            similar = similar[:args.similar_to_seed]

        log.info("Seed profile: %s (%s)", seed_row.get("full_name"), seed_row.get("website"))
        for idx, row in enumerate(similar, start=1):
            log.info(
                "  similar #%d: score=%.2f | source=%s | name=%s | city=%s | reasons=%s | website=%s",
                idx,
                row.get("score", 0.0),
                row.get("source") or "-",
                row.get("full_name") or row.get("name") or "-",
                row.get("city") or "-",
                "; ".join(row.get("reasons") or []),
                row.get("website") or row.get("instagram") or "-",
            )

    log.info(f"Done — {total} instructor profiles upserted total")
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
