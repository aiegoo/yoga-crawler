#!/usr/bin/env python3
"""
Instructor data scraper for the yoga matching system.

Sources
-------
1. Yoga Alliance public directory  (https://www.yogaalliance.org/directory)
   — BeautifulSoup over paginated search results
2. Instagram public profiles        (via instaloader — no API key required)
   — follower count, bio, recent post tags

Output
------
  data/instructors/instructors_raw.json   — one object per instructor
  data/instructors/instructors_seed.sql   — Flyway-ready INSERT statements

Usage
-----
  # Scrape Yoga Alliance directory (e.g. first 3 pages, city = Seoul)
  python scripts/scrape_instructors.py --source yogaalliance --city Seoul --pages 3

  # Enrich existing instructors.json with Instagram follower counts
  python scripts/scrape_instructors.py --source instagram \
      --handles elbee.yogaman,someother_yogi

  # Full pipeline: scrape YA + enrich + write SQL seed
  python scripts/scrape_instructors.py --all --city Seoul --pages 5

Notes
-----
* Yoga Alliance ToS allows non-commercial directory browsing. Obey robots.txt.
  We add a 1-2 s random delay between requests (configurable via --delay).
* Instagram: instaloader reads public profiles only. Rate limits apply —
  use --ig-sleep 5 (seconds between profiles) to stay well within limits.
  If you have an IG account you can pass --ig-user / --ig-pass for higher limits.
* For production use, consider the official Instagram Basic Display API instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

# Optional: instaloader for Instagram profiles
try:
    import instaloader
    INSTALOADER_AVAILABLE = True
except ImportError:
    INSTALOADER_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "instructors"
OUT_JSON = OUT_DIR / "instructors_raw.json"
OUT_SQL = OUT_DIR / "instructors_seed.sql"

YA_SEARCH_URL = "https://www.yogaalliance.org/directory"
YA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# Naver Local Search API (fallback for Korean instructors)
NAVER_LOCAL_URL = "https://openapi.naver.com/v1/search/local.json"
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

KR_CITIES = [
    "서울", "부산", "대구", "인천", "광주", "대전", "수원", "울산",
    "성남", "고양", "용인", "창원", "청주", "전주", "안산", "안양",
]

INSTRUCTOR_KEYWORDS = ["요가강사", "요가코치", "요가선생님"]

# Certification tier weights used in trust score calculation
CERT_WEIGHT: dict[str, float] = {
    "E-RYT-500": 1.0,
    "E-RYT-200": 0.8,
    "RYT-500":   0.6,
    "RYT-200":   0.4,
    "YACEP":     0.2,
}


# ── Yoga Alliance scraper ─────────────────────────────────────────────────────

def scrape_yoga_alliance(city: str, pages: int, delay: float) -> list[dict]:
    """
    Scrape the Yoga Alliance public directory for instructors in a given city.

    The YA directory renders HTML server-side so BeautifulSoup works directly.
    Pagination is via `?page=N` query param.
    """
    results: list[dict] = []
    with httpx.Client(headers=YA_HEADERS, timeout=15, follow_redirects=True) as client:
        for page in range(1, pages + 1):
            url = f"{YA_SEARCH_URL}?city={city}&type=RYT&page={page}"
            log.info("YA page %d → %s", page, url)

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("HTTP error on page %d: %s", page, exc)
                break

            # Detect YA policy-redirect (directory access removed without login)
            if "directory-listing-policy" in str(resp.url) or "policies-priorities" in str(resp.url):
                log.warning(
                    "Yoga Alliance redirected to policy page — directory is no longer publicly accessible. "
                    "URL: %s", resp.url
                )
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # Each instructor card — selector may need updating if YA redesigns their page
            cards = (
                soup.select("div.teacher-card")
                or soup.select("li.directory-result")
                or soup.select("[class*='instructor']")
                or soup.select("[class*='teacher']")
            )

            if not cards:
                log.info("No cards found on page %d — may be last page or selector mismatch", page)
                snippet = resp.text[:500].replace("\n", " ")
                log.debug("HTML snippet: %s", snippet)
                break

            for card in cards:
                instructor = _parse_ya_card(card)
                if instructor:
                    results.append(instructor)

            log.info("  → %d instructors collected so far", len(results))
            jitter = random.uniform(0.5, delay)
            time.sleep(jitter)

    return results


def _parse_ya_card(card) -> dict | None:
    """Extract fields from a single YA directory card element."""
    name_el = (
        card.select_one("h2") or card.select_one("h3")
        or card.select_one(".name") or card.select_one("[class*='name']")
    )
    if not name_el:
        return None

    full_name = name_el.get_text(strip=True)
    slug = re.sub(r"[^a-z0-9]+", "-", full_name.lower()).strip("-")

    cert_el = card.select_one("[class*='cert']") or card.select_one("[class*='level']")
    cert = cert_el.get_text(strip=True) if cert_el else None

    city_el = card.select_one("[class*='city']") or card.select_one("[class*='location']")
    city = city_el.get_text(strip=True) if city_el else None

    bio_el = card.select_one("p") or card.select_one("[class*='bio']")
    bio = bio_el.get_text(strip=True) if bio_el else None

    ya_id_el = card.select_one("a[href*='/profile/']")
    ya_id = None
    if ya_id_el:
        m = re.search(r"/profile/(\d+)", ya_id_el.get("href", ""))
        ya_id = m.group(1) if m else None

    return {
        "instructor_id":       slug,
        "full_name":           full_name,
        "bio":                 bio,
        "certification_level": _normalise_cert(cert),
        "yoga_alliance_id":    ya_id,
        "lineage_school":      None,
        "lineage_depth":       0,
        "city":                city,
        "country":             None,
        "instagram_handle":    None,
        "instagram_followers": None,
        "specialties":         [],
        "avg_rating":          None,
        "review_count":        0,
        "data_source":         "yogaalliance",
        "scraped_at":          datetime.now(timezone.utc).isoformat(),
    }


def _normalise_cert(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.upper()
    for cert in CERT_WEIGHT:
        if cert in raw:
            return cert
    return raw[:50]


# ── Instagram enrichment ──────────────────────────────────────────────────────

def enrich_instagram(instructors: list[dict], handles: list[str],
                     ig_sleep: float, ig_user: str | None, ig_pass: str | None) -> list[dict]:
    """
    For each handle in `handles`, fetch public profile data via instaloader
    and merge into the matching instructor dict (matched by instagram_handle).
    If a handle isn't found in instructors list, a new stub entry is created.
    """
    if not INSTALOADER_AVAILABLE:
        log.error("instaloader not installed — run: pip install instaloader")
        return instructors

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )

    if ig_user and ig_pass:
        try:
            loader.login(ig_user, ig_pass)
            log.info("Logged in to Instagram as %s", ig_user)
        except Exception as exc:
            log.warning("IG login failed (%s) — continuing as anonymous", exc)

    idx_by_handle = {
        (i.get("instagram_handle") or "").lower(): n
        for n, i in enumerate(instructors)
    }

    for handle in handles:
        handle = handle.lstrip("@").strip()
        log.info("Instagram: fetching @%s", handle)

        try:
            profile = instaloader.Profile.from_username(loader.context, handle)
        except instaloader.exceptions.ProfileNotExistsException:
            log.warning("Profile @%s not found", handle)
            continue
        except instaloader.exceptions.ConnectionException as exc:
            log.warning("Rate limited or connection error for @%s: %s", handle, exc)
            time.sleep(ig_sleep * 3)
            continue

        ig_data = {
            "instagram_handle":    handle,
            "instagram_followers": profile.followers,
            "instagram_url":       f"https://www.instagram.com/{handle}/",
            "bio":                 profile.biography[:500] if profile.biography else None,
            "full_name":           profile.full_name or handle,
        }

        idx = idx_by_handle.get(handle.lower())
        if idx is not None:
            instructors[idx].update({k: v for k, v in ig_data.items() if v is not None})
        else:
            # New entry from Instagram only
            slug = re.sub(r"[^a-z0-9]+", "-", ig_data["full_name"].lower()).strip("-") or handle
            stub = {
                "instructor_id":       slug,
                "full_name":           ig_data["full_name"],
                "bio":                 ig_data.get("bio"),
                "certification_level": None,
                "yoga_alliance_id":    None,
                "lineage_school":      None,
                "lineage_depth":       0,
                "city":                None,
                "country":             None,
                "instagram_handle":    handle,
                "instagram_followers": ig_data["instagram_followers"],
                "instagram_url":       ig_data["instagram_url"],
                "specialties":         _infer_specialties_from_bio(ig_data.get("bio", "")),
                "avg_rating":          None,
                "review_count":        0,
                "data_source":         "instagram",
                "scraped_at":          datetime.now(timezone.utc).isoformat(),
            }
            instructors.append(stub)
            idx_by_handle[handle.lower()] = len(instructors) - 1

        log.info("  @%s — %d followers", handle, profile.followers)
        time.sleep(ig_sleep)

    return instructors


def _infer_specialties_from_bio(bio: str) -> list[str]:
    """Heuristic: look for known yoga style / condition keywords in bio text."""
    if not bio:
        return []
    bio_lower = bio.lower()
    keywords = {
        "back_pain":   ["back pain", "back health", "spine"],
        "prenatal":    ["prenatal", "pregnancy", "maternity"],
        "restorative": ["restorative", "yin", "gentle"],
        "vinyasa":     ["vinyasa", "flow"],
        "ashtanga":    ["ashtanga"],
        "iyengar":     ["iyengar"],
        "kundalini":   ["kundalini"],
        "meditation":  ["meditation", "mindfulness"],
        "breathwork":  ["pranayama", "breathwork", "breathe"],
    }
    return [tag for tag, terms in keywords.items() if any(t in bio_lower for t in terms)]



# ── Naver Local Search instructor scraper ────────────────────────────────────

def scrape_naver_instructors(cities=None, delay=1.5):
    """Search Naver Local API for individual yoga instructors in Korean cities."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        log.warning("NAVER_CLIENT_ID/SECRET not set — skipping Naver instructor search")
        return []

    cities = cities or KR_CITIES
    results = []
    seen_ids = set()
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    client = httpx.Client(headers=headers, timeout=10, follow_redirects=True)

    for city in cities:
        for keyword in INSTRUCTOR_KEYWORDS:
            query = f"{city} {keyword}"
            start = 1
            while start <= 100:
                params = {"query": query, "display": 5, "start": start, "sort": "comment"}
                try:
                    resp = client.get(NAVER_LOCAL_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get("items", [])
                except Exception as exc:
                    log.warning("Naver '%s' start=%d error: %s", query, start, exc)
                    break
                if not items:
                    break
                log.info("Naver '%s' start=%d → %d items", query, start, len(items))
                for item in items:
                    name = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                    if not name:
                        continue
                    inst_id = re.sub(r"[^a-z0-9]+", "-", (name + "-" + city).lower()).strip("-")
                    if inst_id in seen_ids:
                        continue
                    seen_ids.add(inst_id)
                    desc = item.get("description", "") or ""
                    addr = item.get("roadAddress") or item.get("address") or ""
                    results.append({
                        "instructor_id":       inst_id,
                        "full_name":           name,
                        "bio":                 desc or None,
                        "certification_level": None,
                        "yoga_alliance_id":    None,
                        "lineage_school":      None,
                        "lineage_depth":       0,
                        "city":                city,
                        "country":             "KR",
                        "address":             addr or None,
                        "telephone":           item.get("telephone") or None,
                        "naver_link":          item.get("link") or None,
                        "instagram_handle":    None,
                        "instagram_followers": None,
                        "specialties":         _infer_specialties_from_bio(desc),
                        "avg_rating":          None,
                        "review_count":        0,
                        "data_source":         "naver_local",
                        "scraped_at":          datetime.now(timezone.utc).isoformat(),
                    })
                total = int(data.get("total", 0))
                if start + 5 > min(total, 100):
                    break
                start += 5
                time.sleep(random.uniform(0.4, delay))
        time.sleep(random.uniform(0.2, 0.5))

    client.close()
    log.info("Naver instructor search: %d unique instructors", len(results))
    return results


# ── Trust score calculation ───────────────────────────────────────────────────

def compute_trust_score(instructor: dict) -> float:
    """
    trust_score = cert_weight + review_weight + lineage_weight + ig_weight
    All components capped so total ≤ 1.0
    """
    score = 0.0

    # Certification (0.0–1.0 each, take the highest tier)
    cert = instructor.get("certification_level") or ""
    score += CERT_WEIGHT.get(cert, 0.0)

    # Reviews (0.0–0.3)
    avg = instructor.get("avg_rating")
    if avg is not None:
        score += min(float(avg), 5.0) / 5.0 * 0.3

    # Lineage depth (0.0–0.2, 5 levels max)
    depth = min(int(instructor.get("lineage_depth") or 0), 4)
    score += depth * 0.05

    # Instagram followers (log scale → 0.0–0.1)
    followers = instructor.get("instagram_followers") or 0
    if followers > 0:
        score += min(math.log10(followers) / 7.0, 0.1)

    return round(min(score, 1.0), 3)


# ── SQL seed writer ───────────────────────────────────────────────────────────

def _sql_str(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    escaped = str(v).replace("'", "''")
    return f"'{escaped}'"


def write_sql(instructors: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "-- Auto-generated by scripts/scrape_instructors.py",
        f"-- {datetime.now(timezone.utc).isoformat()}",
        "--",
        "-- Run after V4__add_instructors.sql migration.",
        "",
        "INSERT INTO instructors (",
        "    instructor_id, full_name, bio, certification_level, yoga_alliance_id,",
        "    fyt_certified, lineage_school, lineage_depth, instagram_handle,",
        "    instagram_followers, instagram_url, website_url,",
        "    avg_rating, review_count, instructor_trust_score,",
        "    city, country, data_source, scraped_at",
        ") VALUES",
    ]

    rows = []
    for i in instructors:
        trust = compute_trust_score(i)
        i["instructor_trust_score"] = trust
        row = (
            f"    ({_sql_str(i['instructor_id'])}, {_sql_str(i['full_name'])}, "
            f"{_sql_str(i.get('bio'))}, {_sql_str(i.get('certification_level'))}, "
            f"{_sql_str(i.get('yoga_alliance_id'))}, "
            f"{_sql_str(i.get('fyt_certified', False))}, "
            f"{_sql_str(i.get('lineage_school'))}, "
            f"{_sql_str(i.get('lineage_depth', 0))}, "
            f"{_sql_str(i.get('instagram_handle'))}, "
            f"{_sql_str(i.get('instagram_followers'))}, "
            f"{_sql_str(i.get('instagram_url'))}, "
            f"{_sql_str(i.get('website_url'))}, "
            f"{_sql_str(i.get('avg_rating'))}, "
            f"{_sql_str(i.get('review_count', 0))}, "
            f"{_sql_str(trust)}, "
            f"{_sql_str(i.get('city'))}, "
            f"{_sql_str(i.get('country'))}, "
            f"{_sql_str(i.get('data_source', 'manual'))}, "
            f"{_sql_str(i.get('scraped_at'))})"
        )
        rows.append(row)

    lines.append(",\n".join(rows) + ";")
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("SQL seed written → %s (%d rows)", path, len(rows))


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["yogaalliance", "instagram", "naver", "all"],
                   default="naver")
    p.add_argument("--all", action="store_true",
                   help="Equivalent to --source all")
    p.add_argument("--cities", default="",
                   help="Comma-separated Korean city names (default: all 16)")
    p.add_argument("--city", default="Seoul",
                   help="City for Yoga Alliance directory search (default: Seoul)")
    p.add_argument("--pages", type=int, default=2,
                   help="Number of YA directory pages to scrape (default: 2)")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Max seconds to wait between YA requests (default: 2.0)")
    p.add_argument("--handles", default="",
                   help="Comma-separated Instagram handles to enrich (no @ needed)")
    p.add_argument("--ig-sleep", type=float, default=5.0,
                   help="Seconds between Instagram profile fetches (default: 5.0)")
    p.add_argument("--ig-user", default=None,
                   help="Instagram username for authenticated session (optional)")
    p.add_argument("--ig-pass", default=None,
                   help="Instagram password (use env var IG_PASS instead)")
    p.add_argument("--input", type=Path, default=None,
                   help="Existing instructors_raw.json to enrich (skip YA scrape)")
    p.add_argument("--out-json", type=Path, default=OUT_JSON)
    p.add_argument("--out-sql",  type=Path, default=OUT_SQL)
    p.add_argument("--dry-run", action="store_true", help="No-op: accepted for pipeline compatibility")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    source = "all" if args.all else args.source

    # Load existing data if provided
    instructors: list[dict] = []
    if args.input and args.input.exists():
        instructors = json.loads(args.input.read_text())
        log.info("Loaded %d instructors from %s", len(instructors), args.input)

    # Yoga Alliance scrape
    if source in ("yogaalliance", "all") and not args.input:
        ya_results = scrape_yoga_alliance(args.city, args.pages, args.delay)
        instructors.extend(ya_results)
        log.info("Yoga Alliance: %d instructors collected", len(ya_results))

    # Naver Local Search (Korean instructors)
    if source in ("naver", "all") and not args.input:
        cities = [c.strip() for c in args.cities.split(",") if c.strip()] or None
        naver_results = scrape_naver_instructors(cities=cities, delay=args.delay)
        instructors.extend(naver_results)

    # Instagram enrichment
    handles = [h.strip() for h in args.handles.split(",") if h.strip()]
    if source in ("instagram", "all") and handles:
        ig_pass = args.ig_pass or os.environ.get("IG_PASS")
        instructors = enrich_instagram(instructors, handles, args.ig_sleep,
                                       args.ig_user, ig_pass)

    if not instructors:
        log.warning("No instructor data collected. Check selectors or network access.")
        sys.exit(0)

    # Compute trust scores
    for i in instructors:
        i["instructor_trust_score"] = compute_trust_score(i)

    # Deduplicate by instructor_id (last-write-wins)
    seen: dict[str, dict] = {}
    for i in instructors:
        seen[i["instructor_id"]] = i
    instructors = list(seen.values())

    # Write outputs
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(instructors, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    log.info("JSON written → %s (%d instructors)", args.out_json, len(instructors))

    write_sql(instructors, args.out_sql)
    log.info("Done. To seed DB: psql -d yogadb -f %s", args.out_sql)


if __name__ == "__main__":
    main()