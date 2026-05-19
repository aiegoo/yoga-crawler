-- ============================================================
-- yoga-crawler: PostgreSQL search database setup
-- Run once: psql -U postgres -f db_setup.sql
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS postgis;          -- geospatial queries
CREATE EXTENSION IF NOT EXISTS pg_trgm;          -- fuzzy name matching
CREATE EXTENSION IF NOT EXISTS unaccent;         -- accent-insensitive search

-- Custom FTS config that strips accents (works with Korean simple tokenizer)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_ts_config WHERE cfgname = 'simple_unaccent'
  ) THEN
    CREATE TEXT SEARCH CONFIGURATION simple_unaccent (COPY = simple);
  END IF;
END$$;

-- ============================================================
-- 1. studios
-- ============================================================
CREATE TABLE IF NOT EXISTS studios (
  id           SERIAL PRIMARY KEY,
  source       TEXT        NOT NULL,                    -- 'kakao' | 'naver'
  source_id    TEXT,                                    -- platform's unique ID
  name         TEXT        NOT NULL,
  category     TEXT,
  phone        TEXT,
  address      TEXT,
  road_address TEXT,
  lng          DOUBLE PRECISION,
  lat          DOUBLE PRECISION,
  location     GEOGRAPHY(POINT, 4326),                 -- PostGIS point
  place_url    TEXT,
  crawled_at   TIMESTAMPTZ DEFAULT NOW(),
  search_vec   TSVECTOR,                               -- full-text search vector
  UNIQUE (source, source_id)
);

-- Geospatial index (nearest-studio queries)
CREATE INDEX IF NOT EXISTS studios_location_gix
  ON studios USING GIST(location);

-- Full-text search index
CREATE INDEX IF NOT EXISTS studios_search_gin
  ON studios USING GIN(search_vec);

-- Fuzzy name search
CREATE INDEX IF NOT EXISTS studios_name_trgm
  ON studios USING GIN(name gin_trgm_ops);

-- Auto-update search_vec + location on insert/update
CREATE OR REPLACE FUNCTION studios_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vec := to_tsvector(
    'simple',
    coalesce(NEW.name, '') || ' ' ||
    coalesce(NEW.address, '') || ' ' ||
    coalesce(NEW.road_address, '') || ' ' ||
    coalesce(NEW.category, '')
  );
  IF NEW.lng IS NOT NULL AND NEW.lat IS NOT NULL THEN
    NEW.location := ST_MakePoint(NEW.lng, NEW.lat)::GEOGRAPHY;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS studios_sync_trgr ON studios;
CREATE TRIGGER studios_sync_trgr
  BEFORE INSERT OR UPDATE ON studios
  FOR EACH ROW EXECUTE FUNCTION studios_sync();


-- ============================================================
-- 2. instructors
-- ============================================================
CREATE TABLE IF NOT EXISTS instructors (
  id           SERIAL PRIMARY KEY,
  source       TEXT        NOT NULL,
  source_id    TEXT,
  name         TEXT        NOT NULL,
  city         TEXT,
  certifications TEXT[],                              -- ['RYT-200','YACEP']
  studio_name  TEXT,
  website      TEXT,
  instagram    TEXT,
  specialties  TEXT[],
  crawled_at   TIMESTAMPTZ DEFAULT NOW(),
  search_vec   TSVECTOR,
  UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS instructors_search_gin
  ON instructors USING GIN(search_vec);

CREATE INDEX IF NOT EXISTS instructors_name_trgm
  ON instructors USING GIN(name gin_trgm_ops);

CREATE OR REPLACE FUNCTION instructors_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vec := to_tsvector(
    'simple',
    coalesce(NEW.name, '') || ' ' ||
    coalesce(NEW.city, '') || ' ' ||
    coalesce(NEW.studio_name, '') || ' ' ||
    coalesce(array_to_string(NEW.specialties, ' '), '')
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS instructors_sync_trgr ON instructors;
CREATE TRIGGER instructors_sync_trgr
  BEFORE INSERT OR UPDATE ON instructors
  FOR EACH ROW EXECUTE FUNCTION instructors_sync();


-- ============================================================
-- 3. associations
-- ============================================================
CREATE TABLE IF NOT EXISTS associations (
  id           SERIAL PRIMARY KEY,
  source       TEXT        NOT NULL,
  source_id    TEXT,
  name         TEXT        NOT NULL,
  name_en      TEXT,
  org_type     TEXT,                                  -- 'alliance'|'federation'|'cep'
  website      TEXT,
  registration_id TEXT,
  member_count INT,
  cert_levels  TEXT[],                               -- ['RYT-200','RYT-500']
  crawled_at   TIMESTAMPTZ DEFAULT NOW(),
  search_vec   TSVECTOR,
  UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS associations_search_gin
  ON associations USING GIN(search_vec);

CREATE OR REPLACE FUNCTION associations_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vec := to_tsvector(
    'simple',
    coalesce(NEW.name, '') || ' ' ||
    coalesce(NEW.name_en, '') || ' ' ||
    coalesce(NEW.org_type, '')
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS associations_sync_trgr ON associations;
CREATE TRIGGER associations_sync_trgr
  BEFORE INSERT OR UPDATE ON associations
  FOR EACH ROW EXECUTE FUNCTION associations_sync();


-- ============================================================
-- Convenience views
-- ============================================================

-- Studios with human-readable distance (used by app search)
CREATE OR REPLACE VIEW studios_search AS
SELECT
  id,
  source,
  name,
  category,
  phone,
  road_address AS address,
  lng,
  lat,
  place_url,
  crawled_at,
  search_vec
FROM studios
WHERE lat IS NOT NULL AND lng IS NOT NULL;

-- Summary counts
CREATE OR REPLACE VIEW data_summary AS
SELECT 'studios'      AS "table", count(*) AS records FROM studios
UNION ALL
SELECT 'instructors'  AS "table", count(*) AS records FROM instructors
UNION ALL
SELECT 'associations' AS "table", count(*) AS records FROM associations;
