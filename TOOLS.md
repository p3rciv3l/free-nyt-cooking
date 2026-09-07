# free-nyt-cooking — Query Tool Surface (`nytc` CLI)

One binary, `tools/nytc`, Python 3 stdlib only (`sqlite3`, `json`, `argparse`).
Every subcommand prints JSON to stdout and nothing else; errors go to stderr with
a non-zero exit. Agents should never need to open the db or JSONL directly.

Two output shapes:
- **summary** (default for list commands): the `recipe_summary` view — one line
  of orientation per recipe, cheap to scan, no steps/ingredients. Printed as a
  JSON array.
- **full**: the complete canonical record (`nytc get`, or `--full` anywhere),
  one JSON object per line.

Global flags on every query command:
`--db PATH` (default `db/recipes.db`), `--limit N` (default 10, max 100),
`--offset N`, `--fields a,b,c` (projection on summary rows), `--full`.
Vocabulary commands (`tags`, `ingredients`, `authors`) take `--db` and
`--limit` (default 50).

Shared filter flags (accepted by `search`, `filter`, `random`, `top`, `similar`):

| Flag | Semantics |
|---|---|
| `--cuisine X` (repeatable) | tag category `cuisine`; OR within a flag, AND across categories |
| `--diet X` (repeatable) | e.g. `vegetarian`, `vegan`, `gluten-free`, `dairy-free`, `kosher` |
| `--course X` (repeatable) | e.g. `dinner`, `dessert`, `appetizer`, `side-dish` |
| `--author NAME` | exact match first, falls back to case-insensitive substring |
| `--include-ingredient X` (repeatable) | all must be present; matches `ingredients.item` (substring, case-insensitive) |
| `--exclude-ingredient X` (repeatable) | none may be present |
| `--max-total-minutes N` / `--min-total-minutes N` | null times are excluded when a bound is given |
| `--min-servings N` / `--max-servings N` | overlap test against `servings_min..servings_max` |
| `--cal-min/-max N` `--protein-min/-max N` `--carbs-min/-max N` `--fat-min/-max N` | macro ranges; rows with null in a constrained column are excluded |
| `--min-rating R` (0–5) / `--min-rating-count N` | popularity floor, e.g. `--min-rating 4 --min-rating-count 100` |
| `--nutrition-source nyt\|computed\|mixed` | |
| `--published-since YYYY-MM-DD` / `--published-until YYYY-MM-DD` | |
| `--sort FIELD` | `rating`, `rating_count`, `calories`, `protein_g`, `total_minutes`, `published_at` (prefix `-` for desc; default desc for ratings/calories/protein/published, asc for time). NULLs always last. |

## Commands

### `nytc get <slug-or-nyt-id> [<slug-or-nyt-id>...]`
Full canonical record(s), one JSON object per line. The only command family that
returns ingredients and steps. `--fields` applies (top-level keys).
A numeric argument is an `nyt_id`; anything else is a slug. NYT reuses a few
slugs for distinct recipes: a shared slug returns every matching record with a
stderr note (use the `nyt_id` to pick one). Unknown ids/slugs warn on stderr
and are skipped.

### `nytc search "<text>" [filters]`
FTS5 over name + description + ingredients + steps (porter-stemmed, phrase
support: the string is passed through as an FTS5 query; bare words are ANDed;
on a syntax error the CLI retries with every token quoted). Ranked by FTS
score, or by `--sort` when given. Summary rows plus a `score` field.
Empty text (`""`) = filter-only mode, identical to `filter`.

### `nytc filter [filters]`
Structured-only query, no text. Summary rows.

### `nytc random [filters] [--n 1]`
Uniform random pick among matches (`--n` max 20).

### `nytc similar <slug-or-nyt-id> [--n 5]`
Ranked by shared-tag count + shared-ingredient Jaccard over `ingredients.item`,
ties broken by rating. Summary rows plus a `similarity` field (`--n` max 50).
A shared slug (see `get`) errors and asks for an `nyt_id`.

### `nytc top --by <column> [filters] [--n 10] [--asc] [--per-recipe]`
Nutrition/popularity leaderboard. `--by` ∈ `protein_g, calories, fiber_g,
sodium_mg, rating_average, rating_count` (+ any numeric recipes column).
Default order is descending (highest first); use `--by=-calories` (note the
`=`) or `--asc` for lowest-first. NULLs always sort last. Summary rows plus a
`value` field with the ranked number. `--per-recipe` switches ranking to
whole-recipe numbers by multiplying per-serving values by `servings_max`
(rows where basis is not per_serving or servings are unknown get NULL and
sort last).

### `nytc tags [--category cuisine|diet|course|meal|occasion|other] [--prefix p]`
Vocabulary discovery: every tag with recipe counts, so agents filter with real
values instead of guessing.

### `nytc ingredients "<substring>"`
Ingredient vocabulary discovery: distinct `item` values with counts. Use before
`--include-ingredient` to find the canonical spelling ("chicken thigh" vs
"chicken thighs").

### `nytc authors [--prefix p]`
Distinct authors with recipe counts.

### `nytc stats`
Dataset overview: counts, tag/macro coverage, null rates per nutrition column,
rating distribution.

### `nytc export [filters] [--format jsonl]`
Full records for a filtered subset, one per line. `jsonl` is the only format.
Honors `--limit`/`--offset` (default limit 10, max 100 — page with `--offset`
for larger extracts).

## Notes on db-reconstructed full records

`get`/`--full`/`export` rebuild canonical records from the db. Two fields are
lossy relative to the JSONL shards: `time.raw` comes back `{}` (the db stores
parsed minutes only) and `nutrition.computed_with` comes back `null`. Read the
JSONL shards (via the scraper tooling) when those matter.

## Machine-readable manifest (tools-manifest.json)

Draft-2020-12 JSON Schema per tool; `command` shows the exact CLI invocation.
Agents that prefer function-calling JSON can consume this file directly.

## Worked examples

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

## Repo tooling (data pipeline)

```bash
python3 tools/scrape.py --urls urls.txt          # crawl -> data/shards/ shards
python3 tools/validate.py data/shards/          # schema-validate every line (CI gate)
python3 tools/merge_shards.py                    # post-crawl: unify <prefix>.<crawler>.jsonl -> <prefix>.jsonl
python3 tools/build_db.py                        # rebuild db/recipes.db + data/manifest.json
```
