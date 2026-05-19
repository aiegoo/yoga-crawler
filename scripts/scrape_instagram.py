#!/usr/bin/env python3
"""
Instagram scraper for yoga instructors and class schedules.

Strategy
--------
1. Hashtag crawl — iterate posts under yoga-instructor hashtags to discover
   instructor accounts.  Korean hashtags targeted: #요가강사 #요가클래스
   #서울요가 #요가스케줄 and city variants.
2. Profile harvest — for each unique instructor profile, read the bio, follower
   count, and recent posts to extract class schedule information.
3. Caption parsing — regex heuristics extract class style, day/time, price and
   studio name from Korean Instagram captions.

Outputs
-------
  data/instructors/instructors_ig.json   — instructor records (merged into main)
  data/classes/classes_raw.json          — class schedule records

Usage
-----
  # Discover instructors via hashtags (no login, public only)
  python scripts/scrape_instagram.py --mode hashtag --limit 200

  # Enrich from handles already known (e.g. scraped from studio pages)
  python scripts/scrape_instagram.py --mode profiles \
      --handles-file data/studios/studios_enriched.json

  # Full pipeline
  python scripts/scrape_instagram.py --mode all --limit 300 --delay 3

  # Authenticated (higher rate limits)
  python scripts/scrape_instagram.py --mode all --ig-user YOU --ig-pass PASS

Notes
-----
* All data fetched is from public profiles only.
* Instagram rate-limits unauthenticated requests heavily — keep --delay >= 3.
* instaloader >= 4.10 required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import instaloader
except ImportError:
    print("ERROR: pip install instaloader", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Hashtags to crawl for instructor discovery ────────────────────────────────

BASE_HASHTAGS = [
    "요가강사",        # yoga instructor
    "요가코치",        # yoga coach
    "요가클래스",      # yoga class
    "요가스케줄",      # yoga schedule
    "요가레슨",        # yoga lesson
    "요가수업",        # yoga lesson/class
    "서울요가",        # Seoul yoga
    "빈야사요가강사",  # vinyasa yoga instructor
    "아쉬탕가요가",    # ashtanga yoga
    "핫요가강사",      # hot yoga instructor
    "음요가",          # yin yoga
    "임산부요가",      # prenatal yoga
    "요가자격증",      # yoga certification
]

CITY_HASHTAGS = [f"{c}요가" for c in [
    "서울", "부산", "대구", "인천", "광주", "대전", "수원",
    "성남", "고양", "용인", "청주", "전주", "안양", "천안",
]]

ALL_HASHTAGS = BASE_HASHTAGS + CITY_HASHTAGS

# ── Caption parsing patterns ──────────────────────────────────────────────────

# Yoga style keywords mapped to canonical names
STYLE_MAP = {
    "빈야사": "vinyasa",   "vinyasa": "vinyasa",
    "아쉬탕가": "ashtanga", "ashtanga": "ashtanga",
    "하타": "hatha",        "hatha": "hatha",
    "쿤달리니": "kundalini", "kundalini": "kundalini",
    "핫요가": "hot",        "hot yoga": "hot",
    "음요가": "yin",        "yin yoga": "yin",
    "리스토러티브": "restorative", "restorative": "restorative",
    "임산부요가": "prenatal", "산전요가": "prenatal", "prenatal": "prenatal",
    "필라테스": "pilates",   "pilates": "pilates",
    "명상": "meditation",   "meditation": "meditation",
    "흐름": "flow",
}

# Day-of-week extraction
DAY_PATTERN = re.compile(
    r"(?:월|화|수|목|금|토|일)요일|(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?",
    re.IGNORECASE,
)
DAY_KR = {"월": "mon", "화": "tue", "수": "wed", "목": "thu", "금": "fri", "토": "sat", "일": "sun"}

# Time extraction: "10시", "10:30", "오전 10시", "오후 7시 30분"
TIME_PATTERN = re.compile(
    r"(?:오전|오후|아침|저녁|낮)?\s*(\d{1,2})시(?:\s*(\d{2})분)?|(\d{1,2}):(\d{2})\s*(?:AM|PM)?",
    re.IGNORECASE,
)

# Price extraction: "5만원", "50,000원", "월 12만원"
PRICE_PATTERN = re.compile(
    r"(\d+(?:,\d{3})*)\s*원|(\d+)\s*만\s*원",
    re.IGNORECASE,
)

# Contraindication keywords (for classes.contraindications safety flag)
CONTRAINDICATION_KEYWORDS = {
    "back_pain":    ["허리", "척추", "디스크"],
    "knee_pain":    ["무릎", "관절"],
    "pregnancy":    ["임산부", "임신", "산전", "산후"],
    "hypertension": ["고혈압", "혈압"],
    "injury":       ["부상", "통증"],
}

# Instructor bio indicators — accounts that look like instructors
INSTRUCTOR_BIO_KEYWORDS = [
    "요가강사", "요가선생", "요가코치", "yoga instructor", "yoga teacher",
    "RYT", "E-RYT", "YACEP", "yoga alliance",
    "빈야사", "아쉬탕가", "하타", "쿤달리니",
    "수련", "스튜디오", "클래스 문의",
]


# ── Instagram loader setup ────────────────────────────────────────────────────

def make_loader(ig_user: str | None = None, ig_pass: str | None = None) -> instaloader.Instaloader:
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
        max_connection_attempts=2,
    )
    if ig_user and ig_pass:
        try:
            loader.login(ig_user, ig_pass)
            log.info("Logged in as @%s", ig_user)
        except Exception as exc:
            log.warning("Login failed: %s — continuing as anonymous", exc)
    return loader


# ── Caption parsing ───────────────────────────────────────────────────────────

def parse_styles(text: str) -> list[str]:
    text_lower = text.lower()
    found = set()
    for kr, canon in STYLE_MAP.items():
        if kr in text_lower:
            found.add(canon)
    return sorted(found)


def parse_schedule_from_caption(caption: str) -> list[dict]:
    """
    Extract one or more class slots from an Instagram caption.
    Returns list of schedule dicts:
      {"day": "mon", "time": "10:00", "style": "vinyasa", "price_krw": 50000}
    """
    if not caption:
        return []

    slots = []
    styles = parse_styles(caption)

    # Find all time mentions
    for m in TIME_PATTERN.finditer(caption):
        hour = int(m.group(1) or m.group(3) or 0)
        minute = int(m.group(2) or m.group(4) or 0)

        # Check for 오후 (PM) modifier in the preceding 15 chars
        pre = caption[max(0, m.start() - 15): m.start()]
        if "오후" in pre or "저녁" in pre:
            if hour < 12:
                hour += 12

        time_str = f"{hour:02d}:{minute:02d}"

        # Find nearby day mentions (within ±50 chars)
        window = caption[max(0, m.start() - 50): m.end() + 50]
        days_found = []
        for day_m in DAY_PATTERN.finditer(window):
            raw = day_m.group(0)
            for kr, en in DAY_KR.items():
                if raw.startswith(kr):
                    days_found.append(en)
                    break
            else:
                days_found.append(raw[:3].lower())

        price = None
        for pm in PRICE_PATTERN.finditer(window):
            if pm.group(2):   # "N만원"
                price = int(pm.group(2)) * 10000
            elif pm.group(1): # "N,000원"
                price = int(pm.group(1).replace(",", ""))
            break

        slot = {
            "time": time_str,
            "days": days_found or [],
            "styles": styles,
            "price_krw": price,
        }
        slots.append(slot)

    # If no time found but styles found, create a style-only slot
    if not slots and styles:
        slots.append({"time": None, "days": [], "styles": styles, "price_krw": None})

    return slots


def parse_contraindications(bio: str, caption: str = "") -> list[str]:
    text = (bio or "") + " " + (caption or "")
    flags = []
    for flag, keywords in CONTRAINDICATION_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            flags.append(flag)
    return flags


def looks_like_instructor(profile: instaloader.Profile) -> bool:
    bio = (profile.biography or "").lower()
    return any(kw.lower() in bio for kw in INSTRUCTOR_BIO_KEYWORDS)


# ── Core scraping functions ───────────────────────────────────────────────────

def harvest_hashtag(loader: instaloader.Instaloader, hashtag: str,
                    limit: int, delay: float) -> tuple[set[str], list[dict]]:
    """
    Crawl posts under `hashtag`. Return:
      - set of Instagram usernames that look like instructors
      - list of raw class schedule dicts extracted from captions
    """
    instructor_handles: set[str] = set()
    raw_classes: list[dict] = []
    count = 0

    log.info("Hashtag #%s (limit=%d)", hashtag, limit)
    try:
        posts = loader.get_hashtag_posts(hashtag)
    except Exception as exc:
        log.warning("#%s fetch error: %s", hashtag, exc)
        return instructor_handles, raw_classes

    for post in posts:
        if count >= limit:
            break
        count += 1

        try:
            owner = post.owner_username
            caption = post.caption or ""

            # Instructor detection: check bio lazily only for promising captions
            if any(kw in caption for kw in ["강사", "코치", "선생", "클래스", "수업", "스케줄", "레슨"]):
                instructor_handles.add(owner)

            # Parse class schedule from caption
            slots = parse_schedule_from_caption(caption)
            for slot in slots:
                raw_classes.append({
                    "source_handle":    owner,
                    "source_post_id":   str(post.mediaid),
                    "source_hashtag":   hashtag,
                    "caption_snippet":  caption[:300],
                    "time":             slot["time"],
                    "days":             slot["days"],
                    "styles":           slot["styles"],
                    "price_krw":        slot["price_krw"],
                    "scraped_at":       datetime.now(timezone.utc).isoformat(),
                })

        except instaloader.exceptions.LoginRequiredException:
            log.warning("#%s: login required at post %d — stopping hashtag", hashtag, count)
            break
        except Exception as exc:
            log.debug("Post error: %s", exc)
            continue

        time.sleep(random.uniform(delay * 0.5, delay))

    log.info("  #%s → %d posts, %d potential instructors, %d class slots",
             hashtag, count, len(instructor_handles), len(raw_classes))
    return instructor_handles, raw_classes


def harvest_profile(loader: instaloader.Instaloader, handle: str,
                    post_limit: int, delay: float) -> tuple[dict | None, list[dict]]:
    """
    Fetch full profile info for `handle` and parse recent posts for schedules.
    Returns (instructor_dict, list_of_class_dicts).
    """
    try:
        profile = instaloader.Profile.from_username(loader.context, handle)
    except instaloader.exceptions.ProfileNotExistsException:
        log.debug("@%s not found", handle)
        return None, []
    except instaloader.exceptions.LoginRequiredException:
        log.warning("@%s requires login", handle)
        return None, []
    except Exception as exc:
        log.warning("@%s error: %s", handle, exc)
        return None, []

    if not looks_like_instructor(profile):
        return None, []

    bio = profile.biography or ""
    styles = parse_styles(bio)
    contraindications = parse_contraindications(bio)

    # Stable ID from username
    inst_id = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-")

    instructor = {
        "instructor_id":       inst_id,
        "full_name":           profile.full_name or handle,
        "bio":                 bio[:500] or None,
        "instagram_handle":    handle,
        "instagram_url":       f"https://www.instagram.com/{handle}/",
        "instagram_followers": profile.followers,
        "certification_level": _extract_cert(bio),
        "yoga_alliance_id":    None,
        "lineage_school":      None,
        "lineage_depth":       0,
        "city":                _extract_city(bio),
        "country":             "KR",
        "specialties":         styles,
        "contraindications":   contraindications,
        "avg_rating":          None,
        "review_count":        0,
        "data_source":         "instagram",
        "scraped_at":          datetime.now(timezone.utc).isoformat(),
    }

    # Parse recent posts for class schedules
    classes: list[dict] = []
    try:
        for post in profile.get_posts():
            if len(classes) >= post_limit:
                break
            caption = post.caption or ""
            slots = parse_schedule_from_caption(caption)
            for slot in slots:
                if not slot["styles"] and not slot["time"]:
                    continue
                class_id = hashlib.md5(
                    f"{handle}:{post.mediaid}:{slot['time']}".encode()
                ).hexdigest()[:12]
                classes.append({
                    "class_id":          class_id,
                    "instructor_id":     inst_id,
                    "instructor_handle": handle,
                    "studio_id":         None,  # linked later via studio enrichment
                    "title":             f"{handle} {'/'.join(slot['styles']) or '요가'} 클래스",
                    "style":             slot["styles"][0] if slot["styles"] else None,
                    "difficulty":        None,
                    "duration_min":      None,
                    "price_krw":         slot["price_krw"],
                    "schedule": {
                        "days":  slot["days"],
                        "time":  slot["time"],
                    },
                    "target_outcomes":   [],
                    "contraindications": parse_contraindications("", caption),
                    "source_post_id":    str(post.mediaid),
                    "caption_snippet":   caption[:300],
                    "scraped_at":        datetime.now(timezone.utc).isoformat(),
                })
            time.sleep(random.uniform(0.5, delay))
    except instaloader.exceptions.LoginRequiredException:
        log.warning("@%s posts require login", handle)
    except Exception as exc:
        log.debug("@%s posts error: %s", handle, exc)

    log.info("  @%s — %d followers, %d class slots found", handle, profile.followers, len(classes))
    return instructor, classes


def _extract_cert(bio: str) -> str | None:
    for cert in ["E-RYT-500", "E-RYT-200", "RYT-500", "RYT-200", "YACEP"]:
        if cert in bio.upper():
            return cert
    return None


def _extract_city(bio: str) -> str | None:
    cities = ["서울", "부산", "대구", "인천", "광주", "대전", "수원", "울산",
              "성남", "고양", "용인", "창원", "청주", "전주", "안산", "안양", "천안"]
    for city in cities:
        if city in bio:
            return city
    return None


# ── Handle list from existing studio enrichment ───────────────────────────────

def load_handles_from_studios(studios_json: Path) -> list[str]:
    """Extract instagram handles already scraped from studio pages."""
    if not studios_json.exists():
        return []
    data = json.loads(studios_json.read_text())
    handles = []
    for studio in data:
        h = studio.get("instagram") or studio.get("instagram_handle")
        if h:
            # Strip URL prefix if present
            h = h.rstrip("/").split("/")[-1].lstrip("@")
            if h:
                handles.append(h)
    log.info("Found %d Instagram handles in studio enrichment data", len(handles))
    return handles


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["hashtag", "profiles", "all"], default="all",
                    help="hashtag=discover only, profiles=enrich only, all=both (default)")
    ap.add_argument("--hashtags", default="",
                    help="Comma-separated hashtags to crawl (default: built-in list)")
    ap.add_argument("--limit", type=int, default=150,
                    help="Max posts to scan per hashtag (default: 150)")
    ap.add_argument("--post-limit", type=int, default=20,
                    help="Max recent posts to parse per profile (default: 20)")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="Seconds between requests (default: 3.0)")
    ap.add_argument("--handles", default="",
                    help="Comma-separated handles to profile directly")
    ap.add_argument("--handles-file", type=Path, default=None,
                    help="JSON file to read instagram handles from (studio enrichment output)")
    ap.add_argument("--ig-user", default=os.environ.get("IG_USER"),
                    help="Instagram username for auth session (env: IG_USER)")
    ap.add_argument("--ig-pass", default=os.environ.get("IG_PASS"),
                    help="Instagram password (env: IG_PASS)")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "data",
                    help="Output root directory (default: data/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and print without writing files")
    args = ap.parse_args()

    loader = make_loader(args.ig_user, args.ig_pass)

    all_instructor_handles: set[str] = set()
    all_raw_classes: list[dict] = []

    # ── Phase 1: hashtag discovery ────────────────────────────────────────────
    if args.mode in ("hashtag", "all"):
        hashtags = [h.strip() for h in args.hashtags.split(",") if h.strip()] or ALL_HASHTAGS
        for ht in hashtags:
            try:
                handles, classes = harvest_hashtag(loader, ht, args.limit, args.delay)
                all_instructor_handles.update(handles)
                all_raw_classes.extend(classes)
            except KeyboardInterrupt:
                log.info("Interrupted — saving partial results")
                break
            # Longer pause between hashtags to avoid rate limiting
            time.sleep(random.uniform(args.delay, args.delay * 2))

        log.info("Phase 1 complete: %d candidate instructor handles discovered",
                 len(all_instructor_handles))

    # ── Phase 2: profile harvesting ───────────────────────────────────────────
    if args.mode in ("profiles", "all"):
        # Handles from CLI
        explicit = [h.strip().lstrip("@") for h in args.handles.split(",") if h.strip()]
        all_instructor_handles.update(explicit)

        # Handles from studio enrichment file
        if args.handles_file:
            file_handles = load_handles_from_studios(args.handles_file)
            all_instructor_handles.update(file_handles)
        else:
            # Auto-detect from studios enriched JSON
            default_enriched = args.out_dir / "studios" / "studios_enriched.json"
            if default_enriched.exists():
                all_instructor_handles.update(load_handles_from_studios(default_enriched))

        instructors: list[dict] = []
        profile_classes: list[dict] = []
        handles_list = sorted(all_instructor_handles)

        log.info("Phase 2: profiling %d handles", len(handles_list))
        for i, handle in enumerate(handles_list, 1):
            log.info("[%d/%d] @%s", i, len(handles_list), handle)
            inst, classes = harvest_profile(loader, handle, args.post_limit, args.delay)
            if inst:
                instructors.append(inst)
                profile_classes.extend(classes)
            time.sleep(random.uniform(args.delay, args.delay * 1.5))

        log.info("Phase 2 complete: %d instructors, %d class slots",
                 len(instructors), len(profile_classes))

        # Merge class sources
        all_raw_classes.extend(profile_classes)

        # ── Write instructors ────────────────────────────────────────────────
        if not args.dry_run and instructors:
            out_dir = args.out_dir / "instructors"
            out_dir.mkdir(parents=True, exist_ok=True)
            ig_json = out_dir / "instructors_ig.json"
            ig_json.write_text(
                json.dumps(instructors, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.info("Wrote %s (%d instructors)", ig_json, len(instructors))

            # Merge into main instructors_raw.json
            main_json = out_dir / "instructors_raw.json"
            existing = json.loads(main_json.read_text()) if main_json.exists() else []
            merged = {r["instructor_id"]: r for r in existing}
            for r in instructors:
                merged[r["instructor_id"]] = r
            main_json.write_text(
                json.dumps(list(merged.values()), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.info("Merged into %s (%d total instructors)", main_json, len(merged))
        elif args.dry_run:
            log.info("[dry-run] Would write %d instructors", len(instructors))
            for inst in instructors[:3]:
                log.info("  %s @%s %d followers", inst["full_name"],
                         inst["instagram_handle"], inst["instagram_followers"])

    # ── Write classes ─────────────────────────────────────────────────────────
    # Dedup by class_id
    seen_class_ids: set[str] = set()
    deduped_classes: list[dict] = []
    for cls in all_raw_classes:
        cid = cls.get("class_id") or hashlib.md5(
            f"{cls.get('source_handle')}:{cls.get('source_post_id')}:{cls.get('time')}".encode()
        ).hexdigest()[:12]
        cls["class_id"] = cid
        if cid not in seen_class_ids:
            seen_class_ids.add(cid)
            deduped_classes.append(cls)

    log.info("Total class slots (deduped): %d", len(deduped_classes))

    if not args.dry_run and deduped_classes:
        out_dir = args.out_dir / "classes"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_json = out_dir / "classes_raw.json"
        out_json.write_text(
            json.dumps(deduped_classes, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Wrote %s (%d classes)", out_json, len(deduped_classes))
    elif args.dry_run:
        log.info("[dry-run] Would write %d class slots", len(deduped_classes))
        for cls in deduped_classes[:3]:
            log.info("  %s | %s | %s", cls.get("source_handle"),
                     cls.get("styles"), cls.get("time"))

    log.info("Done.")


if __name__ == "__main__":
    main()
