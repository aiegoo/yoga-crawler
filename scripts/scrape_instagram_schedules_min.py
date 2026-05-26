#!/usr/bin/env python3
"""scrape_instagram_schedules_min.py

Mandatory Instagram schedule ingestion path:
- reads studios.instagram URLs from DB
- fetches recent public posts per handle
- OCRs image posts that look like schedule content
- parses weekly timetable text heuristically
- upserts classes rows linked to studio_id
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import psycopg2
import psycopg2.extras
import requests

try:
    import instaloader  # type: ignore
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore
    _DEPS_OK = True
except Exception:
    _DEPS_OK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_URL = os.getenv("CRAWL_DB_URL", "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl")


def normalize_day(tok: str) -> str | None:
    t = tok.strip().lower()
    mapping = {
        "월": "Mon", "화": "Tue", "수": "Wed", "목": "Thu", "금": "Fri", "토": "Sat", "일": "Sun",
        "mon": "Mon", "monday": "Mon", "tue": "Tue", "tuesday": "Tue", "wed": "Wed", "wednesday": "Wed",
        "thu": "Thu", "thursday": "Thu", "fri": "Fri", "friday": "Fri", "sat": "Sat", "saturday": "Sat",
        "sun": "Sun", "sunday": "Sun",
    }
    return mapping.get(t)


def parse_timetable_from_text(raw_text: str) -> dict[str, Any] | None:
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    if not lines:
        return None

    day_header: list[str] = []
    slots: list[dict[str, Any]] = []

    for line in lines:
        pieces = [p for p in re.split(r"[\s|/\\]+", line) if p]
        day_hits = [normalize_day(p) for p in pieces]
        day_hits = [d for d in day_hits if d]

        if len(day_hits) >= 3 and not re.search(r"\d{1,2}:\d{2}", line):
            day_header = day_hits
            continue

        m = re.search(r"(\d{1,2}:\d{2})\s*[~\-–]\s*(\d{1,2}:\d{2})", line)
        if not m or not day_header:
            continue

        start, end = m.group(1), m.group(2)
        rest = line[m.end():].strip()
        cells = [c.strip() for c in re.split(r"\s{2,}|\t|\|", rest) if c.strip()]
        if not cells:
            continue

        class_map = {day: cells[i] for i, day in enumerate(day_header) if i < len(cells)}
        if class_map:
            slots.append({"start": start, "end": end, "classes": class_map})

    if not slots:
        return None
    return {"days": day_header, "slots": slots}


def looks_like_schedule(raw: str, caption: str) -> bool:
    text = (raw + "\n" + (caption or "")).lower()
    has_time = bool(re.search(r"\d{1,2}:\d{2}", text))
    has_kw = any(k in text for k in ["시간표", "스케줄", "schedule", "timetable", "월", "화", "수", "목", "금"])
    return has_time and has_kw


def extract_handle(instagram_field: str) -> str | None:
    s = (instagram_field or "").strip()
    if not s:
        return None
    s = s.replace("https://", "").replace("http://", "")
    s = s.replace("www.instagram.com/", "").replace("instagram.com/", "")
    s = s.strip("/@")
    if "/" in s:
        s = s.split("/")[0]
    if not s or " " in s:
        return None
    return s


def fetch_image_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.content


def ocr_raw(image_bytes: bytes) -> str:
    img = Image.open(BytesIO(image_bytes))
    return pytesseract.image_to_string(img, lang="kor+eng")


UPSERT_SQL = """
INSERT INTO classes
  (studio_id, source, source_id, title, style, schedule, crawled_at)
VALUES
  (%(studio_id)s, %(source)s, %(source_id)s, %(title)s, %(style)s, %(schedule)s::jsonb, %(crawled_at)s)
ON CONFLICT (source, source_id) DO UPDATE SET
  title = EXCLUDED.title,
  style = COALESCE(EXCLUDED.style, classes.style),
  schedule = EXCLUDED.schedule,
  crawled_at = EXCLUDED.crawled_at
"""


def upsert_rows(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=100)
    conn.commit()
    return len(rows)


def style_from_title(title: str) -> str | None:
    t = (title or "").lower()
    if "아쉬탕가" in t or "ashtanga" in t:
        return "ashtanga"
    if "빈야사" in t or "vinyasa" in t:
        return "vinyasa"
    if "하타" in t or "hatha" in t:
        return "hatha"
    if "필라테스" in t or "pilates" in t:
        return "pilates"
    if "요가" in t or "yoga" in t:
        return "yoga"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default=DB_URL)
    ap.add_argument("--max-studios", type=int, default=30)
    ap.add_argument("--max-posts", type=int, default=8)
    ap.add_argument("--max-images", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not _DEPS_OK:
        raise SystemExit("Missing deps: instaloader/pytesseract/Pillow")

    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT id, name, instagram
            FROM studios
            WHERE instagram IS NOT NULL AND instagram <> ''
            ORDER BY id
            LIMIT %s
            """,
            (args.max_studios,),
        )
        studios = cur.fetchall()

    log.info("Studios with instagram to process: %d", len(studios))

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )

    total_rows = 0

    for s in studios:
        studio_id = s["id"]
        handle = extract_handle(s["instagram"])
        if not handle:
            continue

        try:
            profile = instaloader.Profile.from_username(loader.context, handle)
        except Exception as e:
            log.warning("profile %s fetch failed: %s", handle, e)
            continue

        inserted_for_studio = 0
        checked_posts = 0

        for post in profile.get_posts():
            if checked_posts >= args.max_posts:
                break
            checked_posts += 1
            caption = post.caption or ""

            images = []
            if post.typename == "GraphSidecar":
                for i, node in enumerate(post.get_sidecar_nodes()):
                    if i >= args.max_images:
                        break
                    images.append((i, node.display_url))
            else:
                images.append((0, post.url))

            for img_idx, img_url in images:
                try:
                    raw = ocr_raw(fetch_image_bytes(img_url))
                except Exception:
                    continue

                if not looks_like_schedule(raw, caption):
                    continue

                timetable = parse_timetable_from_text(raw)
                if not timetable:
                    continue

                class_occ: dict[str, list[dict[str, str]]] = {}
                for slot in timetable["slots"]:
                    for day, klass in slot["classes"].items():
                        title = (klass or "").strip()
                        if len(title) < 2:
                            continue
                        class_occ.setdefault(title, []).append(
                            {"day": day, "start": slot["start"], "end": slot["end"]}
                        )

                rows = []
                for title, occ in class_occ.items():
                    source_id = hashlib.md5(f"instagram_{post.shortcode}_{img_idx}_{title}".encode()).hexdigest()[:16]
                    rows.append(
                        {
                            "studio_id": studio_id,
                            "source": "instagram_schedule",
                            "source_id": source_id,
                            "title": title,
                            "style": style_from_title(title),
                            "schedule": json.dumps(
                                {
                                    "type": "weekly_timetable",
                                    "source_post": f"https://www.instagram.com/p/{post.shortcode}/",
                                    "source_image_index": img_idx,
                                    "occurrences": occ,
                                    "days": timetable["days"],
                                }
                            ),
                            "crawled_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )

                if args.dry_run:
                    inserted_for_studio += len(rows)
                else:
                    inserted_for_studio += upsert_rows(conn, rows)

            time.sleep(1.0)

        total_rows += inserted_for_studio
        if inserted_for_studio:
            log.info("studio %s (%s): +%d", studio_id, handle, inserted_for_studio)

    log.info("Done. upserted=%d", total_rows)
    conn.close()


if __name__ == "__main__":
    main()
