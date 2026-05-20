#!/usr/bin/env python3
"""
scrape_github_yoga.py — Mine GitHub for yoga / Yoga Alliance affiliated profiles.

Searches GitHub for:
  1. Users with yoga-related keywords in bio/location/name
  2. Organizations with yoga focus (yoga studios tech teams, YA affiliates)
  3. Repositories: yoga apps, pose detection, yoga scheduling, YA API integrations
  4. Topics: #yoga, #yoga-pose, #yoga-alliance, #meditation, #pilates

Data stored in:
  instructors table  (source='github')   — developers teaching yoga or working in space
  studios table      (source='github')   — organizations with studio-like presence
  A local JSON file  data/github/yoga_github_YYYYMMDD.json

Usage:
  python scripts/scrape_github_yoga.py                   # all search types
  python scripts/scrape_github_yoga.py --type users      # users only
  python scripts/scrape_github_yoga.py --type repos      # repos only
  python scripts/scrape_github_yoga.py --type orgs       # orgs only
  python scripts/scrape_github_yoga.py --dry-run
  python scripts/scrape_github_yoga.py --limit 200

Environment:
  GITHUB_TOKEN    GitHub personal access token (increases rate limit to 5,000/hr)
                  Create at: https://github.com/settings/tokens (no scopes needed)
  DATABASE_URL    postgresql://...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
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
OUT_DIR   = REPO_ROOT / "data" / "github"

DB_URL = os.environ.get("DATABASE_URL",
    "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GH_API = "https://api.github.com"
GH_HEADERS: dict[str, str] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

# ── Search queries ─────────────────────────────────────────────────────────────

USER_QUERIES = [
    # Bio/location searches
    "yoga instructor in:bio",
    "yoga teacher in:bio",
    "yoga alliance in:bio",
    "yogi developer in:bio",
    "yoga studio in:bio",
    "yoga app developer in:bio",
    "meditation yoga in:bio",
    "요가 강사 in:bio",
    "필라테스 in:bio",
    # Location + yoga
    "yoga in:bio location:Seoul",
    "yoga in:bio location:Korea",
    "yoga in:bio location:Busan",
]

ORG_QUERIES = [
    "yoga in:description",
    "yoga alliance in:description",
    "pilates in:description",
    "yoga studio technology in:description",
    "yoga wellness in:description",
    "yogaalliance",     # org name search
    "yogaconnect",
    "mindfulness wellness in:description",
]

REPO_QUERIES = [
    # Pose detection / computer vision
    "yoga pose detection",
    "yoga pose classification",
    "yoga asana recognition",
    "mediapipe yoga",
    "pose estimation yoga",
    # Apps / platforms
    "yoga app",
    "yoga studio management",
    "yoga booking",
    "yoga schedule",
    "yoga class management",
    # Alliance / certification
    "yoga alliance API",
    "yoga instructor certification",
    "yoga teacher training",
    # Korean
    "요가 앱",
    "요가 포즈",
]

TOPIC_QUERIES = [
    "yoga",
    "yoga-pose",
    "yoga-alliance",
    "yoga-app",
    "meditation",
    "pilates",
    "mindfulness",
    "pose-detection",
    "pose-estimation",
    "wellness",
]

# Keywords to determine if a user is yoga-affiliated
YOGA_BIO_KEYWORDS = [
    "yoga", "yogi", "asana", "meditation", "pilates", "mindfulness",
    "要伽", "요가", "필라테스", "명상",
    "yoga alliance", "ryt", "ryt200", "ryt500", "e-ryt",
    "yoga teacher", "yoga instructor", "yoga coach",
    "vinyasa", "ashtanga", "hatha", "kundalini", "iyengar",
]


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def gh_get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{GH_API}{path}"
    resp = client.get(url, params=params, timeout=15)
    # Rate limit handling
    if resp.status_code == 403:
        reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
        wait = max(0, reset - time.time()) + 5
        log.warning("Rate limited. Waiting %.0fs...", wait)
        time.sleep(wait)
        resp = client.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def search_users(client: httpx.Client, query: str, max_pages: int = 3) -> list[dict]:
    users = []
    for page in range(1, max_pages + 1):
        data = gh_get(client, "/search/users", {
            "q": query, "per_page": 30, "page": page,
            "sort": "followers", "order": "desc",
        })
        items = data.get("items", [])
        if not items:
            break
        users.extend(items)
        total = data.get("total_count", 0)
        if page * 30 >= min(total, 300):
            break
        time.sleep(1)
    return users


def search_repos(client: httpx.Client, query: str, max_pages: int = 3) -> list[dict]:
    repos = []
    for page in range(1, max_pages + 1):
        data = gh_get(client, "/search/repositories", {
            "q": query,
            "per_page": 30,
            "page": page,
            "sort": "stars",
            "order": "desc",
        })
        items = data.get("items", [])
        if not items:
            break
        repos.extend(items)
        total = data.get("total_count", 0)
        if page * 30 >= min(total, 300):
            break
        time.sleep(1)
    return repos


def search_orgs(client: httpx.Client, query: str) -> list[dict]:
    data = gh_get(client, "/search/users", {
        "q": f"{query} type:org",
        "per_page": 30,
        "sort": "followers",
    })
    return data.get("items", [])


def search_by_topic(client: httpx.Client, topic: str, max_pages: int = 2) -> list[dict]:
    repos = []
    for page in range(1, max_pages + 1):
        data = gh_get(client, "/search/repositories", {
            "q": f"topic:{topic}",
            "per_page": 30,
            "page": page,
            "sort": "stars",
        })
        items = data.get("items", [])
        if not items:
            break
        repos.extend(items)
        total = data.get("total_count", 0)
        if page * 30 >= min(total, 200):
            break
        time.sleep(0.5)
    return repos


def get_user_detail(client: httpx.Client, login: str) -> dict:
    try:
        return gh_get(client, f"/users/{login}")
    except Exception:
        return {}


def is_yoga_affiliated(user: dict) -> bool:
    """Heuristic: check if user bio/name/location suggests yoga affiliation."""
    text = " ".join([
        user.get("bio") or "",
        user.get("name") or "",
        user.get("company") or "",
        user.get("location") or "",
        user.get("blog") or "",
    ]).lower()
    return any(kw in text for kw in YOGA_BIO_KEYWORDS)


# ── Normalization ──────────────────────────────────────────────────────────────

def user_to_instructor(user: dict) -> dict:
    """Map GitHub user data to instructors table schema."""
    bio = user.get("bio") or ""
    # Detect certifications from bio
    certs = []
    for cert in ["ryt200", "ryt 200", "ryt500", "ryt 500", "e-ryt", "yoga alliance"]:
        if cert.lower() in bio.lower():
            certs.append(cert.upper())

    return {
        "source": "github",
        "source_id": str(user["id"]),
        "name": user.get("name") or user.get("login", ""),
        "city": user.get("location") or "",
        "website": user.get("blog") or "",
        "instagram": None,
        "bio_text": bio[:1000],
        "certifications": certs or None,
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "rag_payload": {
            "github_login":    user.get("login"),
            "github_url":      user.get("html_url"),
            "followers":       user.get("followers", 0),
            "public_repos":    user.get("public_repos", 0),
            "avatar_url":      user.get("avatar_url"),
            "company":         user.get("company"),
            "twitter":         user.get("twitter_username"),
            "source_type":     "github_user",
        },
    }


def org_to_studio(org: dict, detail: dict | None = None) -> dict:
    """Map GitHub org to studios table schema."""
    d = detail or org
    name = d.get("name") or d.get("login", "")
    return {
        "source": "github",
        "source_id": f"org_{d.get('id', '')}",
        "name": name,
        "category": "yoga_tech_org",
        "website": d.get("blog") or "",
        "address": d.get("location") or "",
        "road_address": None,
        "lat": None,
        "lng": None,
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "facility_props": {
            "github_login":  d.get("login"),
            "github_url":    d.get("html_url"),
            "followers":     d.get("followers", 0),
            "public_repos":  d.get("public_repos", 0),
            "avatar_url":    d.get("avatar_url"),
            "description":   d.get("bio") or d.get("description") or "",
            "source_type":   "github_org",
        },
    }


def repo_to_record(repo: dict) -> dict:
    """Map GitHub repo to a flat dict for JSON output."""
    return {
        "full_name":     repo.get("full_name"),
        "html_url":      repo.get("html_url"),
        "description":   repo.get("description") or "",
        "stars":         repo.get("stargazers_count", 0),
        "forks":         repo.get("forks_count", 0),
        "language":      repo.get("language") or "",
        "topics":        repo.get("topics", []),
        "owner_login":   repo.get("owner", {}).get("login"),
        "owner_type":    repo.get("owner", {}).get("type"),
        "pushed_at":     repo.get("pushed_at"),
        "created_at":    repo.get("created_at"),
        "license":       (repo.get("license") or {}).get("spdx_id"),
    }


# ── DB upserts ────────────────────────────────────────────────────────────────

INSTRUCTOR_UPSERT = """
INSERT INTO instructors (
    source, source_id, name, city, website, instagram,
    bio_text, certifications, crawled_at, rag_payload
) VALUES (
    %(source)s, %(source_id)s, %(name)s, %(city)s, %(website)s, %(instagram)s,
    %(bio_text)s, %(certifications)s, %(crawled_at)s, %(rag_payload)s
)
ON CONFLICT (source, source_id) DO UPDATE SET
    name         = EXCLUDED.name,
    city         = EXCLUDED.city,
    website      = EXCLUDED.website,
    bio_text     = EXCLUDED.bio_text,
    certifications = COALESCE(EXCLUDED.certifications, instructors.certifications),
    rag_payload  = EXCLUDED.rag_payload,
    crawled_at   = EXCLUDED.crawled_at
"""

STUDIO_UPSERT = """
INSERT INTO studios (
    source, source_id, name, category, website, address,
    road_address, lat, lng, crawled_at, facility_props
) VALUES (
    %(source)s, %(source_id)s, %(name)s, %(category)s, %(website)s, %(address)s,
    %(road_address)s, %(lat)s, %(lng)s, %(crawled_at)s, %(facility_props)s
)
ON CONFLICT (source, source_id) DO UPDATE SET
    name          = EXCLUDED.name,
    website       = EXCLUDED.website,
    facility_props = EXCLUDED.facility_props,
    crawled_at    = EXCLUDED.crawled_at
"""


def save_to_db(conn, instructors: list[dict], orgs: list[dict]) -> None:
    cur = conn.cursor()
    for rec in instructors:
        rec = dict(rec)
        rec["rag_payload"] = json.dumps(rec["rag_payload"], ensure_ascii=False)
        cur.execute(INSTRUCTOR_UPSERT, rec)
    for rec in orgs:
        rec = dict(rec)
        rec["facility_props"] = json.dumps(rec["facility_props"], ensure_ascii=False)
        cur.execute(STUDIO_UPSERT, rec)
    conn.commit()
    cur.close()
    log.info("Saved %d instructors, %d orgs to DB", len(instructors), len(orgs))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Mine GitHub for yoga profiles + repos")
    p.add_argument("--type", choices=["users", "orgs", "repos", "topics", "all"],
                   default="all", help="What to search (default: all)")
    p.add_argument("--limit", type=int, default=500,
                   help="Max items per search type (default 500)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print results without saving to DB")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(headers=GH_HEADERS, timeout=20)
    conn = psycopg2.connect(DB_URL) if not args.dry_run else None

    all_users: dict[str, dict] = {}   # login → detail dict
    all_orgs:  dict[str, dict] = {}   # login → detail dict
    all_repos: dict[str, dict] = {}   # full_name → repo dict

    # ── Users ──────────────────────────────────────────────────────────────────
    if args.type in ("users", "all"):
        log.info("=== Searching GitHub users ===")
        for query in USER_QUERIES:
            log.info("  query: %s", query)
            try:
                users = search_users(client, query, max_pages=3)
                log.info("    → %d raw results", len(users))
                for u in users:
                    login = u["login"]
                    if login in all_users:
                        continue
                    # Fetch full profile to get bio
                    detail = get_user_detail(client, login)
                    if is_yoga_affiliated(detail):
                        all_users[login] = detail
                    time.sleep(0.3)
            except Exception as e:
                log.warning("User search failed (%s): %s", query, e)
            time.sleep(1.5)
            if len(all_users) >= args.limit:
                break
        log.info("Total yoga-affiliated users: %d", len(all_users))

    # ── Orgs ───────────────────────────────────────────────────────────────────
    if args.type in ("orgs", "all"):
        log.info("=== Searching GitHub organizations ===")
        for query in ORG_QUERIES:
            log.info("  query: %s", query)
            try:
                orgs = search_orgs(client, query)
                for o in orgs:
                    login = o["login"]
                    if login in all_orgs:
                        continue
                    detail = get_user_detail(client, login)
                    all_orgs[login] = detail
                    time.sleep(0.3)
            except Exception as e:
                log.warning("Org search failed (%s): %s", query, e)
            time.sleep(1.5)
        log.info("Total yoga orgs found: %d", len(all_orgs))

    # ── Repos ──────────────────────────────────────────────────────────────────
    if args.type in ("repos", "all"):
        log.info("=== Searching GitHub repositories ===")
        for query in REPO_QUERIES:
            log.info("  query: %s", query)
            try:
                repos = search_repos(client, query, max_pages=2)
                for r in repos:
                    fn = r["full_name"]
                    if fn not in all_repos:
                        all_repos[fn] = r
            except Exception as e:
                log.warning("Repo search failed (%s): %s", query, e)
            time.sleep(2)
        log.info("Total yoga repos found: %d", len(all_repos))

    # ── Topics ─────────────────────────────────────────────────────────────────
    if args.type in ("topics", "all"):
        log.info("=== Searching GitHub topics ===")
        for topic in TOPIC_QUERIES:
            log.info("  topic: #%s", topic)
            try:
                repos = search_by_topic(client, topic)
                for r in repos:
                    fn = r["full_name"]
                    if fn not in all_repos:
                        all_repos[fn] = r
            except Exception as e:
                log.warning("Topic search failed (%s): %s", topic, e)
            time.sleep(1.5)
        log.info("Total repos (incl. topics): %d", len(all_repos))

    client.close()

    # ── Normalize ──────────────────────────────────────────────────────────────
    instructor_records = [user_to_instructor(u) for u in all_users.values()]
    org_records        = [org_to_studio(o) for o in all_orgs.values()]
    repo_records       = [repo_to_record(r) for r in all_repos.values()]

    # Sort repos by stars
    repo_records.sort(key=lambda r: r["stars"], reverse=True)

    # ── Output ─────────────────────────────────────────────────────────────────
    ts = date.today().strftime("%Y%m%d")
    out_path = args.out_dir / f"yoga_github_{ts}.json"

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "users":       len(instructor_records),
            "orgs":        len(org_records),
            "repos":       len(repo_records),
        },
        "users":  instructor_records,
        "orgs":   org_records,
        "repos":  repo_records[:200],  # top 200 by stars
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    log.info("Saved → %s", out_path)

    # ── Print summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("GITHUB YOGA PROFILE MINE RESULTS")
    print("=" * 60)
    print(f"  Yoga-affiliated users: {len(instructor_records)}")
    print(f"  Yoga organizations:    {len(org_records)}")
    print(f"  Yoga repositories:     {len(repo_records)}")
    print()
    if repo_records:
        print("  Top 10 repos by stars:")
        for r in repo_records[:10]:
            print(f"    ⭐ {r['stars']:>5}  {r['full_name']}  "
                  f"[{', '.join(r['topics'][:3])}]")
    print()
    if all_orgs:
        print("  Yoga organizations:")
        for login, o in list(all_orgs.items())[:10]:
            print(f"    @{login}: {o.get('bio') or o.get('description') or ''[:60]}")
    print("=" * 60)

    if args.dry_run:
        log.info("DRY RUN — results printed, not saved to DB.")
        return

    save_to_db(conn, instructor_records, org_records)
    conn.close()


if __name__ == "__main__":
    main()
