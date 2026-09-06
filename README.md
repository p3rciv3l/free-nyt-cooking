# free-nyt-cooking

Every NYT Cooking recipe (~26k) as a local, agent-queryable database.
Private, personal-use archive of cooking.nytimes.com data.

- **Canonical data**: JSONL shards in `data/shards/`, one JSON object per
  recipe, sharded by 2-char slug prefix. Schema: `schema/recipe.schema.json`.
- **Query store**: derived SQLite db at `db/recipes.db` (gitignored; rebuild
  with `python3 tools/build_db.py`).
- **Query CLI**: `tools/nytc` — stdlib-only, JSON output. Docs: `TOOLS.md`;
  agents start at `AGENTS.md`; machine-readable surface: `tools-manifest.json`.

## Rebuild / update

```bash
python3 tools/scrape.py --urls urls.txt       # crawl pages -> shards (needs: pip install recipe-scrapers)
python3 tools/validate.py data/shards/       # schema gate (also runs in CI)
python3 tools/merge_shards.py                 # post-crawl: unify per-crawler shards
python3 tools/build_db.py                     # rebuild db + data/manifest.json
```
