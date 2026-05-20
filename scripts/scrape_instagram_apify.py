#!/usr/bin/env python3
"""
Instagram scraper using the Apify Instagram Scraper actor (apify/instagram-scraper).

Why Apify instead of direct instaloader
----------------------------------------
* No Instagram session cookies or burner account required.
* Apify manages residential proxy rotation and anti-detection internally.
* EC2's data-centre IP never touches Instagram's edge — zero ban risk.
* Runs on Apify's cloud; EC2 instance just collects the results.

Authentication
--------------
Set your Apify API token in the environment:

  APIFY_TOKEN=apify_api_xxxxxxxxxxxxxxxxxxxx

Get it from: https://console.apify.com/settings/integrations

Cost (apify/instagram-scraper, $1.50 / 1,000 results)
------------------------------------------------------
  27 hashtags × 50 posts  = 1,350 results  ≈  $2.03
  50 profiles (details)   =    50 results  ≈  $0.08
  50 profiles × 20 posts  = 1,000 results  ≈  $1.50
  ─────────────────────────────────────────────────
  Estimated total                           ≈  $3.61

  Use --hashtag-limit / --post-limit to control spend.

Pipeline
--------
1. HASHTAG  — one actor run with all hashtag explore-page URLs.
              Collects post owner usernames whose captions contain
              yoga instructor keywords.

2. PROFILES — one actor run with all candidate profile URLs.
              resultsType=details → bio, follower count.
              Filters out non-instructors using bio keyword matching.

3. POSTS    — one actor run with confirmed instructor profile URLs.
              resultsType=posts, resultsLimit=<post-limit>.
              Parses captions for class schedule, price, style.

Outputs
-------
  data/instructors/instructors_ig.json   — instructor records (merged into main)
  data/classes/classes_raw.json          — class schedule records

Usage
-----
  # Full pipeline (requires APIFY_TOKEN)
  APIFY_TOKEN=xxx python scripts/scrape_instagram_apify.py --mode all

  # Profile harvest only (use known handles from studio enrichment)
  APIFY_TOKEN=xxx python scripts/scrape_instagram_apify.py \\
    --mode profiles --handles-file data/studios/studios_enriched.json

  # Estimate cost without running
  python scripts/scrape_instagram_apify.py --estimate

  # Dry-run: show what would be sent to Apify
  python scripts/scrape_instagram_apify.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _get_apify_client(token: str):
    """Lazy import of ApifyClient so --estimate / --dry-run work without the package."""
    try:
        from apify_client import ApifyClient  # noqa: PLC0415
    except ImportError:
        print("ERROR: pip install apify-client", file=sys.stderr)
        sys.exit(1)
    return ApifyClient(token)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTOR_ID  = "apify/instagram-scraper"
PRICE_PER_1K = 1.50  # USD, as of May 2026

# ── Hashtags for instructor discovery ────────────────────────────────────────

BASE_HASHTAGS = [
    "요가강사", "요가코치", "요가클래스", "요가스케줄",
    "요가레슨", "요가수업", "서울요가", "빈야사요가강사",
    "아쉬탕가요가", "핫요가강사", "음요가", "임산부요가", "요가자격증",
]
CITY_HASHTAGS = [f"{c}요가" for c in [
    "서울", "부산", "대구", "인천", "광주", "대전", "수원",
    "성남", "고양", "용인", "청주", "전주", "안양", "천안",
]]
ALL_HASHTAGS = BASE_HASHTAGS + CITY_HASHTAGS

# ── Instructor bio filter ─────────────────────────────────────────────────────

INSTRUCTOR_BIO_KEYWORDS = [
    "요가강사", "요가선생", "요가코치", "yoga instructor", "yoga teacher",
    "RYT", "E-RYT", "YACEP", "yoga alliance",
    "빈야사", "아쉬탕가", "하타", "쿤달리니",
    "수련", "스튜디오", "클래스 문의",
]
INSTRUCTOR_CAPTION_KEYWORDS = [
    "강사", "코치", "선생", "클래스", "수업", "스케줄", "레슨",
]

# ── Caption parsing (reused from scrape_instagram.py) ────────────────────────

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
DAY_PATTERN = re.compile(
    r"(?:월|화|수|목|금|토|일)요일|(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?",
    re.IGNORECASE,
)
DAY_KR = {"월": "mon", "화": "tue", "수": "wed", "목": "thu",
          "금": "fri", "토": "sat", "일": "sun"}
TIME_PATTERN = re.compile(
    r"(?:오전|오후|아침|저녁|낮)?\s*(\d{1,2})시(?:\s*(\d{2})분)?|(\d{1,2}):(\d{2})\s*(?:AM|PM)?",
    re.IGNORECASE,
)
PRICE_PATTERN = re.compile(r"(\d+(?:,\d{3})*)\s*원|(\d+)\s*만\s*원", re.IGNORECASE)
CONTRAINDICATION_KEYWORDS = {
    "back_pain":    ["허리", "척추", "디스크"],
    "knee_pain":    ["무릎", "관절"],
    "pregnancy":    ["임산부", "임신", "산전", "산후"],
    "hypertension": ["고혈압", "혈압"],
    "injury":       ["부상", "통증"],
}


def parse_styles(text: str) -> list[str]:
    tl = text.lower()
    return sorted({canon for kw, canon in STYLE_MAP.items() if kw in tl})


def parse_schedule_from_caption(caption: str) -> list[dict]:
    if not caption:
        return []
    slots = []
    styles = parse_styles(caption)
    for m in TIME_PATTERN.finditer(caption):
        hour   = int(m.group(1) or m.group(3) or 0)
        minute = int(m.group(2) or m.group(4) or 0)
        pre = caption[max(0, m.start() - 15): m.start()]
        if "오후" in pre or "저녁" in pre:
            if hour < 12:
                hour += 12
        window    = caption[max(0, m.start() - 50): m.end() + 50]
        days_found = []
        for dm in DAY_PATTERN.finditer(window):
            raw = dm.group(0)
            for kr, en in DAY_KR.items():
                if raw.startswith(kr):
                    days_found.append(en)
                    break
            else:
                days_found.append(raw[:3].lower())
        price = None
        for pm in PRICE_PATTERN.finditer(window):
            price = int(pm.group(2)) * 10000 if pm.group(2) else int(pm.group(1).replace(",", ""))
            break
        slots.append({"time": f"{hour:02d}:{minute:02d}", "days": days_found,
                      "styles": styles, "price_krw": price})
    if not slots and styles:
        slots.append({"time": None, "days": [], "styles": styles, "price_krw": None})
    return slots


def parse_contraindications(text: str) -> list[str]:
    return [flag for flag, kws in CONTRAINDICATION_KEYWORDS.items() if any(k in text for k in kws)]


def _extract_cert(bio: str) -> str | None:
    for cert in ["E-RYT-500", "E-RYT-200", "RYT-500", "RYT-200", "YACEP"]:
        if cert in bio.upper():
            return cert
    return None


def _extract_city(bio: str) -> str | None:
    cities = ["서울", "부산", "대구", "인천", "광주", "대전", "수원", "울산",
              "성남", "고양", "용인", "창원", "청주", "전주", "안산", "안양", "천안"]
    return next((c for c in cities if c in bio), None)


def _looks_like_instructor(bio: str) -> bool:
    bl = bio.lower()
    return any(kw.lower() in bl for kw in INSTRUCTOR_BIO_KEYWORDS)


# ── Apify runner ──────────────────────────────────────────────────────────────

def run_actor(client, run_input: dict, label: str,
              dry_run: bool = False) -> list[dict]:
    """
    Run the Instagram Scraper actor with `run_input` and return all dataset items.
    In dry-run mode, print the input and return an empty list.
    """
    n_urls    = len(run_input.get("directUrls", []))
    limit     = run_input.get("resultsLimit") or run_input.get("searchLimit") or "?"
    rtype     = run_input.get("resultsType") or run_input.get("searchType") or "?"
    est_items = n_urls * (limit if isinstance(limit, int) else 1)
    est_cost  = est_items / 1000 * PRICE_PER_1K

    log.info("[%s] %d URL(s), resultsType=%s, limit=%s → ~%d items (~$%.2f)",
             label, n_urls, rtype, limit, est_items, est_cost)

    if dry_run:
        log.info("[dry-run] Input:\n%s", json.dumps(run_input, indent=2, ensure_ascii=False))
        return []

    run = client.actor(ACTOR_ID).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]
    items = list(client.dataset(dataset_id).iterate_items())
    log.info("[%s] ← %d items (dataset %s, cost $%.4f)",
             label, len(items), dataset_id, run.get("usageTotalUsd") or 0)
    return items


# ── Hashtag URL helper ────────────────────────────────────────────────────────

def hashtag_url(tag: str) -> str:
    """Build the Instagram explore URL for a hashtag (handles Korean chars)."""
    return f"https://www.instagram.com/explore/tags/{urllib.parse.quote(tag, safe='')}/"


def profile_url(handle: str) -> str:
    handle = handle.lstrip("@")
    return f"https://www.instagram.com/{handle}/"


# ── Phase 1: Hashtag discovery ────────────────────────────────────────────────

def phase_hashtag(client, hashtags: list[str],
                  hashtag_limit: int, dry_run: bool) -> set[str]:
    """
    One actor run across all hashtags.  Returns set of candidate handles.
    """
    direct_urls = [hashtag_url(ht) for ht in hashtags]
    run_input = {
        "directUrls":   direct_urls,
        "resultsType":  "posts",
        "resultsLimit": hashtag_limit,
        "addParentData": True,   # adds dataSource field to distinguish hashtag
    }
    items = run_actor(client, run_input, label="hashtag-discovery",
                      dry_run=dry_run)

    handles: set[str] = set()
    for item in items:
        caption = item.get("caption") or ""
        owner   = item.get("ownerUsername") or item.get("ownerId") or ""
        if not owner:
            continue
        if any(kw in caption for kw in INSTRUCTOR_CAPTION_KEYWORDS):
            handles.add(owner)

    log.info("Phase 1 → %d candidate instructor handles from %d hashtags",
             len(handles), len(hashtags))
    return handles


# ── Phase 2: Profile details ──────────────────────────────────────────────────

def phase_profiles(client, handles: list[str],
                   batch_size: int, dry_run: bool) -> list[dict]:
    """
    One (or more) actor runs for profile detail lookup.
    Returns list of confirmed instructor dicts.
    """
    instructors: list[dict] = []
    # Batch into chunks to avoid actor timeout on very large lists
    for i in range(0, max(len(handles), 1), batch_size):
        chunk = handles[i: i + batch_size]
        run_input = {
            "directUrls":   [profile_url(h) for h in chunk],
            "resultsType":  "details",
            "resultsLimit": 1,
        }
        items = run_actor(client, run_input,
                          label=f"profiles-batch-{i//batch_size + 1}",
                          dry_run=dry_run)
        for item in items:
            bio = item.get("biography") or ""
            if not _looks_like_instructor(bio):
                continue
            handle   = item.get("username") or ""
            inst_id  = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-")
            instructors.append({
                "instructor_id":       inst_id,
                "full_name":           item.get("fullName") or handle,
                "bio":                 bio[:500] or None,
                "instagram_handle":    handle,
                "instagram_url":       item.get("url") or profile_url(handle),
                "instagram_followers": item.get("followersCount") or 0,
                "certification_level": _extract_cert(bio),
                "yoga_alliance_id":    None,
                "lineage_school":      None,
                "lineage_depth":       0,
                "city":                _extract_city(bio),
                "country":             "KR",
                "specialties":         parse_styles(bio),
                "contraindications":   parse_contraindications(bio),
                "avg_rating":          None,
                "review_count":        0,
                "data_source":         "instagram_apify",
                "scraped_at":          datetime.now(timezone.utc).isoformat(),
            })

    log.info("Phase 2 → %d confirmed instructors (from %d candidates)",
             len(instructors), len(handles))
    return instructors


# ── Phase 3: Post scraping for class schedule ─────────────────────────────────

def phase_posts(client, instructors: list[dict],
                post_limit: int, batch_size: int, dry_run: bool) -> list[dict]:
    """
    One (or more) actor runs to fetch recent posts from confirmed instructors.
    Parses captions for class schedule slots.
    Returns list of class dicts.
    """
    classes: list[dict] = []
    handles = [inst["instagram_handle"] for inst in instructors if inst["instagram_handle"]]
    inst_map = {inst["instagram_handle"]: inst for inst in instructors}

    for i in range(0, max(len(handles), 1), batch_size):
        chunk = handles[i: i + batch_size]
        run_input = {
            "directUrls":   [profile_url(h) for h in chunk],
            "resultsType":  "posts",
            "resultsLimit": post_limit,
        }
        items = run_actor(client, run_input,
                          label=f"posts-batch-{i//batch_size + 1}",
                          dry_run=dry_run)
        for item in items:
            caption = item.get("caption") or ""
            handle  = item.get("ownerUsername") or item.get("ownerId") or ""
            post_id = str(item.get("id") or item.get("shortCode") or "")
            inst    = inst_map.get(handle)
            if not inst:
                continue

            slots = parse_schedule_from_caption(caption)
            for slot in slots:
                if not slot["styles"] and not slot["time"]:
                    continue
                class_id = hashlib.md5(
                    f"{handle}:{post_id}:{slot['time']}".encode()
                ).hexdigest()[:12]
                classes.append({
                    "class_id":          class_id,
                    "instructor_id":     inst["instructor_id"],
                    "instructor_handle": handle,
                    "studio_id":         None,
                    "title":             (
                        f"{inst['full_name']} "
                        f"{'/'.join(slot['styles']) or '요가'} 클래스"
                    ),
                    "style":             slot["styles"][0] if slot["styles"] else None,
                    "difficulty":        None,
                    "duration_min":      None,
                    "price_krw":         slot["price_krw"],
                    "schedule": {
                        "days": slot["days"],
                        "time": slot["time"],
                    },
                    "target_outcomes":   [],
                    "contraindications": parse_contraindications(caption),
                    "source_post_id":    post_id,
                    "caption_snippet":   caption[:300],
                    "scraped_at":        datetime.now(timezone.utc).isoformat(),
                })

    log.info("Phase 3 → %d class slots extracted", len(classes))
    return classes


# ── Handle list helpers ───────────────────────────────────────────────────────

def load_handles_from_file(path: Path) -> list[str]:
    """Load instagram handles from a studio enrichment JSON file."""
    if not path.exists():
        log.warning("Handles file not found: %s", path)
        return []
    data = json.loads(path.read_text())
    handles = []
    for entry in data:
        h = entry.get("instagram") or entry.get("instagram_handle") or ""
        h = h.rstrip("/").split("/")[-1].lstrip("@").strip()
        if h and h != "rsrc.php":
            handles.append(h)
    log.info("Loaded %d handles from %s", len(handles), path)
    return handles


def cost_estimate(n_hashtags: int, hashtag_limit: int,
                  n_profiles: int, post_limit: int) -> None:
    phase1 = n_hashtags * hashtag_limit
    phase2 = n_profiles
    phase3 = n_profiles * post_limit
    total  = phase1 + phase2 + phase3
    print(f"\nCost estimate ({ACTOR_ID}, ${PRICE_PER_1K}/1,000 results)")
    print(f"  Phase 1 hashtag discovery : {n_hashtags:3d} × {hashtag_limit:3d}  "
          f"= {phase1:6,d} results  ${phase1/1000*PRICE_PER_1K:.2f}")
    print(f"  Phase 2 profile details   : {n_profiles:3d} × 1    "
          f"= {phase2:6,d} results  ${phase2/1000*PRICE_PER_1K:.2f}")
    print(f"  Phase 3 post harvest      : {n_profiles:3d} × {post_limit:3d}  "
          f"= {phase3:6,d} results  ${phase3/1000*PRICE_PER_1K:.2f}")
    print(f"  {'─'*56}")
    print(f"  Total                                "
          f"= {total:6,d} results  ${total/1000*PRICE_PER_1K:.2f}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["hashtag", "profiles", "posts", "all"],
                    default="all",
                    help="Which phases to run (default: all)")
    ap.add_argument("--hashtags", default="",
                    help="Comma-separated hashtags (default: built-in 27-tag list)")
    ap.add_argument("--hashtag-limit", type=int, default=50,
                    help="Max posts per hashtag page (default: 50)")
    ap.add_argument("--handles", default="",
                    help="Comma-separated Instagram handles to process directly")
    ap.add_argument("--handles-file", type=Path, default=None,
                    help="JSON file with Instagram handles (studio enrichment output)")
    ap.add_argument("--post-limit", type=int, default=20,
                    help="Recent posts to fetch per instructor profile (default: 20)")
    ap.add_argument("--batch-size", type=int, default=50,
                    help="Max directUrls per actor run (default: 50)")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "data",
                    help="Output root directory (default: data/)")
    ap.add_argument("--apify-token", default=os.environ.get("APIFY_TOKEN"),
                    help="Apify API token (env: APIFY_TOKEN)")
    ap.add_argument("--estimate", action="store_true",
                    help="Show cost estimate and exit (no API call)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show Apify inputs without making API calls")
    args = ap.parse_args()

    # ── Hashtag list ─────────────────────────────────────────────────────────
    hashtags = [h.strip() for h in args.hashtags.split(",") if h.strip()] or ALL_HASHTAGS

    # ── Seed handles ─────────────────────────────────────────────────────────
    seed_handles: list[str] = []
    if args.handles:
        seed_handles = [h.strip().lstrip("@") for h in args.handles.split(",") if h.strip()]
    if args.handles_file:
        seed_handles += load_handles_from_file(args.handles_file)
    else:
        default_enriched = args.out_dir / "studios" / "studios_enriched.json"
        if default_enriched.exists():
            seed_handles += load_handles_from_file(default_enriched)
    seed_handles = list(dict.fromkeys(seed_handles))  # dedup, preserve order

    # ── Cost estimate ─────────────────────────────────────────────────────────
    est_profiles = max(len(seed_handles), 50)  # conservative estimate
    cost_estimate(len(hashtags), args.hashtag_limit, est_profiles, args.post_limit)
    if args.estimate:
        return

    # ── Auth check ────────────────────────────────────────────────────────────
    if not args.apify_token and not args.dry_run:
        log.error("APIFY_TOKEN not set. Get it from https://console.apify.com/settings/integrations")
        sys.exit(1)

    client = _get_apify_client(args.apify_token or "dry-run-token") if not args.dry_run else None

    instructors: list[dict] = []
    classes:     list[dict] = []

    # ── Phase 1: hashtag discovery ────────────────────────────────────────────
    discovered_handles: set[str] = set(seed_handles)
    if args.mode in ("hashtag", "all"):
        new_handles = phase_hashtag(client, hashtags, args.hashtag_limit, args.dry_run)
        discovered_handles.update(new_handles)

    # ── Phase 2: profile details ──────────────────────────────────────────────
    if args.mode in ("profiles", "all"):
        all_handles = sorted(discovered_handles)
        log.info("Total handles to profile: %d", len(all_handles))
        instructors = phase_profiles(client, all_handles, args.batch_size, args.dry_run)

    # ── Phase 3: post harvest ─────────────────────────────────────────────────
    if args.mode in ("posts", "all") and instructors:
        classes = phase_posts(client, instructors, args.post_limit,
                              args.batch_size, args.dry_run)
    elif args.mode == "posts" and not instructors:
        # posts-only mode: treat seed handles as confirmed instructors
        stub_instructors = [{
            "instructor_id":    re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-"),
            "full_name":        h,
            "instagram_handle": h,
        } for h in sorted(discovered_handles)]
        classes = phase_posts(client, stub_instructors, args.post_limit,
                              args.batch_size, args.dry_run)

    # ── Dedup classes ─────────────────────────────────────────────────────────
    seen: set[str] = set()
    deduped_classes = []
    for cls in classes:
        cid = cls.get("class_id", "")
        if cid not in seen:
            seen.add(cid)
            deduped_classes.append(cls)

    # ── Write outputs ─────────────────────────────────────────────────────────
    if args.dry_run:
        log.info("[dry-run] Would write %d instructors and %d classes",
                 len(instructors), len(deduped_classes))
        return

    if instructors:
        out_dir = args.out_dir / "instructors"
        out_dir.mkdir(parents=True, exist_ok=True)

        ig_path = out_dir / "instructors_ig.json"
        ig_path.write_text(
            json.dumps(instructors, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Wrote %s (%d instructors)", ig_path, len(instructors))

        main_path = out_dir / "instructors_raw.json"
        existing  = json.loads(main_path.read_text()) if main_path.exists() else []
        merged    = {r["instructor_id"]: r for r in existing}
        for r in instructors:
            merged[r["instructor_id"]] = r
        main_path.write_text(
            json.dumps(list(merged.values()), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Merged into %s (%d total)", main_path, len(merged))

    if deduped_classes:
        out_dir = args.out_dir / "classes"
        out_dir.mkdir(parents=True, exist_ok=True)
        cls_path = out_dir / "classes_raw.json"
        cls_path.write_text(
            json.dumps(deduped_classes, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Wrote %s (%d classes)", cls_path, len(deduped_classes))

    log.info("Done. Instructors: %d | Classes: %d", len(instructors), len(deduped_classes))


if __name__ == "__main__":
    main()
