#!/usr/bin/env python3
"""
scrape_enrich.py — Enrich yoga studio records for RAG + ranking pipeline.

Enrichment sources
------------------
1. Google Places API  — rating, review_count, reviews, website, opening_hours,
                        popular_times, facility props, wheelchair access
2. Kakao place page   — scrapes HTML for instagram/facebook/website links
3. Naver search       — "{name} 인스타그램" / "{name} 페이스북" link discovery

Outputs per studio
------------------
  - Google Places: rating, review_count, reviews[], opening_hours, price_level,
                   website, google_place_id, facility_props
  - Social links:  instagram, facebook, youtube
  - Spatial:       geohash (precision-6), neighborhood, neighborhood_tags[]
  - RAG payload:   { raw_chunk, lineage_tags, injury_exclusion_flags }
  - Timestamp:     enriched_at

Data sinks
----------
  data/studios/studios_enriched.json   — full enriched records (JSON array)
  PostgreSQL studios table             — enriched columns updated in place

Usage
-----
  python scripts/scrape_enrich.py                    # all sources, all studios
  python scripts/scrape_enrich.py --source google    # Google Places only
  python scripts/scrape_enrich.py --source social    # social links only
  python scripts/scrape_enrich.py --limit 10         # first 10 unenriched
  python scripts/scrape_enrich.py --dry-run          # print without writing

Environment
-----------
  GOOGLE_PLACES_API_KEY   Google Cloud project with Places API (New) enabled
  NAVER_CLIENT_ID         Naver Open API — 검색 (Web Search)
  NAVER_CLIENT_SECRET
  DATABASE_URL            postgresql://yogacrawl:yogacrawl@localhost/yogacrawl
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR   = REPO_ROOT / "data" / "studios"

# ── API config ────────────────────────────────────────────────────────────────
GOOGLE_KEY   = os.environ.get("GOOGLE_PLACES_API_KEY", "")
NAVER_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

GOOGLE_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
GOOGLE_DETAIL_URL = "https://maps.googleapis.com/maps/api/place/details/json"
NAVER_SEARCH_URL  = "https://openapi.naver.com/v1/search/webkr.json"

GOOGLE_DETAIL_FIELDS = ",".join([
    "place_id", "name", "website", "formatted_phone_number",
    "rating", "user_ratings_total", "price_level",
    "reviews", "opening_hours", "business_status", "url",
    "wheelchair_accessible_entrance",
    "types",
])

# Social regex patterns
_IG_RE   = re.compile(r'instagram\.com/([A-Za-z0-9_.]+)', re.I)
_FB_RE   = re.compile(r'facebook\.com/([A-Za-z0-9_./-]+)', re.I)
_YT_RE   = re.compile(r'youtube\.com/(channel|@|user)/([A-Za-z0-9_\-]+)', re.I)
_URL_RE  = re.compile(r'https?://[^\s"\'<>]+', re.I)

# Korean address district extraction
# e.g. "서울특별시 강남구 논현동 12-3" → ["강남구", "논현동"]
_ADDR_DISTRICT_RE = re.compile(r'([가-힣]+(?:시|구|군|동|읍|면|로|길))', re.U)

# ── Geohash (pure-Python, no extra dependency) ────────────────────────────────
_GH_CHARS = "0123456789bcdefghjkmnpqrstuvwxyz"

def compute_geohash(lat: float, lng: float, precision: int = 6) -> str:
    """Encode lat/lng to a geohash string of given precision."""
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    bits = [16, 8, 4, 2, 1]
    bit_idx = 0
    char_idx = 0
    chars: list[str] = []
    is_lng = True

    while len(chars) < precision:
        for bit in bits:
            if is_lng:
                mid = (lng_range[0] + lng_range[1]) / 2
                if lng >= mid:
                    char_idx |= bit
                    lng_range[0] = mid
                else:
                    lng_range[1] = mid
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if lat >= mid:
                    char_idx |= bit
                    lat_range[0] = mid
                else:
                    lat_range[1] = mid
            is_lng = not is_lng

        chars.append(_GH_CHARS[char_idx])
        char_idx = 0

    return "".join(chars)


# ── Neighborhood parser ───────────────────────────────────────────────────────

def parse_neighborhood(road_address: str | None) -> tuple[str | None, list[str]]:
    """
    Extract primary neighborhood and tags from a Korean road address.

    Returns (neighborhood, neighborhood_tags)
    e.g. "서울 강남구 논현동" → ("강남구", ["강남구", "논현동"])
    """
    if not road_address:
        return None, []
    tokens = _ADDR_DISTRICT_RE.findall(road_address)
    # Filter to meaningful granularity: 구/동 level
    tags = [t for t in tokens if t.endswith(("구", "동", "읍", "면"))]
    neighborhood = tags[0] if tags else (tokens[0] if tokens else None)
    return neighborhood, list(dict.fromkeys(tags))  # deduplicate, preserve order


# ── RAG payload builder ───────────────────────────────────────────────────────

def build_rag_payload(studio: dict, google_data: dict, lineage_tags: list[str] | None = None) -> dict:
    """
    Build the RAG-ready payload for vector ingestion.

    Schema matches the spec:
    {
      "studio_id":            str,
      "geo":                  { lat, lon, geohash },
      "search_vector_payload": {
          "raw_chunk":                str,    # fed to embedding model
          "instructor_lineage_tags":  [str],
          "injury_exclusion_flags":   [str],  # kill-switch contraindications
      }
    }
    """
    lat  = studio.get("lat") or 0.0
    lng  = studio.get("lng") or 0.0
    name = studio.get("name", "")
    addr = studio.get("road_address") or studio.get("address") or ""
    neighborhood = studio.get("neighborhood") or ""

    # Build the descriptive raw chunk for embedding
    parts = [f"{name} in {neighborhood or addr}."]

    category = studio.get("category") or google_data.get("business_status", "")
    if category:
        parts.append(f"Category: {category}.")

    website = google_data.get("website") or studio.get("website")
    if website:
        parts.append(f"Website: {website}.")

    if google_data.get("rating"):
        parts.append(
            f"Google rating {google_data['rating']} "
            f"({google_data.get('review_count', 0)} reviews)."
        )

    reviews = google_data.get("reviews") or []
    if reviews:
        top_texts = [rv["text"] for rv in reviews[:3] if rv.get("text")]
        if top_texts:
            parts.append("Recent reviews: " + " | ".join(top_texts[:3]))

    oh = google_data.get("opening_hours") or {}
    weekdays = oh.get("weekday_text") or []
    if weekdays:
        parts.append("Hours: " + "; ".join(weekdays[:3]))

    raw_chunk = " ".join(parts)

    return {
        "studio_id": f"{name.lower().replace(' ', '-')}-{addr[:10].replace(' ', '')}",
        "geo": {
            "lat":     lat,
            "lon":     lng,
            "geohash": compute_geohash(lat, lng, 6) if lat and lng else None,
        },
        "search_vector_payload": {
            "raw_chunk":                raw_chunk,
            "instructor_lineage_tags":  lineage_tags or [],
            "injury_exclusion_flags":   [],   # populated from classes table
        },
    }


# ── Facility props builder ────────────────────────────────────────────────────

def build_facility_props(google_result: dict) -> dict:
    """
    Map Google Places result fields to facility_props JSONB.
    Resolves wheelchair access, price signals, and parking from types/amenities.
    """
    types = google_result.get("types", [])
    props: dict[str, Any] = {
        "wheelchair": bool(google_result.get("wheelchair_accessible_entrance")),
        "parking":    any(t in types for t in ["parking", "car_parking"]),
        "shower":     None,     # not available via Places API — can be scraped from page
        "mat_rental": None,
        "lockers":    None,
    }
    return {k: v for k, v in props.items() if v is not None}


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    url = os.environ.get("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "yogacrawl"),
        user=os.environ.get("PGUSER", "yogacrawl"),
        password=os.environ.get("PGPASSWORD", "yogacrawl"),
    )


def load_studios(conn, limit: int | None) -> list[dict]:
    """Load studios from DB; unenriched records come first."""
    sql = """
        SELECT id, name, road_address, address, lat, lng, place_url, phone, category
        FROM studios
        ORDER BY enriched_at NULLS FIRST, id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def save_enrichment(conn, studio_id: int, data: dict, dry_run: bool) -> None:
    if dry_run:
        log.info("  [DRY-RUN] studio id=%d keys=%s",
                 studio_id, [k for k, v in data.items() if v is not None])
        return

    writeable = [
        "website", "instagram", "facebook", "youtube",
        "brand_parent", "google_place_id",
        "rating", "review_count", "price_level",
        "reviews", "opening_hours", "popular_times",
        "geohash", "neighborhood", "neighborhood_tags",
        "facility_props", "rag_payload",
        "enriched_at",
    ]
    present = {k: data[k] for k in writeable if k in data and data[k] is not None}
    if not present:
        return

    updates = ", ".join(f"{k} = %({k})s" for k in present)
    row = dict(present)
    for jf in ("reviews", "opening_hours", "popular_times", "facility_props", "rag_payload"):
        if isinstance(row.get(jf), (dict, list)):
            row[jf] = psycopg2.extras.Json(row[jf])

    row["id"] = studio_id
    with conn.cursor() as cur:
        cur.execute(f"UPDATE studios SET {updates} WHERE id = %(id)s", row)
    conn.commit()


# ── Google Places ─────────────────────────────────────────────────────────────

def google_text_search(name: str, address: str) -> str | None:
    if not GOOGLE_KEY:
        return None
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(GOOGLE_SEARCH_URL, params={
                "query": f"{name} {address}",
                "key": GOOGLE_KEY,
                "language": "ko",
                "type": "gym",
            })
            r.raise_for_status()
            results = r.json().get("results", [])
            return results[0]["place_id"] if results else None
    except Exception as exc:
        log.warning("Google text search failed for %r: %s", name, exc)
        return None


def google_place_details(place_id: str) -> dict:
    if not GOOGLE_KEY or not place_id:
        return {}
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(GOOGLE_DETAIL_URL, params={
                "place_id": place_id,
                "fields": GOOGLE_DETAIL_FIELDS,
                "key": GOOGLE_KEY,
                "language": "ko",
                "reviews_sort": "newest",
            })
            r.raise_for_status()
            res = r.json().get("result", {})

        enriched: dict[str, Any] = {
            "google_place_id": place_id,
            "website":         res.get("website"),
            "rating":          res.get("rating"),
            "review_count":    res.get("user_ratings_total"),
            "price_level":     res.get("price_level"),
            "facility_props":  build_facility_props(res),
        }

        raw_reviews = res.get("reviews", [])
        if raw_reviews:
            enriched["reviews"] = [
                {
                    "author":    rv.get("author_name"),
                    "rating":    rv.get("rating"),
                    "text":      rv.get("text"),
                    "time":      rv.get("relative_time_description"),
                    "timestamp": rv.get("time"),
                }
                for rv in raw_reviews
            ]

        oh = res.get("opening_hours", {})
        if oh:
            enriched["opening_hours"] = {
                "weekday_text": oh.get("weekday_text", []),
                "open_now":     oh.get("open_now"),
            }

        return enriched

    except Exception as exc:
        log.warning("Google place details failed for %s: %s", place_id, exc)
        return {}


def google_popular_times(place_id: str) -> dict | None:
    """Optional: requires `pip install populartimes` on EC2."""
    try:
        import populartimes  # type: ignore
        data = populartimes.get_id(GOOGLE_KEY, place_id)
        pt = data.get("populartimes")
        if not pt:
            return None
        short = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return {short[i]: entry.get("data", []) for i, entry in enumerate(pt)}
    except ImportError:
        return None
    except Exception as exc:
        log.debug("populartimes failed: %s", exc)
        return None


# ── Social link extraction ────────────────────────────────────────────────────

def _extract_social(text: str) -> dict[str, str | None]:
    ig   = _IG_RE.search(text)
    fb   = _FB_RE.search(text)
    yt   = _YT_RE.search(text)
    urls = _URL_RE.findall(text)
    website = None
    for u in urls:
        if not any(x in u for x in ["instagram", "facebook", "youtube", "kakao", "naver", "google", "t.co"]):
            website = u.split("?")[0].rstrip("/")
            break
    return {
        "instagram": f"https://instagram.com/{ig.group(1)}"            if ig  else None,
        "facebook":  f"https://facebook.com/{fb.group(1)}"             if fb  else None,
        "youtube":   f"https://youtube.com/{yt.group(1)}/{yt.group(2)}" if yt  else None,
        "website":   website,
    }


def scrape_kakao_place(place_url: str) -> dict[str, str | None]:
    if not place_url:
        return {}
    try:
        with httpx.Client(timeout=10, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(place_url)
            if r.status_code != 200:
                return {}
            return _extract_social(r.text)
    except Exception as exc:
        log.debug("Kakao place scrape failed: %s", exc)
        return {}


def naver_social_search(name: str, kind: str) -> str | None:
    if not NAVER_ID or not NAVER_SECRET:
        return None
    kw   = {"instagram": "인스타그램", "facebook": "페이스북"}.get(kind, kind)
    pat  = _IG_RE if kind == "instagram" else _FB_RE
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(NAVER_SEARCH_URL,
                      params={"query": f"{name} {kw}", "display": 5},
                      headers={"X-Naver-Client-Id": NAVER_ID,
                               "X-Naver-Client-Secret": NAVER_SECRET})
            r.raise_for_status()
        for item in r.json().get("items", []):
            text = item.get("description", "") + " " + item.get("link", "")
            m = pat.search(text)
            if m:
                return (f"https://instagram.com/{m.group(1)}" if kind == "instagram"
                        else f"https://facebook.com/{m.group(1)}")
    except Exception as exc:
        log.debug("Naver social search failed: %s", exc)
    return None


# ── Per-studio enrichment ─────────────────────────────────────────────────────

def enrich_studio(studio: dict, sources: list[str], delay: float) -> dict:
    """
    Collect enrichment data for one studio; return new-column dict.
    """
    name    = studio["name"]
    address = studio.get("road_address") or studio.get("address") or ""
    lat     = studio.get("lat") or 0.0
    lng     = studio.get("lng") or 0.0

    result: dict[str, Any] = {"enriched_at": datetime.now(timezone.utc).isoformat()}

    # Spatial fields (no API needed)
    if lat and lng:
        result["geohash"] = compute_geohash(lat, lng, 6)
    neighborhood, nbr_tags = parse_neighborhood(address)
    if neighborhood:
        result["neighborhood"]      = neighborhood
        result["neighborhood_tags"] = nbr_tags

    # Google Places
    google_data: dict[str, Any] = {}
    if "google" in sources and GOOGLE_KEY:
        log.info("  Google: %s", name)
        place_id = google_text_search(name, address)
        if place_id:
            google_data = google_place_details(place_id)
            result.update(google_data)
            pt = google_popular_times(place_id)
            if pt:
                result["popular_times"] = pt
            time.sleep(delay)
        else:
            log.debug("  Google: no place_id for %r", name)

    # Social links
    if "social" in sources:
        social: dict[str, str | None] = {}

        kakao_url = studio.get("place_url") or ""
        if kakao_url:
            log.info("  Kakao social: %s", name)
            kakao_social = scrape_kakao_place(kakao_url)
            social.update({k: v for k, v in kakao_social.items() if v})
            time.sleep(delay * 0.4)

        for kind in ("instagram", "facebook"):
            if not social.get(kind):
                link = naver_social_search(name, kind)
                if link:
                    social[kind] = link
                time.sleep(delay * 0.3)

        for k, v in social.items():
            if v and not result.get(k):
                result[k] = v

    # Build RAG payload (after all sources have been merged)
    studio_with_enrichment = {**studio, **result}
    result["rag_payload"] = build_rag_payload(
        studio_with_enrichment, google_data, lineage_tags=[]
    )

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Enrich studios with ratings, social links, geohash, and RAG payload"
    )
    p.add_argument("--source", choices=["google", "social", "all"], default="all")
    p.add_argument("--limit",   type=int,   default=None,
                   help="Max studios to process (default: all unenriched)")
    p.add_argument("--delay",   type=float, default=1.5,
                   help="Base delay between requests (default 1.5s)")
    p.add_argument("--out-dir", type=Path,  default=OUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args  = parse_args()
    sources = ["google", "social"] if args.source == "all" else [args.source]

    if not GOOGLE_KEY and "google" in sources:
        log.warning("GOOGLE_PLACES_API_KEY not set — Google enrichment skipped")
        sources = [s for s in sources if s != "google"]
    if not sources:
        log.error("No sources available. Set GOOGLE_PLACES_API_KEY.")
        return

    try:
        conn = get_conn()
        log.info("Connected to PostgreSQL")
    except Exception as exc:
        log.error("DB connection failed: %s", exc)
        return

    studios = load_studios(conn, args.limit)
    log.info("Processing %d studios  sources=%s", len(studios), sources)

    enriched_all: list[dict] = []

    for i, studio in enumerate(studios, 1):
        log.info("[%d/%d] %s", i, len(studios), studio["name"])
        try:
            data = enrich_studio(studio, sources, args.delay)
            save_enrichment(conn, studio["id"], data, args.dry_run)
            enriched_all.append({**studio, **data})
            time.sleep(args.delay + random.uniform(0, 0.5))
        except KeyboardInterrupt:
            log.info("Interrupted — saving progress...")
            break
        except Exception as exc:
            log.warning("Failed to enrich %r: %s", studio["name"], exc)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "studios_enriched.json"
    out_path.write_text(
        json.dumps(enriched_all, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("Wrote %s (%d records)", out_path, len(enriched_all))

    # Summary
    log.info(
        "Results  rating=%d  instagram=%d  facebook=%d  website=%d  geohash=%d  rag=%d",
        sum(1 for s in enriched_all if s.get("rating")),
        sum(1 for s in enriched_all if s.get("instagram")),
        sum(1 for s in enriched_all if s.get("facebook")),
        sum(1 for s in enriched_all if s.get("website")),
        sum(1 for s in enriched_all if s.get("geohash")),
        sum(1 for s in enriched_all if s.get("rag_payload")),
    )

    conn.close()


if __name__ == "__main__":
    main()

Sources
-------
1. Google Places API  — rating, review_count, reviews, website, opening_hours, popular_times
2. Kakao place page   — scrapes place_url HTML for instagram/facebook/website links
3. Naver search       — searches "{name} 인스타그램" / "{name} 페이스북" for social links

Output
------
  data/studios/studios_enriched.json   — full enriched records
  DB studios table                     — upserted in place

Usage
-----
  # Enrich all un-enriched studios
  python scripts/scrape_enrich.py

  # Enrich only Google Places (skip social scraping)
  python scripts/scrape_enrich.py --source google

  # Enrich only social links
  python scripts/scrape_enrich.py --source social

  # Limit to N studios (for testing)
  python scripts/scrape_enrich.py --limit 10 --delay 1.5

  # Dry-run
  python scripts/scrape_enrich.py --dry-run

Environment
-----------
  GOOGLE_PLACES_API_KEY  — Google Cloud project with Places API enabled
  NAVER_CLIENT_ID        — Naver Search API
  NAVER_CLIENT_SECRET
  DATABASE_URL           — postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT  = Path(__file__).resolve().parent.parent
OUT_DIR    = REPO_ROOT / "data" / "studios"
OUT_JSON   = OUT_DIR / "studios_enriched.json"

# ── API config ────────────────────────────────────────────────────────────────
GOOGLE_KEY    = os.environ.get("GOOGLE_PLACES_API_KEY", "")
NAVER_ID      = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET  = os.environ.get("NAVER_CLIENT_SECRET", "")

GOOGLE_SEARCH_URL  = "https://maps.googleapis.com/maps/api/place/textsearch/json"
GOOGLE_DETAIL_URL  = "https://maps.googleapis.com/maps/api/place/details/json"
NAVER_SEARCH_URL   = "https://openapi.naver.com/v1/search/webkr.json"

# Fields to request from Google Place Details
GOOGLE_DETAIL_FIELDS = ",".join([
    "place_id",
    "name",
    "website",
    "formatted_phone_number",
    "rating",
    "user_ratings_total",
    "price_level",
    "reviews",
    "opening_hours",
    "business_status",
    "url",                   # Google Maps link
])

# Social link patterns
_IG_RE  = re.compile(r'instagram\.com/([A-Za-z0-9_.]+)', re.I)
_FB_RE  = re.compile(r'facebook\.com/([A-Za-z0-9_./-]+)', re.I)
_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)


# ── DB ─────────────────────────────────────────────────────────────────────────

def get_conn():
    url = os.environ.get("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "yogacrawl"),
        user=os.environ.get("PGUSER", "yogacrawl"),
        password=os.environ.get("PGPASSWORD", "yogacrawl"),
    )


def load_studios(conn, limit: int | None) -> list[dict]:
    """Load studios from DB, prioritising un-enriched ones."""
    sql = """
        SELECT id, name, road_address, address, lat, lng, place_url, phone
        FROM studios
        ORDER BY enriched_at NULLS FIRST, id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def save_enrichment(conn, studio_id: int, data: dict, dry_run: bool) -> None:
    if dry_run:
        log.info("  [DRY-RUN] Would update studio id=%d: %s",
                 studio_id, {k: v for k, v in data.items() if v is not None})
        return

    fields = [
        "website", "instagram", "facebook", "google_place_id",
        "rating", "review_count", "price_level",
        "reviews", "opening_hours", "popular_times", "enriched_at",
    ]
    updates = ", ".join(f"{f} = %({f})s" for f in fields if f in data)
    if not updates:
        return

    row = {f: data.get(f) for f in fields}
    # Serialise JSONB fields
    for jf in ("reviews", "opening_hours", "popular_times"):
        if isinstance(row.get(jf), (dict, list)):
            row[jf] = psycopg2.extras.Json(row[jf])

    row["id"] = studio_id
    with conn.cursor() as cur:
        cur.execute(f"UPDATE studios SET {updates} WHERE id = %(id)s", row)
    conn.commit()


# ── Google Places ──────────────────────────────────────────────────────────────

def google_text_search(name: str, address: str) -> str | None:
    """Find Google place_id for a studio by name + address."""
    if not GOOGLE_KEY:
        return None
    query = f"{name} {address}"
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(GOOGLE_SEARCH_URL, params={
                "query": query,
                "key": GOOGLE_KEY,
                "language": "ko",
                "type": "gym",
            })
            r.raise_for_status()
            results = r.json().get("results", [])
            if results:
                return results[0]["place_id"]
    except Exception as exc:
        log.warning("Google text search failed for %r: %s", name, exc)
    return None


def google_place_details(place_id: str) -> dict:
    """Fetch Place Details for a given place_id."""
    if not GOOGLE_KEY or not place_id:
        return {}
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(GOOGLE_DETAIL_URL, params={
                "place_id": place_id,
                "fields": GOOGLE_DETAIL_FIELDS,
                "key": GOOGLE_KEY,
                "language": "ko",
                "reviews_sort": "newest",
            })
            r.raise_for_status()
            result = r.json().get("result", {})

        enriched: dict[str, Any] = {
            "google_place_id": place_id,
            "website":         result.get("website"),
            "rating":          result.get("rating"),
            "review_count":    result.get("user_ratings_total"),
            "price_level":     result.get("price_level"),
        }

        # Condense reviews to author + rating + text + relative time
        raw_reviews = result.get("reviews", [])
        if raw_reviews:
            enriched["reviews"] = [
                {
                    "author":    rv.get("author_name"),
                    "rating":    rv.get("rating"),
                    "text":      rv.get("text"),
                    "time":      rv.get("relative_time_description"),
                    "timestamp": rv.get("time"),
                }
                for rv in raw_reviews
            ]

        # Opening hours
        oh = result.get("opening_hours", {})
        if oh:
            enriched["opening_hours"] = {
                "weekday_text": oh.get("weekday_text", []),
                "open_now":     oh.get("open_now"),
            }

        return enriched

    except Exception as exc:
        log.warning("Google place details failed for %s: %s", place_id, exc)
        return {}


def google_popular_times(place_id: str) -> dict | None:
    """
    Fetch popular times via the populartimes library (optional).
    Falls back gracefully if not installed.
    """
    try:
        import populartimes  # type: ignore
        data = populartimes.get_id(GOOGLE_KEY, place_id)
        pt = data.get("populartimes")
        if not pt:
            return None
        # Convert [{name:"Monday", data:[...]}, ...] → {Mon:[...], ...}
        short = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return {short[i]: entry.get("data", []) for i, entry in enumerate(pt)}
    except ImportError:
        return None
    except Exception as exc:
        log.debug("populartimes failed: %s", exc)
        return None


# ── Social link extraction ────────────────────────────────────────────────────

def _extract_social(html_or_text: str) -> dict[str, str | None]:
    ig = _IG_RE.search(html_or_text)
    fb = _FB_RE.search(html_or_text)
    urls = _URL_RE.findall(html_or_text)
    website = None
    for u in urls:
        if not any(x in u for x in ["instagram", "facebook", "kakao", "naver", "google"]):
            website = u.split("?")[0].rstrip("/")
            break
    return {
        "instagram": f"https://instagram.com/{ig.group(1)}" if ig else None,
        "facebook":  f"https://facebook.com/{fb.group(1)}" if fb else None,
        "website":   website,
    }


def scrape_kakao_place(place_url: str, delay: float) -> dict[str, str | None]:
    """Scrape the Kakao place detail page for social links."""
    if not place_url:
        return {}
    try:
        with httpx.Client(timeout=10, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = client.get(place_url)
            if r.status_code != 200:
                return {}
            time.sleep(delay)
            return _extract_social(r.text)
    except Exception as exc:
        log.debug("Kakao place scrape failed: %s", exc)
        return {}


def naver_social_search(name: str, kind: str, delay: float) -> str | None:
    """
    Search Naver Web for "{name} 인스타그램" or "{name} 페이스북".
    Returns the first matching social URL found.
    """
    if not NAVER_ID or not NAVER_SECRET:
        return None
    kw_map = {"instagram": "인스타그램", "facebook": "페이스북"}
    query = f"{name} {kw_map.get(kind, kind)}"
    pattern = _IG_RE if kind == "instagram" else _FB_RE
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(NAVER_SEARCH_URL, params={"query": query, "display": 5},
                           headers={"X-Naver-Client-Id": NAVER_ID,
                                    "X-Naver-Client-Secret": NAVER_SECRET})
            r.raise_for_status()
        time.sleep(delay)
        for item in r.json().get("items", []):
            text = item.get("description", "") + " " + item.get("link", "")
            m = pattern.search(text)
            if m:
                if kind == "instagram":
                    return f"https://instagram.com/{m.group(1)}"
                else:
                    return f"https://facebook.com/{m.group(1)}"
    except Exception as exc:
        log.debug("Naver social search failed: %s", exc)
    return None


# ── Enrich one studio ─────────────────────────────────────────────────────────

def enrich_studio(studio: dict, sources: list[str], delay: float) -> dict:
    """
    Collect enrichment data for one studio.
    Returns a dict with the new fields (None means not found).
    """
    name    = studio["name"]
    address = studio.get("road_address") or studio.get("address") or ""
    result: dict[str, Any] = {"enriched_at": datetime.now(timezone.utc).isoformat()}

    # ── Google Places ──────────────────────────────────────────────────────
    if "google" in sources and GOOGLE_KEY:
        log.info("  Google: %s", name)
        place_id = google_text_search(name, address)
        if place_id:
            gd = google_place_details(place_id)
            result.update(gd)
            pt = google_popular_times(place_id)
            if pt:
                result["popular_times"] = pt
            time.sleep(delay)
        else:
            log.debug("  Google: no place_id found for %r", name)

    # ── Social links ───────────────────────────────────────────────────────
    if "social" in sources:
        social: dict[str, str | None] = {}

        # 1. Try Kakao place page first (has social links in structured HTML)
        kakao_url = studio.get("place_url") or ""
        if kakao_url:
            log.info("  Kakao page: %s", name)
            kakao_social = scrape_kakao_place(kakao_url, delay * 0.5)
            social.update({k: v for k, v in kakao_social.items() if v})

        # 2. Fallback: Naver web search for missing social links
        if not social.get("instagram"):
            ig = naver_social_search(name, "instagram", delay * 0.5)
            if ig:
                social["instagram"] = ig

        if not social.get("facebook"):
            fb = naver_social_search(name, "facebook", delay * 0.5)
            if fb:
                social["facebook"] = fb

        # Merge — don't overwrite Google website if already found
        for k, v in social.items():
            if v and not result.get(k):
                result[k] = v

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enrich studios with ratings, reviews, social links")
    p.add_argument("--source", choices=["google", "social", "all"], default="all")
    p.add_argument("--limit", type=int, default=None,
                   help="Max studios to process (default: all)")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Base delay between requests (default: 1.5s)")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    sources = ["google", "social"] if args.source == "all" else [args.source]

    if not GOOGLE_KEY and "google" in sources:
        log.warning("GOOGLE_PLACES_API_KEY not set — Google enrichment skipped")
        sources = [s for s in sources if s != "google"]

    if not sources:
        log.error("No enrichment sources available. Set GOOGLE_PLACES_API_KEY.")
        return

    try:
        conn = get_conn()
        log.info("Connected to PostgreSQL")
    except Exception as exc:
        log.error("DB connection failed: %s", exc)
        return

    studios = load_studios(conn, args.limit)
    log.info("Enriching %d studios (sources: %s)", len(studios), sources)

    enriched_all: list[dict] = []

    for i, studio in enumerate(studios, 1):
        log.info("[%d/%d] %s", i, len(studios), studio["name"])
        try:
            data = enrich_studio(studio, sources, args.delay)
            save_enrichment(conn, studio["id"], data, args.dry_run)
            enriched_all.append({**studio, **data})

            # Brief jitter to avoid rate limits
            time.sleep(args.delay + random.uniform(0, 0.5))

        except KeyboardInterrupt:
            log.info("Interrupted — saving progress...")
            break
        except Exception as exc:
            log.warning("Failed to enrich %r: %s", studio["name"], exc)
            continue

    # Write enriched JSON
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "studios_enriched.json"
    out_path.write_text(
        json.dumps(enriched_all, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )
    log.info("Wrote %s (%d records)", out_path, len(enriched_all))

    # Summary
    with_rating  = sum(1 for s in enriched_all if s.get("rating"))
    with_ig      = sum(1 for s in enriched_all if s.get("instagram"))
    with_fb      = sum(1 for s in enriched_all if s.get("facebook"))
    with_website = sum(1 for s in enriched_all if s.get("website"))
    log.info("Results: rating=%d  instagram=%d  facebook=%d  website=%d",
             with_rating, with_ig, with_fb, with_website)

    conn.close()


if __name__ == "__main__":
    main()
