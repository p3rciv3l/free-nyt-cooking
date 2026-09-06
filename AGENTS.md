# free-nyt-cooking — AGENTS.md

Every NYT Cooking recipe as a local, queryable database. Read this before
querying. Nothing here requires network access.

## 30-second orientation

- The data: ~26k recipes from cooking.nytimes.com, as JSONL shards in
  `data/shards/` plus a prebuilt SQLite db at `db/recipes.db`.
- The tool: `tools/nytc` — one CLI, JSON in/stdout out. **Always query through
  `nytc`; do not read the JSONL files or open the db yourself.**
- The machine-readable version of everything below: `tools-manifest.json`.
- The record contract: `schema/recipe.schema.json` (human docs: `SCHEMA.md`).

## If `db/recipes.db` is missing

```bash
python3 tools/build_db.py        # rebuilds from data/shards/ in < 1 min
```

## The one workflow that covers 95% of uses

1. **Discover vocabulary first** if your filter values are guesses:
   `nytc tags --category diet`, `nytc ingredients "chicken thigh"`,
   `nytc authors`.
2. **Filter/search to a shortlist of summaries** (name, time, macros, rating —
   no full text yet): `nytc search ...` or `nytc filter ...`.
3. **Fetch full records only for the chosen few**: `nytc get <slug>`.

Never `get` more recipes than you will actually read. Summaries are the cheap
currency; full records are for final candidates.

## Command cheat sheet

| You want | Command |
|---|---|
| One known recipe, full detail | `nytc get <slug>` |
| Text search + any filters | `nytc search "<text>" [filters]` |
| Filters only, no text | `nytc filter [filters]` |
| Surprise me | `nytc random [filters] [--n N]` |
| More like this one | `nytc similar <slug> [--n N]` |
| Highest/lowest by a macro or rating | `nytc top --by <col> [filters] [--asc] [--per-recipe]` |
| Valid tag values | `nytc tags [--category ...]` |
| Canonical ingredient spelling | `nytc ingredients "<substring>"` |
| Who cooks here | `nytc authors` |
| What's in this dataset | `nytc stats` |
| Bulk full records | `nytc export [filters] --format jsonl` |

## Filter flags (search / filter / random / top / similar)

```
--cuisine X            repeatable; e.g. italian, indian, mexican
--diet X               repeatable; e.g. vegetarian, vegan, gluten-free
--course X             repeatable; e.g. dinner, dessert, side-dish
--author NAME
--include-ingredient X repeatable, all must match
--exclude-ingredient X repeatable, none may match
--max-total-minutes N  (--min-... also exists)
--min-servings N  --max-servings N
--cal-min/-max  --protein-min/-max  --carbs-min/-max  --fat-min/-max
--min-rating R --min-rating-count N
--nutrition-source nyt|computed|mixed
--published-since YYYY-MM-DD  --published-until YYYY-MM-DD
--sort FIELD (-prefix = desc)  --limit N  --offset N  --fields a,b,c  --full
```

## Field cheat sheet (summary rows)

`slug, name, author, url, total_minutes, servings_min, servings_max,
calories, protein_g, carbohydrates_g, fat_g, nutrition_source,
rating_average, rating_count, published_at`

Full records add `description, servings.text, time.{prep,cook,raw},
ingredients[], steps[], tags{}, images[], nutrition{}, video_url,
comment_count, scraped_at, scraper_version`.

## Examples

```bash
# High-protein vegetarian dinners under 45 min, well-reviewed
nytc search "" --diet vegetarian --course dinner --protein-min 25 \
  --max-total-minutes 45 --min-rating 4 --min-rating-count 50 --limit 10

# "something with miso and chicken, no cilantro"
nytc search "miso chicken" --exclude-ingredient cilantro

# lowest-calorie desserts with published (not computed) nutrition
nytc top --by=-calories --course dessert --nutrition-source nyt --n 10

# pick tonight's dinner at random from weeknight-friendly options
nytc random --course dinner --max-total-minutes 30 --min-rating 4

# what can I cook with what's in the fridge
nytc search "" --include-ingredient "chicken thigh" --include-ingredient lemon \
  --max-total-minutes 60 --sort -rating

# full recipe once the shortlist is chosen
nytc get miso-chicken
```

## Gotchas — read these before trusting results

- **Null means unknown, never zero.** A recipe with no published calories is
  not a 0-calorie recipe. Any macro filter silently excludes null rows — that's
  intended, but know it's happening.
- **`nutrition_source` matters.** `nyt` = published on the site; `computed` =
  our estimate. Pass `--nutrition-source nyt` when the user needs published
  numbers.
- **Macros are per serving** unless `nutrition_basis` says otherwise. Use
  `top --per-recipe` for whole-dish comparisons.
- **Ratings and comment counts are a snapshot** as of `scraped_at`, not live.
- **Ingredient include/exclude matches `item`**, the parsed noun, not the raw
  line. Check spelling with `nytc ingredients` when a match looks wrong.
- **`author` can be empty** on some recipes.
- **The slug is the key.** Everything cross-references on it.
- **`--by=-calories` needs the `=`** (argparse treats a bare `-calories` as a
  flag); `--asc` does the same thing.
- **db-reconstructed full records lose `time.raw`** (comes back `{}`) — the
  JSONL shards keep the raw strings.
