# free-nyt-cooking — Canonical Recipe Schema

Two layers, one source of truth:

1. **Canonical record (JSONL)** — one JSON object per recipe, emitted by the scraper,
   sharded on disk. This is the durable, git-tracked data.
2. **Query store (SQLite)** — built from the JSONL by `tools/build_db.py`. Disposable,
   rebuildable, shipped as a release artifact. All agent queries hit this file.

The JSONL record is the contract with the scraper. The SQLite schema is the contract
with agents. Field names are identical across both wherever possible.

---

## 1. Canonical record (JSONL) — field table

| Field | Type | Req | Notes |
|---|---|---|---|
| `slug` | string | ✓ | From the NYT URL: `/recipes/1023456-miso-chicken` → `miso-chicken`. Lowercase, `[a-z0-9-]` (NYT serves a few capitalized slugs; lowercased at merge, `url` keeps source casing). NOT unique: NYT reuses a small number of slugs for distinct recipes, so `nyt_id` is the key. |
| `nyt_id` | integer | ✓ | Primary key. Numeric recipe id from the URL. Stable, unique upstream key; every cross-reference (db tables, CLI lookups) uses it. |
| `url` | string | ✓ | Canonical `https://cooking.nytimes.com/recipes/<nyt_id>-<slug>` |
| `name` | string | ✓ | Display name as published. |
| `author` | string | ✓ | Display name, e.g. `"Melissa Clark"`. Empty string if unattributed (NYT has some). |
| `description` | string | ✓ | Headnote / standfirst. `""` if none. |
| `servings` | object | ✓ | `{ "text": "4 servings", "min": 4, "max": 4 }`. `min`/`max` null when unparseable (e.g. "Makes about 3 cups" → text only). `yield_type`: `"servings" \| "pieces" \| "volume" \| "other"`. |
| `time` | object | ✓ | `{ "prep_minutes": int\|null, "cook_minutes": int\|null, "total_minutes": int\|null, "raw": {"prep": "15 minutes", ...} }`. Keep raw strings; parse to minutes best-effort. `total_minutes` null when NYT gives none. |
| `ingredients` | array | ✓ | See ingredient object below. Order preserved. |
| `steps` | array | ✓ | `[{ "position": 1, "text": "..." }]`. One object per numbered step as published. |
| `nutrition` | object\|null | ✓ | See nutrition object below. Null when no data at all. |
| `tags` | object | ✓ | `{ "cuisine": [...], "diet": [...], "course": [...], "meal": [...], "occasion": [...], "other": [...] }`. All values lowercase slugs (`"gluten-free"`, `"italian"`). Empty arrays fine. NYT's tag facets map: CUISINE→cuisine, DIET→diet, COURSE/MEAL_TYPE→course/meal, OCCASION→occasion; unmapped → other. |
| `images` | array | ✓ | `[{ "url": "...", "alt": "...", "primary": true }]`. URLs only, no binary caching (bare-bones). Empty array if none. |
| `rating` | object\|null | ✓ | `{ "average": 4.6, "count": 8231 }`. Null when unrated. |
| `video_url` | string\|null | opt | When a recipe video exists. |
| `comment_count` | integer\|null | opt | Popularity signal alongside rating.count. |
| `published_at` | string\|null | ✓ | ISO 8601 date (`YYYY-MM-DD`) of NYT publication. Null if unknown. |
| `scraped_at` | string | ✓ | ISO 8601 timestamp of scrape. |
| `scraper_version` | string | ✓ | Semver of the scraper that produced the record. Lets rebuilds target stale records. |

### Ingredient object

| Field | Type | Req | Notes |
|---|---|---|---|
| `position` | integer | ✓ | 1-based, order as published. |
| `raw` | string | ✓ | Verbatim line: `"1 ½ cups all-purpose flour, sifted"`. Never discarded — parse failures degrade to raw-only. |
| `group` | string\|null | ✓ | Component heading when the recipe splits ingredients (`"For the sauce:"` → `"sauce"`). Null for ungrouped. |
| `quantity` | number\|null | ✓ | Parsed numeric amount (fractions, ranges → lower bound; `"1–2"` → `{"quantity": 1, "quantity_max": 2}` via optional `quantity_max`). Null for "to taste". |
| `unit` | string\|null | ✓ | Normalized: `cup, tbsp, tsp, oz, lb, g, kg, ml, l, clove, can, bunch, ...`. Null for countable items. |
| `item` | string | ✓ | Canonical-ish ingredient noun, lowercase, trimmed of prep: `"all-purpose flour"`. This is the include/exclude match key. Best-effort parse; when parsing is ambiguous, `item` = cleaned raw text (never null/empty). |
| `preparation` | string\|null | ✓ | `"sifted"`, `"finely chopped"`, `"room temperature"`. |
| `optional` | boolean | ✓ | Default false. |

### Nutrition object (per serving unless noted)

| Field | Type | Notes |
|---|---|---|
| `source` | string | **Required.** `"nyt"` = shown on the NYT page (NYT displays Edamam-derived per-serving nutrition on many recipes), `"computed"` = produced by our pipeline for recipes without published data, `"mixed"` = some fields each way. |
| `basis` | string | `"per_serving"` (default) or `"per_recipe"`. |
| `calories` | number\|null | kcal. |
| `protein_g` | number\|null | |
| `carbohydrates_g` | number\|null | |
| `fat_g` | number\|null | |
| `saturated_fat_g` | number\|null | |
| `unsaturated_fat_g` | number\|null | |
| `trans_fat_g` | number\|null | |
| `cholesterol_mg` | number\|null | |
| `sodium_mg` | number\|null | |
| `fiber_g` | number\|null | |
| `sugar_g` | number\|null | |
| `computed_with` | string\|null | Tool/model used when `source` includes computed, e.g. `"usda-sr-legacy-v1"`. Reproducibility hook. |

Every nutrient is independently nullable. A recipe with only calories published
has all other fields null. **Consumers must treat null as unknown, never zero.**

---

## 2. JSON Schema (schema/recipe.schema.json)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/<org>/free-nyt-cooking/schema/recipe.schema.json",
  "title": "NytCookingRecipe",
  "type": "object",
  "required": ["slug", "nyt_id", "url", "name", "author", "description",
               "servings", "time", "ingredients", "steps", "nutrition",
               "tags", "images", "rating", "scraped_at", "scraper_version"],
  "properties": {
    "slug":      { "type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$" },
    "nyt_id":    { "type": "integer" },
    "url":       { "type": "string", "format": "uri" },
    "name":      { "type": "string", "minLength": 1 },
    "author":    { "type": "string" },
    "description": { "type": "string" },
    "servings": {
      "type": "object",
      "required": ["text"],
      "properties": {
        "text": { "type": "string" },
        "min":  { "type": ["integer", "null"] },
        "max":  { "type": ["integer", "null"] },
        "yield_type": { "type": "string", "enum": ["servings", "pieces", "volume", "other"], "default": "servings" }
      }
    },
    "time": {
      "type": "object",
      "required": ["raw"],
      "properties": {
        "prep_minutes":  { "type": ["integer", "null"] },
        "cook_minutes":  { "type": ["integer", "null"] },
        "total_minutes": { "type": ["integer", "null"] },
        "raw": { "type": "object", "additionalProperties": { "type": "string" } }
      }
    },
    "ingredients": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["position", "raw", "group", "quantity", "unit", "item", "preparation", "optional"],
        "properties": {
          "position": { "type": "integer", "minimum": 1 },
          "raw":      { "type": "string", "minLength": 1 },
          "group":    { "type": ["string", "null"] },
          "quantity": { "type": ["number", "null"] },
          "quantity_max": { "type": ["number", "null"] },
          "unit":     { "type": ["string", "null"] },
          "item":     { "type": "string", "minLength": 1 },
          "preparation": { "type": ["string", "null"] },
          "optional": { "type": "boolean", "default": false }
        }
      }
    },
    "steps": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["position", "text"],
        "properties": {
          "position": { "type": "integer", "minimum": 1 },
          "text":     { "type": "string", "minLength": 1 }
        }
      }
    },
    "nutrition": {
      "type": ["object", "null"],
      "required": ["source", "basis"],
      "properties": {
        "source": { "type": "string", "enum": ["nyt", "computed", "mixed"] },
        "basis":  { "type": "string", "enum": ["per_serving", "per_recipe"], "default": "per_serving" },
        "calories": { "type": ["number", "null"] },
        "protein_g": { "type": ["number", "null"] },
        "carbohydrates_g": { "type": ["number", "null"] },
        "fat_g": { "type": ["number", "null"] },
        "saturated_fat_g": { "type": ["number", "null"] },
        "unsaturated_fat_g": { "type": ["number", "null"] },
        "trans_fat_g": { "type": ["number", "null"] },
        "cholesterol_mg": { "type": ["number", "null"] },
        "sodium_mg": { "type": ["number", "null"] },
        "fiber_g": { "type": ["number", "null"] },
        "sugar_g": { "type": ["number", "null"] },
        "computed_with": { "type": ["string", "null"] }
      }
    },
    "tags": {
      "type": "object",
      "required": ["cuisine", "diet", "course", "meal", "occasion", "other"],
      "properties": {
        "cuisine":  { "type": "array", "items": { "type": "string" } },
        "diet":     { "type": "array", "items": { "type": "string" } },
        "course":   { "type": "array", "items": { "type": "string" } },
        "meal":     { "type": "array", "items": { "type": "string" } },
        "occasion": { "type": "array", "items": { "type": "string" } },
        "other":    { "type": "array", "items": { "type": "string" } }
      },
      "additionalProperties": false
    },
    "images": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["url", "primary"],
        "properties": {
          "url": { "type": "string", "format": "uri" },
          "alt": { "type": "string" },
          "primary": { "type": "boolean" }
        }
      }
    },
    "rating": {
      "type": ["object", "null"],
      "required": ["average", "count"],
      "properties": {
        "average": { "type": "number", "minimum": 0, "maximum": 5 },
        "count":   { "type": "integer", "minimum": 0 }
      }
    },
    "video_url": { "type": ["string", "null"] },
    "comment_count": { "type": ["integer", "null"] },
    "published_at": { "type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "scraped_at": { "type": "string", "format": "date-time" },
    "scraper_version": { "type": "string" }
  },
  "additionalProperties": false
}
```

---

## 3. SQLite query store (schema/ddl.sql)

Design rules:
- Macro/time/rating filters are plain `WHERE` on `recipes` columns — no JSON
  extraction in the hot path.
- Ingredients and tags are real tables so include/exclude is an indexed join.
- FTS5 external-content table covers name + description + ingredients + steps.
- Every query answerable from `recipes` alone never touches ingredients/steps —
  that is what keeps "query without reading whole recipes" true.

```sql
-- free-nyt-cooking SQLite query store DDL.
-- Derived artifact: built only by tools/build_db.py from data/shards/*.jsonl.
-- Note: recipes carries two denormalized columns (ingredients_text, steps_text)
-- so the FTS5 external-content table + trigger trio below work unchanged.
--
-- Primary key is nyt_id, not slug: NYT reuses a small number of name-slugs
-- for distinct recipes, so slug is non-unique (indexed) and nyt_id is the
-- stable cross-reference key everywhere.

PRAGMA journal_mode = WAL;

CREATE TABLE recipes (
  nyt_id           INTEGER PRIMARY KEY,
  slug             TEXT NOT NULL,
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

CREATE INDEX idx_recipes_slug          ON recipes(slug);
CREATE INDEX idx_recipes_author        ON recipes(author);
CREATE INDEX idx_recipes_total_minutes ON recipes(total_minutes);
CREATE INDEX idx_recipes_calories      ON recipes(calories);
CREATE INDEX idx_recipes_protein       ON recipes(protein_g);
CREATE INDEX idx_recipes_rating        ON recipes(rating_average);
CREATE INDEX idx_recipes_published     ON recipes(published_at);

CREATE TABLE ingredients (
  nyt_id      INTEGER NOT NULL REFERENCES recipes(nyt_id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,
  raw         TEXT NOT NULL,
  grp         TEXT,
  quantity    REAL,
  quantity_max REAL,
  unit        TEXT,
  item        TEXT NOT NULL,     -- include/exclude match key, lowercase
  preparation TEXT,
  optional    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (nyt_id, position)
);
CREATE INDEX idx_ingredients_item ON ingredients(item);

CREATE TABLE steps (
  nyt_id   INTEGER NOT NULL REFERENCES recipes(nyt_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  text     TEXT NOT NULL,
  PRIMARY KEY (nyt_id, position)
);

CREATE TABLE tags (
  nyt_id   INTEGER NOT NULL REFERENCES recipes(nyt_id) ON DELETE CASCADE,
  category TEXT NOT NULL CHECK (category IN ('cuisine','diet','course','meal','occasion','other')),
  tag      TEXT NOT NULL,
  PRIMARY KEY (nyt_id, category, tag)
);
CREATE INDEX idx_tags_tag      ON tags(tag);
CREATE INDEX idx_tags_category ON tags(category, tag);

CREATE TABLE images (
  nyt_id   INTEGER NOT NULL REFERENCES recipes(nyt_id) ON DELETE CASCADE,
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
```

**Summary view** — the default shape of every list-style tool result:

```sql
CREATE VIEW recipe_summary AS
SELECT nyt_id, slug, name, author, url, total_minutes, servings_min, servings_max,
       calories, protein_g, carbohydrates_g, fat_g, nutrition_source,
       rating_average, rating_count, published_at
FROM recipes;
```

---

## 4. Scraping-coverage caveats (handoff notes for the scraper agent)

- NYT publishes per-serving nutrition (Edamam-derived) on many but not all
  recipes. When present on the page, record `source: "nyt"`. Leave `nutrition`
  null when absent; computation is a separate pipeline step, not the scraper's job.
- Ratings/comments are dynamic; `scraped_at` on the record marks their vintage.
- Some old recipes lack structured times or yields — nulls are expected, not errors.
- `author` can be "" (wire-service or unattributed recipes). Keep it queryable anyway.
