# free-nyt-cooking

Every recipe on NYT Cooking as a local, queryable dataset: 25,418 recipes
scraped from cooking.nytimes.com, stored as JSONL with a SQLite query db and
a small CLI on top.

## What's inside

- `data/shards/` - the canonical data. 286 JSONL shards, one JSON object per
  recipe, sharded by the first 2 characters of the slug. Records are keyed by
  NYT's recipe id (`nyt_id`); NYT reuses some slugs for different recipes, so
  slug alone is not unique.
- `db/recipes.db` - derived SQLite db with indexes and full-text search.
  Gitignored; rebuild from the shards in under a minute.
- `tools/nytc` - query CLI (Python 3, stdlib only, JSON output): search,
  filter, similar, top-by-macro, random, export.
- Docs: `SCHEMA.md` (record fields), `DATA-LAYOUT.md` (repo layout),
  `TOOLS.md` (CLI reference), `AGENTS.md` (agent quickstart),
  `tools-manifest.json` (machine-readable CLI surface).

## Quick start

```bash
python3 tools/build_db.py        # builds db/recipes.db from data/shards/
tools/nytc search "miso chicken" --max-total-minutes 45
tools/nytc top --by protein_g --diet vegetarian --n 10
tools/nytc get pumpkin-creme-brulee     # full record; accepts slug or nyt_id
```

Each record carries name, author, ingredients, steps, times, servings, tags,
per-serving nutrition (94.5% published by NYT), a rating snapshot, image URLs,
and the source URL.

## How the data was sourced

Every recipe URL in NYT's own sitemap (25,595 ids, 1958-present) was fetched
anonymously - no account, no cookies, one request per ~2 seconds with
backoff - and parsed with `recipe-scrapers` plus NYT's embedded `__NEXT_DATA__`
JSON. 25,418 parsed and validated; the other 177 are dead on NYT's side (404s,
redirects, same-recipe re-issues) and are listed in
`crawl-state/dead-letter-*.jsonl`. `tools/validate.py` schema-gates every
record and runs in CI. `tools/scrape.py` re-crawls a URL list for updates.

## License and use

Recipe content (text, images, data) belongs to The New York Times. This is a
personal-use archive, not affiliated with or endorsed by NYT; if you use the
data, respect NYT's terms and copyright.
