#!/usr/bin/env python3
"""
scrape_ig_profiles.py — Batch Instagram profile harvester for yoga studios/instructors.

Strategy (in order of preference):
  1. Apify (apify/instagram-scraper) — cloud-side, no IP ban risk, costs ~$0.0015/profile
  2. Instaloader + session cookie     — free, rate-limited, requires INSTAGRAM_SESSION_ID
  3. Public oEmbed fallback           — no auth, gives display name + thumbnail only

Input sources:
  A. studios table WHERE instagram IS NOT NULL   — already-known handles
  B. studios table facility_props->>'web_crawled_at' NOT NULL  — web-crawled pages
  C. instructors table WHERE instagram IS NOT NULL
  D. --handles-file  CSV/JSON file with handles

Outputs (merged into DB):
  studios.instagram            confirmed handle (updates if changed)
  studios.facility_props       ig_followers, ig_bio, ig_post_count, ig_verified
  instructors.instagram        confirmed handle
  instructors.rag_payload      ig_followers, ig_bio, ig_recent_tags[]

Usage:
  python scripts/scrape_ig_profiles.py                 # all known handles (instaloader)
  python scripts/scrape_ig_profiles.py --mode apify    # Apify cloud run
  python scripts/scrape_ig_profiles.py --mode oembed   # no auth fallback
  python scripts/scrape_ig_profiles.py --handles @yogaspace_seoul,@abc_yoga
  python scripts/scrape_ig_profiles.py --discover      # mine DB website text for handles
  python scripts/scrape_ig_profiles.py --limit 50 --delay 8

Environment:
  APIFY_TOKEN             Apify API token (apify.com)
  INSTAGRAM_SESSION_ID    IG session cookie (from browser DevTools)
  INSTAGRAM_CSRFTOKEN     IG csrf token
  DATABASE_URL            postgresql://...
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

import httpx
import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DB_URL = os.environ.get("DATABASE_URL",
    "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl")
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")
IG_SESSION   = os.environ.get("INSTAGRAM_SESSION_ID", "")
IG_CSRF      = os.environ.get("INSTAGRAM_CSRFTOKEN", "")

_IG_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,30})', re.I)
_STOP_HANDLES = {"p", "explore", "stories", "reel", "reels", "accounts",
                 "about", "legal", "privacy", "press", "login", "direct"}

# ── oEmbed (no auth) ──────────────────────────────────────────────────────────

def oembed_profile(handle: str) -> dict | None:
    """
    Instagram oEmbed gives very limited public data — just display name
    and profile thumbnail. Works without any auth.
    """
    url = f"https://www.instagram.com/{handle}/"
    try:
        resp = httpx.get(
            "https://www.instagram.com/api/v1/oembed/",
            params={"url": url, "format": "json"},
            timeout=10,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "handle": handle,
                "display_name": data.get("author_name", ""),
                "thumbnail": data.get("thumbnail_url", ""),
                "profile_url": url,
                "method": "oembed",
            }
    except Exception:
        pass
    return None


# ── Instaloader (session cookie) ──────────────────────────────────────────────

def instaloader_profile(handle: str, loader: Any) -> dict | None:
    """Fetch a public profile via instaloader."""
    try:
        import instaloader
        profile = instaloader.Profile.from_username(loader.context, handle)
        return {
            "handle": handle,
            "display_name": profile.full_name,
            "bio": profile.biography,
            "followers": profile.followers,
            "following": profile.followees,
            "post_count": profile.mediacount,
            "is_verified": profile.is_verified,
            "is_business": profile.is_business_account,
            "profile_pic_url": profile.profile_pic_url,
            "external_url": profile.external_url or "",
            "recent_hashtags": _extract_hashtags_from_posts(profile, max_posts=12),
            "method": "instaloader",
        }
    except Exception as exc:
        log.debug("instaloader %s: %s", handle, exc)
        return None


def _extract_hashtags_from_posts(profile: Any, max_posts: int = 12) -> list[str]:
    """Collect unique hashtags from the most recent posts."""
    tags: set[str] = set()
    try:
        for i, post in enumerate(profile.get_posts()):
            if i >= max_posts:
                break
            caption = post.caption or ""
            tags.update(re.findall(r"#([A-Za-z가-힣0-9_]+)", caption))
    except Exception:
        pass
    return sorted(tags)[:30]


def build_instaloader(session_id: str, csrf_token: str) -> Any | None:
    try:
        import instaloader
        L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            post_metadata_txt_pattern="",
            quiet=True,
        )
        if session_id:
            L.context._session.cookies.update({
                "sessionid": session_id,
                "csrftoken": csrf_token or "",
            })
            L.context._session.headers.update({
                "X-CSRFToken": csrf_token or "",
            })
            log.info("Instaloader: using session cookie auth")
        return L
    except ImportError:
        log.warning("instaloader not installed. pip install instaloader")
        return None


# ── Apify ─────────────────────────────────────────────────────────────────────

APIFY_IG_ACTOR = "apify/instagram-scraper"
APIFY_BASE = "https://api.apify.com/v2"


def _apify_actor_path(actor_id: str) -> str:
    # Apify REST endpoints expect actor ID as user~actor-name in the URL path.
    return actor_id.replace("/", "~")


def apify_run_profiles(handles: list[str], post_limit: int = 5) -> list[dict]:
    """
    Run the Apify Instagram Scraper on a list of handles.
    Returns list of profile dicts.
    """
    if not APIFY_TOKEN:
        log.error("APIFY_TOKEN not set. Cannot use Apify.")
        return []

    profile_urls = [f"https://www.instagram.com/{h.lstrip('@')}/" for h in handles]

    run_input = {
        "directUrls": profile_urls,
        "resultsType": "details",
        "resultsLimit": post_limit,
        "addParentData": False,
    }

    actor_path = _apify_actor_path(APIFY_IG_ACTOR)

    log.info("Apify: starting actor run for %d profiles...", len(handles))
    resp = httpx.post(
        f"{APIFY_BASE}/acts/{actor_path}/runs",
        headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
        json={"runInput": run_input},
        timeout=30,
    )
    resp.raise_for_status()
    run_id = resp.json()["data"]["id"]
    log.info("Apify run started: %s", run_id)

    # Poll until finished (max 10 min)
    for attempt in range(60):
        time.sleep(10)
        status_resp = httpx.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
            timeout=10,
        )
        status = status_resp.json()["data"]["status"]
        log.info("  Apify run %s: %s (%d/60)", run_id, status, attempt + 1)
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        log.error("Apify run %s finished with status: %s", run_id, status)
        return []

    # Fetch dataset
    dataset_id = status_resp.json()["data"]["defaultDatasetId"]
    items_resp = httpx.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
        params={"format": "json", "clean": "true"},
        timeout=30,
    )
    items = items_resp.json()
    log.info("Apify: fetched %d items from dataset", len(items))
    return _normalize_apify_items(items)


def _normalize_apify_items(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        username = item.get("username") or item.get("ownerUsername") or ""
        if not username:
            continue
        out.append({
            "handle": username,
            "display_name": item.get("fullName") or item.get("full_name") or "",
            "bio": item.get("biography") or item.get("bio") or "",
            "followers": item.get("followersCount") or item.get("followers_count") or 0,
            "following": item.get("followingCount") or item.get("following_count") or 0,
            "post_count": item.get("postsCount") or item.get("posts_count") or 0,
            "is_verified": item.get("verified") or False,
            "is_business": item.get("businessCategoryName") is not None,
            "profile_pic_url": item.get("profilePicUrl") or "",
            "external_url": item.get("externalUrl") or "",
            "recent_hashtags": _tags_from_apify_posts(item.get("latestPosts", [])),
            "method": "apify",
        })
    return out


def _tags_from_apify_posts(posts: list[dict]) -> list[str]:
    tags: set[str] = set()
    for post in posts:
        caption = post.get("caption") or ""
        tags.update(re.findall(r"#([A-Za-z가-힣0-9_]+)", caption))
    return sorted(tags)[:30]


# ── Discover handles from DB text ─────────────────────────────────────────────

def discover_handles_from_db(conn) -> dict[str, list[int]]:
    """
    Mine studios.facility_props->>'rag_chunk' and studios.website for IG handles.
    Returns {handle: [studio_id, ...]}
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id,
               instagram,
               facility_props->>'rag_chunk' AS rag_chunk,
               rag_payload->>'raw_chunk'    AS raw_chunk
        FROM studios
        WHERE (
            facility_props ? 'rag_chunk'
            OR rag_payload ? 'raw_chunk'
            OR instagram IS NOT NULL
        )
        AND (
            (facility_props->>'ig_followers') IS NULL
        )
    """)
    handle_map: dict[str, list[int]] = {}
    for row in cur.fetchall():
        # Already-known handle
        if row["instagram"]:
            h = row["instagram"].lstrip("@").lower()
            if h not in _STOP_HANDLES:
                handle_map.setdefault(h, []).append(row["id"])
        # Mine from crawled text
        for text_field in (row["rag_chunk"] or "", row["raw_chunk"] or ""):
            for m in _IG_RE.finditer(text_field):
                h = m.group(1).lower().rstrip(".")
                if h not in _STOP_HANDLES and len(h) >= 2:
                    handle_map.setdefault(h, []).append(row["id"])
    cur.close()
    log.info("Discovered %d unique IG handles from DB", len(handle_map))
    return handle_map


def load_handles_from_db(conn) -> dict[str, list[int]]:
    """Load all known handles from studios + instructors tables."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    handle_map: dict[str, list[int]] = {}

    cur.execute("SELECT id, instagram FROM studios WHERE instagram IS NOT NULL AND instagram != ''")
    for row in cur.fetchall():
        h = row["instagram"].lstrip("@").lower()
        if h not in _STOP_HANDLES:
            handle_map.setdefault(h, []).append(row["id"])

    cur.execute("SELECT id, instagram FROM instructors WHERE instagram IS NOT NULL AND instagram != ''")
    for row in cur.fetchall():
        h = row["instagram"].lstrip("@").lower()
        if h not in _STOP_HANDLES:
            handle_map.setdefault(h, []).append(row["id"])

    cur.close()
    return handle_map


# ── DB write-back ─────────────────────────────────────────────────────────────

def upsert_ig_data(conn, profile: dict, studio_ids: list[int]) -> None:
    cur = conn.cursor()
    ig_patch = {
        "ig_followers":  profile.get("followers"),
        "ig_bio":        profile.get("bio", "")[:500],
        "ig_post_count": profile.get("post_count"),
        "ig_verified":   profile.get("is_verified", False),
        "ig_fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if profile.get("recent_hashtags"):
        ig_patch["ig_hashtags"] = profile["recent_hashtags"]

    for sid in studio_ids:
        cur.execute("""
            UPDATE studios SET
                instagram = %s,
                facility_props = COALESCE(facility_props, '{}'::jsonb) || %s::jsonb,
                enriched_at = NOW()
            WHERE id = %s
        """, (
            profile["handle"],
            json.dumps(ig_patch, ensure_ascii=False),
            sid,
        ))

    conn.commit()
    cur.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Batch IG profile harvester for yoga studios")
    p.add_argument("--mode", choices=["instaloader", "apify", "oembed"], default="instaloader",
                   help="Scraping backend (default: instaloader)")
    p.add_argument("--handles", default=None,
                   help="Comma-separated @handle list (skips DB lookup)")
    p.add_argument("--handles-file", type=Path, default=None,
                   help="JSON/CSV file with handles column")
    p.add_argument("--discover", action="store_true",
                   help="Mine DB web-crawled text for additional handles")
    p.add_argument("--limit", type=int, default=500,
                   help="Max profiles to fetch per run (default 500)")
    p.add_argument("--delay", type=float, default=8.0,
                   help="Seconds between instaloader requests (default 8)")
    p.add_argument("--apify-post-limit", type=int, default=5,
                   help="Posts per profile to fetch in Apify mode")
    p.add_argument("--dry-run", action="store_true",
                   help="Print without writing to DB")
    args = p.parse_args()

    conn = psycopg2.connect(DB_URL)

    # ── Collect handles ────────────────────────────────────────────────────────
    if args.handles:
        raw_handles = [h.strip().lstrip("@") for h in args.handles.split(",")]
        handle_map = {h: [] for h in raw_handles}
    elif args.handles_file:
        raw = json.loads(args.handles_file.read_text())
        if isinstance(raw, list):
            handle_map = {}
            for item in raw:
                h = (item.get("instagram") or item.get("handle") or "").lstrip("@")
                if h and h not in _STOP_HANDLES:
                    handle_map[h] = []
        else:
            handle_map = {k: [] for k in raw}
    else:
        handle_map = load_handles_from_db(conn)

    if args.discover:
        discovered = discover_handles_from_db(conn)
        for h, ids in discovered.items():
            if h not in handle_map:
                handle_map[h] = ids

    handles = list(handle_map.keys())[:args.limit]
    log.info("Targeting %d Instagram profiles (mode=%s)", len(handles), args.mode)

    if not handles:
        log.warning("No handles found. Run --discover or populate instagram column first.")
        conn.close()
        return

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if args.mode == "apify":
        results = apify_run_profiles(handles, post_limit=args.apify_post_limit)
        result_map = {r["handle"]: r for r in results}

    elif args.mode == "instaloader":
        loader = build_instaloader(IG_SESSION, IG_CSRF)
        if not loader:
            log.error("Instaloader unavailable. Try --mode oembed or --mode apify")
            conn.close()
            return
        result_map = {}
        for i, handle in enumerate(handles):
            log.info("[%d/%d] @%s", i + 1, len(handles), handle)
            data = instaloader_profile(handle, loader)
            if data:
                result_map[handle] = data
            sleep_s = args.delay * (0.7 + random.random() * 0.6)
            time.sleep(sleep_s)

    else:  # oembed
        result_map = {}
        for handle in handles:
            data = oembed_profile(handle)
            if data:
                result_map[handle] = data
            time.sleep(0.5)

    log.info("Fetched %d/%d profiles", len(result_map), len(handles))

    # ── Write back ────────────────────────────────────────────────────────────
    if args.dry_run:
        print(json.dumps(
            {h: v for h, v in list(result_map.items())[:5]},
            ensure_ascii=False, indent=2
        ))
    else:
        for handle, profile in result_map.items():
            ids = handle_map.get(handle, [])
            upsert_ig_data(conn, profile, ids)
        log.info("Upserted %d IG profiles into DB", len(result_map))

    conn.close()

    # Summary
    followers = [v.get("followers", 0) for v in result_map.values() if v.get("followers")]
    if followers:
        log.info("Follower stats: min=%d  median=%d  max=%d",
                 min(followers),
                 sorted(followers)[len(followers)//2],
                 max(followers))


if __name__ == "__main__":
    main()
