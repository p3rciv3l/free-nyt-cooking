# free-nyt-cooking

Every NYT cooking recipe, made easily queryable. Tell your agent to look at TOOLS.md when searching the DB.

https://github.com/user-attachments/assets/3e373ce6-6ed4-4726-a3a4-be7d686009c4

25,418 recipes as JSONL shards under `data/shards/` (keyed by `nyt_id`), with a
SQLite db and the `tools/nytc` CLI on top. Each record carries name, author,
ingredients, steps, times, servings, tags, per-serving nutrition, rating, image
URLs, and the source URL.

## Suggested queries

```bash
python3 tools/build_db.py   # one-time setup: builds db/recipes.db from the shards (<1 min)
```

- `tools/nytc stats` - dataset overview: recipe, tag, and nutrition coverage counts.
- `tools/nytc search "miso chicken" --max-total-minutes 45` - full-text search with structured filters.
- `tools/nytc filter --diet vegetarian --course dinner --max-total-minutes 30` - structured-only query, no text.
- `tools/nytc top --by protein_g --diet vegetarian --n 10` - leaderboard over any numeric field.
- `tools/nytc similar pumpkin-creme-brulee --n 5` - recipes most similar to a given one.
- `tools/nytc get marcella-hazan-tomato-sauce` - full record (ingredients and steps); accepts slug or nyt_id.

Full CLI reference: [TOOLS.md](TOOLS.md).

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
