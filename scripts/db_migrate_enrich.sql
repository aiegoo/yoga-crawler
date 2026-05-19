-- ============================================================
-- db_migrate_enrich.sql  (v2 — expanded for RAG pipeline)
-- Adds social, rating, review, spatial, facility, and RAG
-- payload columns to all three tables; adds classes table.
--
-- Run: psql -U yogacrawl -d yogacrawl -f /tmp/db_migrate_enrich.sql
-- Idempotent: all ALTER TABLE use IF NOT EXISTS.
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. STUDIOS — social, ratings, location enrichment, RAG payload
-- ─────────────────────────────────────────────────────────────

-- Social & web presence
ALTER TABLE studios ADD COLUMN IF NOT EXISTS website         TEXT;
ALTER TABLE studios ADD COLUMN IF NOT EXISTS instagram       TEXT;
ALTER TABLE studios ADD COLUMN IF NOT EXISTS facebook        TEXT;
ALTER TABLE studios ADD COLUMN IF NOT EXISTS youtube         TEXT;

-- Brand / franchise
ALTER TABLE studios ADD COLUMN IF NOT EXISTS brand_parent    TEXT;

-- Google Places
ALTER TABLE studios ADD COLUMN IF NOT EXISTS google_place_id TEXT;
ALTER TABLE studios ADD COLUMN IF NOT EXISTS rating          NUMERIC(3,2);
ALTER TABLE studios ADD COLUMN IF NOT EXISTS review_count    INT;
ALTER TABLE studios ADD COLUMN IF NOT EXISTS price_level     SMALLINT;      -- 0-4
ALTER TABLE studios ADD COLUMN IF NOT EXISTS reviews         JSONB;         -- [{author,rating,text,time,timestamp}]
ALTER TABLE studios ADD COLUMN IF NOT EXISTS opening_hours   JSONB;         -- {weekday_text:[…], open_now:bool}
ALTER TABLE studios ADD COLUMN IF NOT EXISTS popular_times   JSONB;         -- {Mon:[busy0..23],…}

-- Spatial enrichment
ALTER TABLE studios ADD COLUMN IF NOT EXISTS geohash         TEXT;          -- precision-6, e.g. "wydm6b"
ALTER TABLE studios ADD COLUMN IF NOT EXISTS neighborhood    TEXT;          -- primary district tag
ALTER TABLE studios ADD COLUMN IF NOT EXISTS neighborhood_tags TEXT[];      -- ["강남구","논현동","가로수길"]

-- Amenities / facility props
-- { shower:bool, mat_rental:bool, parking:bool, wheelchair:bool, lockers:bool }
ALTER TABLE studios ADD COLUMN IF NOT EXISTS facility_props  JSONB;

-- RAG vector payload (fed into embedding model)
-- { raw_chunk:str, lineage_tags:[str], injury_exclusion_flags:[str] }
ALTER TABLE studios ADD COLUMN IF NOT EXISTS rag_payload     JSONB;

ALTER TABLE studios ADD COLUMN IF NOT EXISTS enriched_at     TIMESTAMPTZ;

-- Indexes
CREATE UNIQUE INDEX IF NOT EXISTS studios_google_place_id_idx
  ON studios (google_place_id) WHERE google_place_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS studios_rating_idx
  ON studios (rating DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS studios_geohash_idx
  ON studios (geohash) WHERE geohash IS NOT NULL;

CREATE INDEX IF NOT EXISTS studios_neighborhood_trgm
  ON studios USING GIN (neighborhood gin_trgm_ops);

CREATE INDEX IF NOT EXISTS studios_rag_gin
  ON studios USING GIN (rag_payload jsonb_path_ops);

-- Updated sync trigger: includes neighborhood + RAG chunk in search_vec
CREATE OR REPLACE FUNCTION studios_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vec := to_tsvector(
    'simple',
    coalesce(NEW.name, '')            || ' ' ||
    coalesce(NEW.address, '')         || ' ' ||
    coalesce(NEW.road_address, '')    || ' ' ||
    coalesce(NEW.category, '')        || ' ' ||
    coalesce(NEW.neighborhood, '')    || ' ' ||
    coalesce(array_to_string(NEW.neighborhood_tags, ' '), '') || ' ' ||
    coalesce(NEW.instagram, '')       || ' ' ||
    coalesce(NEW.facebook, '')        || ' ' ||
    coalesce(NEW.rag_payload->>'raw_chunk', '')
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


-- ─────────────────────────────────────────────────────────────
-- 2. INSTRUCTORS — bio, lineage, contraindications, RAG payload
-- ─────────────────────────────────────────────────────────────

ALTER TABLE instructors ADD COLUMN IF NOT EXISTS aliases         TEXT[];     -- spiritual/stage names
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS bio_text        TEXT;       -- raw bio for LLM embedding
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS lineage         TEXT[];     -- ["Iyengar","Ashtanga"]
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS tiktok          TEXT;
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS linkedin        TEXT;
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS blog_url        TEXT;
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS years_teaching  SMALLINT;   -- calculated from cert_date
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS cert_date       DATE;       -- first certification date
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS contraindications TEXT[];   -- ["acute_knee_injury"]
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS rag_payload     JSONB;
ALTER TABLE instructors ADD COLUMN IF NOT EXISTS enriched_at     TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS instructors_lineage_gin
  ON instructors USING GIN (lineage);

CREATE INDEX IF NOT EXISTS instructors_contraindications_gin
  ON instructors USING GIN (contraindications);

-- Updated sync: includes bio_text + lineage in search_vec
CREATE OR REPLACE FUNCTION instructors_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vec := to_tsvector(
    'simple',
    coalesce(NEW.name, '')           || ' ' ||
    coalesce(NEW.city, '')           || ' ' ||
    coalesce(NEW.studio_name, '')    || ' ' ||
    coalesce(array_to_string(NEW.specialties, ' '), '') || ' ' ||
    coalesce(array_to_string(NEW.lineage, ' '), '')     || ' ' ||
    coalesce(NEW.bio_text, '')
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS instructors_sync_trgr ON instructors;
CREATE TRIGGER instructors_sync_trgr
  BEFORE INSERT OR UPDATE ON instructors
  FOR EACH ROW EXECUTE FUNCTION instructors_sync();


-- ─────────────────────────────────────────────────────────────
-- 3. ASSOCIATIONS — registry status, RYS accreditation
-- ─────────────────────────────────────────────────────────────

ALTER TABLE associations ADD COLUMN IF NOT EXISTS registry_status    TEXT;      -- active|inactive|suspended
ALTER TABLE associations ADD COLUMN IF NOT EXISTS is_rys             BOOLEAN DEFAULT FALSE;  -- hosts teacher training
ALTER TABLE associations ADD COLUMN IF NOT EXISTS accreditation_level TEXT;     -- RYS-200|RYS-500|RYS-300
ALTER TABLE associations ADD COLUMN IF NOT EXISTS country            TEXT;
ALTER TABLE associations ADD COLUMN IF NOT EXISTS enriched_at        TIMESTAMPTZ;


-- ─────────────────────────────────────────────────────────────
-- 4. CLASSES — curriculum, biometrics, kill-switch filters
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS classes (
  id                SERIAL PRIMARY KEY,
  studio_id         INT REFERENCES studios(id) ON DELETE CASCADE,
  instructor_id     INT REFERENCES instructors(id) ON DELETE SET NULL,
  source            TEXT NOT NULL DEFAULT 'manual',
  source_id         TEXT,

  title             TEXT NOT NULL,
  style             TEXT,               -- Vinyasa|Hatha|Yin|Restorative|Power|Workshop
  difficulty        SMALLINT,           -- 1 (beginner) → 5 (advanced)
  pacing            TEXT,               -- slow|medium|high
  duration_min      INT,
  price             NUMERIC(8,2),

  description       TEXT,               -- raw text → LLM embedding
  target_outcomes   TEXT[],             -- ["hamstring_flexibility","core_stability"]
  contraindications TEXT[],             -- KILL-SWITCH filter flags

  schedule          JSONB,              -- { mon:{open:"06:00",close:"21:00"}, … }
  crawled_at        TIMESTAMPTZ DEFAULT NOW(),
  search_vec        TSVECTOR,

  UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS classes_studio_idx
  ON classes (studio_id);

CREATE INDEX IF NOT EXISTS classes_search_gin
  ON classes USING GIN (search_vec);

CREATE INDEX IF NOT EXISTS classes_outcomes_gin
  ON classes USING GIN (target_outcomes);

-- CRITICAL: GIN index on contraindications for fast kill-switch filtering
CREATE INDEX IF NOT EXISTS classes_contraindications_gin
  ON classes USING GIN (contraindications);

CREATE INDEX IF NOT EXISTS classes_style_difficulty_idx
  ON classes (style, difficulty);

CREATE OR REPLACE FUNCTION classes_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vec := to_tsvector(
    'simple',
    coalesce(NEW.title, '')       || ' ' ||
    coalesce(NEW.style, '')       || ' ' ||
    coalesce(NEW.description, '') || ' ' ||
    coalesce(array_to_string(NEW.target_outcomes, ' '), '') || ' ' ||
    coalesce(NEW.pacing, '')
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS classes_sync_trgr ON classes;
CREATE TRIGGER classes_sync_trgr
  BEFORE INSERT OR UPDATE ON classes
  FOR EACH ROW EXECUTE FUNCTION classes_sync();


-- ─────────────────────────────────────────────────────────────
-- 5. VIEWS
-- ─────────────────────────────────────────────────────────────

-- Ranking: score = rating × ln(review_count + 1)
CREATE OR REPLACE VIEW studios_ranked AS
SELECT
  id,
  name,
  road_address,
  phone,
  website,
  instagram,
  facebook,
  neighborhood,
  neighborhood_tags,
  geohash,
  rating,
  review_count,
  price_level,
  facility_props,
  lng,
  lat,
  CASE
    WHEN rating IS NOT NULL AND review_count > 0
    THEN round((rating * ln(review_count + 1))::numeric, 3)
    ELSE 0
  END AS rank_score,
  enriched_at,
  crawled_at
FROM studios
WHERE lat IS NOT NULL
ORDER BY rank_score DESC;

-- RAG-ready records: studios with populated rag_payload
CREATE OR REPLACE VIEW studios_rag_ready AS
SELECT
  id,
  name,
  geohash,
  lat,
  lng,
  neighborhood,
  rag_payload,
  enriched_at
FROM studios
WHERE rag_payload IS NOT NULL
ORDER BY enriched_at DESC;

-- Kill-switch view: classes safe for a given injury set (example — use in app)
-- Usage: SELECT * FROM classes WHERE NOT (contraindications && ARRAY['acute_knee_injury'])
CREATE OR REPLACE VIEW classes_safe_preview AS
SELECT
  c.id,
  c.title,
  c.style,
  c.difficulty,
  c.pacing,
  c.target_outcomes,
  c.contraindications,
  s.name  AS studio_name,
  i.name  AS instructor_name
FROM classes c
LEFT JOIN studios     s ON s.id = c.studio_id
LEFT JOIN instructors i ON i.id = c.instructor_id;

-- Updated summary
CREATE OR REPLACE VIEW data_summary AS
SELECT 'studios'      AS "table", count(*) AS records FROM studios
UNION ALL
SELECT 'instructors'  AS "table", count(*) AS records FROM instructors
UNION ALL
SELECT 'associations' AS "table", count(*) AS records FROM associations
UNION ALL
SELECT 'classes'      AS "table", count(*) AS records FROM classes;
