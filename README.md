# free-nyt-cooking

Every NYT cooking recipe, made easily queryable. Tell your agent to look at TOOLS.md when searching the DB.

https://github.com/user-attachments/assets/19bb49ee-787e-4ceb-bc44-2eb86e12ad90

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
