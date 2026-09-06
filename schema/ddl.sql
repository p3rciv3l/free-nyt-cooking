-- free-nyt-cooking SQLite query store DDL.
-- Derived artifact: built only by tools/build_db.py from data/recipes/*.jsonl.
-- Note: recipes carries two denormalized columns (ingredients_text, steps_text)
-- so the FTS5 external-content table + trigger trio below work unchanged.

PRAGMA journal_mode = WAL;

CREATE TABLE recipes (
  slug             TEXT PRIMARY KEY,
  nyt_id           INTEGER NOT NULL UNIQUE,
  url              TEXT NOT NULL,
  name             TEXT NOT NULL,
  author           TEXT NOT NULL DEFAULT '',
  description      TEXT NOT NULL DEFAULT '',
  servings_text    TEXT NOT NULL DEFAULT '',
  servings_min     INTEGER,
  servings_max     INTEGER,
  yield_type       TEXT NOT NULL DEFAULT 'servings',
  prep_minutes     INTEGER,
  cook_minutes     INTEGER,
  total_minutes    INTEGER,
  calories         REAL,
  protein_g        REAL,
  carbohydrates_g  REAL,
  fat_g            REAL,
  saturated_fat_g  REAL,
  unsaturated_fat_g REAL,
  trans_fat_g      REAL,
  cholesterol_mg   REAL,
  sodium_mg        REAL,
  fiber_g          REAL,
  sugar_g          REAL,
  nutrition_source TEXT CHECK (nutrition_source IN ('nyt','computed','mixed')),
  nutrition_basis  TEXT NOT NULL DEFAULT 'per_serving',
  rating_average   REAL,
  rating_count     INTEGER,
  comment_count    INTEGER,
  video_url        TEXT,
  published_at     TEXT,          -- YYYY-MM-DD
  scraped_at       TEXT NOT NULL,
  scraper_version  TEXT NOT NULL,
  ingredients_text TEXT NOT NULL DEFAULT '',  -- denormalized, feeds FTS
  steps_text       TEXT NOT NULL DEFAULT ''   -- denormalized, feeds FTS
);

CREATE INDEX idx_recipes_author        ON recipes(author);
CREATE INDEX idx_recipes_total_minutes ON recipes(total_minutes);
CREATE INDEX idx_recipes_calories      ON recipes(calories);
CREATE INDEX idx_recipes_protein       ON recipes(protein_g);
CREATE INDEX idx_recipes_rating        ON recipes(rating_average);
CREATE INDEX idx_recipes_published     ON recipes(published_at);

CREATE TABLE ingredients (
  slug        TEXT NOT NULL REFERENCES recipes(slug) ON DELETE CASCADE,
  position    INTEGER NOT NULL,
  raw         TEXT NOT NULL,
  grp         TEXT,
  quantity    REAL,
  quantity_max REAL,
  unit        TEXT,
  item        TEXT NOT NULL,     -- include/exclude match key, lowercase
  preparation TEXT,
  optional    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (slug, position)
);
CREATE INDEX idx_ingredients_item ON ingredients(item);

CREATE TABLE steps (
  slug     TEXT NOT NULL REFERENCES recipes(slug) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  text     TEXT NOT NULL,
  PRIMARY KEY (slug, position)
);

CREATE TABLE tags (
  slug     TEXT NOT NULL REFERENCES recipes(slug) ON DELETE CASCADE,
  category TEXT NOT NULL CHECK (category IN ('cuisine','diet','course','meal','occasion','other')),
  tag      TEXT NOT NULL,
  PRIMARY KEY (slug, category, tag)
);
CREATE INDEX idx_tags_tag      ON tags(tag);
CREATE INDEX idx_tags_category ON tags(category, tag);

CREATE TABLE images (
  slug     TEXT NOT NULL REFERENCES recipes(slug) ON DELETE CASCADE,
  url      TEXT NOT NULL,
  alt      TEXT NOT NULL DEFAULT '',
  primary_ INTEGER NOT NULL DEFAULT 0
);

-- Full-text search over the text a human reads.
CREATE VIRTUAL TABLE recipes_fts USING fts5(
  name, description, ingredients_text, steps_text,
  content='recipes', content_rowid='rowid',
  tokenize='porter unicode61'
);

-- Standard FTS5 external-content trigger trio.
CREATE TRIGGER recipes_ai AFTER INSERT ON recipes BEGIN
  INSERT INTO recipes_fts(rowid, name, description, ingredients_text, steps_text)
  VALUES (new.rowid, new.name, new.description, new.ingredients_text, new.steps_text);
END;
CREATE TRIGGER recipes_ad AFTER DELETE ON recipes BEGIN
  INSERT INTO recipes_fts(recipes_fts, rowid, name, description, ingredients_text, steps_text)
  VALUES ('delete', old.rowid, old.name, old.description, old.ingredients_text, old.steps_text);
END;
CREATE TRIGGER recipes_au AFTER UPDATE ON recipes BEGIN
  INSERT INTO recipes_fts(recipes_fts, rowid, name, description, ingredients_text, steps_text)
  VALUES ('delete', old.rowid, old.name, old.description, old.ingredients_text, old.steps_text);
  INSERT INTO recipes_fts(rowid, name, description, ingredients_text, steps_text)
  VALUES (new.rowid, new.name, new.description, new.ingredients_text, new.steps_text);
END;

-- Summary view: the default shape of every list-style tool result.
CREATE VIEW recipe_summary AS
SELECT slug, name, author, url, total_minutes, servings_min, servings_max,
       calories, protein_g, carbohydrates_g, fat_g, nutrition_source,
       rating_average, rating_count, published_at
FROM recipes;
