#!/usr/bin/env python3
"""
export_to_sheets.py — Export crawled yoga data to a Google Sheet.

Each DB table becomes one worksheet tab:
  • Studios      — 1893+ rows, key business fields
  • Instructors  — 25 rows
  • Associations — 7 rows

Setup (one-time):
  1. Go to https://console.cloud.google.com/
     → Create a project → Enable "Google Sheets API" and "Google Drive API"
  2. IAM & Admin → Service Accounts → Create service account
     → Add role: "Editor" → Create key (JSON) → Download
  3. Open your Google Sheet → Share it with the service account email
     (e.g. export-bot@my-project.iam.gserviceaccount.com)
  4. On the crawler:
       cp /path/to/downloaded-key.json ~/yoga-crawler/.gcp-credentials.json
  5. Set SHEET_ID env var (the long ID from the sheet URL):
       export SHEET_ID="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

Run:
  cd ~/yoga-crawler
  SHEET_ID=<your-id> ~/venv/bin/python scripts/export_to_sheets.py

Optional env vars:
  DATABASE_URL    — PostgreSQL DSN (default: postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl)
  CREDENTIALS     — path to GCP JSON key (default: ~/yoga-crawler/.gcp-credentials.json)
  SHEET_ID        — Google Sheet ID (required)
"""

import os
import sys
import json
import logging
import pathlib
from datetime import datetime, timezone

import psycopg2
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = pathlib.Path(__file__).parent
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://yogacrawl:yogacrawl@localhost:5432/yogacrawl")
CREDENTIALS  = os.environ.get("CREDENTIALS", str(SCRIPT_DIR.parent / ".gcp-credentials.json"))
SHEET_ID     = os.environ.get("SHEET_ID", "")
SCOPES       = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# ── Column selection per table ────────────────────────────────────────────────
TABLES = {
    "studios": {
        "cols": [
            "id", "name", "category", "phone",
            "address", "road_address", "lat", "lng",
            "website", "instagram", "facebook", "youtube",
            "rating", "review_count", "price_level",
            "neighborhood", "source", "place_url", "crawled_at",
        ],
        "order": "name",
    },
    "instructors": {
        "cols": [
            "id", "name", "city", "studio_name",
            "certifications", "specialties", "years_teaching",
            "instagram", "website", "linkedin", "tiktok",
            "source", "crawled_at",
        ],
        "order": "name",
    },
    "associations": {
        "cols": [
            "id", "name", "name_en", "org_type",
            "country", "website", "member_count",
            "cert_levels", "registry_status", "is_rys",
            "accreditation_level", "source", "crawled_at",
        ],
        "order": "name",
    },
    "classes": {
        "cols": [
            "id", "studio_id", "instructor_id",
            "title", "style", "difficulty", "pacing",
            "duration_min", "price", "description",
            "source", "crawled_at",
        ],
        "order": "title",
    },
}


def get_db_rows(cur, table: str, cols: list[str], order: str) -> list[list]:
    safe_cols = ", ".join(f'"{c}"' for c in cols)
    cur.execute(f'SELECT {safe_cols} FROM "{table}" ORDER BY "{order}"')
    rows = cur.fetchall()
    return [list(r) for r in rows]


def serialize_row(row: list) -> list:
    """Convert non-JSON-serializable types to strings."""
    out = []
    for v in row:
        if v is None:
            out.append("")
        elif isinstance(v, (dict, list)):
            out.append(json.dumps(v, ensure_ascii=False))
        elif hasattr(v, "isoformat"):
            out.append(v.isoformat())
        else:
            out.append(str(v))
    return out


def write_sheet(gc: gspread.Client, sheet_id: str, title: str, headers: list[str], rows: list[list]) -> None:
    sh = gc.open_by_key(sheet_id)

    # Get or create worksheet
    try:
        ws = sh.worksheet(title)
        ws.clear()
        log.info("Cleared existing worksheet: %s", title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(len(rows) + 5, 100), cols=len(headers))
        log.info("Created new worksheet: %s", title)

    # Build all data at once to minimise API calls
    all_data = [headers] + [serialize_row(r) for r in rows]
    ws.update(all_data, value_input_option="RAW")

    # Bold the header row
    ws.format("1:1", {"textFormat": {"bold": True}})
    log.info("  ✓ %s: wrote %d rows × %d cols", title, len(rows), len(headers))


def main() -> None:
    if not SHEET_ID:
        log.error("SHEET_ID env var is required. Set it to the Google Sheet ID from the URL.")
        sys.exit(1)

    if not pathlib.Path(CREDENTIALS).exists():
        log.error("Credentials file not found: %s", CREDENTIALS)
        log.error("Follow the setup instructions at the top of this script.")
        sys.exit(1)

    # ── Connect to PostgreSQL ──────────────────────────────────────────────────
    log.info("Connecting to PostgreSQL...")
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()
    log.info("Connected.")

    # ── Authenticate with Google ───────────────────────────────────────────────
    log.info("Authenticating with Google Sheets API...")
    creds = Credentials.from_service_account_file(CREDENTIALS, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    log.info("Authenticated.")

    # ── Write a metadata tab ───────────────────────────────────────────────────
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        sh = gc.open_by_key(SHEET_ID)
        try:
            meta_ws = sh.worksheet("_meta")
            meta_ws.clear()
        except gspread.exceptions.WorksheetNotFound:
            meta_ws = sh.add_worksheet(title="_meta", rows=10, cols=3)

        meta_ws.update([
            ["YogaQ Crawl Export"],
            ["Exported at", exported_at],
            ["Source", "EC2 crawler (team11_test) — yogacrawl PostgreSQL"],
            ["Tables", ", ".join(TABLES.keys())],
        ])
        log.info("Wrote _meta tab")
    except Exception as e:
        log.warning("Could not write _meta tab: %s", e)

    # ── Export each table ──────────────────────────────────────────────────────
    for table, cfg in TABLES.items():
        cols = cfg["cols"]
        # Filter to only columns that actually exist in the table
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table,)
        )
        existing = {r[0] for r in cur.fetchall()}
        cols = [c for c in cols if c in existing]

        rows = get_db_rows(cur, table, cols, cfg["order"])
        log.info("Fetched %d rows from %s", len(rows), table)

        title = table.capitalize()
        write_sheet(gc, SHEET_ID, title, cols, rows)

    conn.close()
    log.info("Done. Open your sheet: https://docs.google.com/spreadsheets/d/%s", SHEET_ID)


if __name__ == "__main__":
    main()
