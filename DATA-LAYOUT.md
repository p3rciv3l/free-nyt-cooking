# free-nyt-cooking — Repository & Data Layout

```
free-nyt-cooking/
├── AGENTS.md                 # THE file agents read first
├── README.md                 # human overview: what, why, rebuild instructions
├── SCHEMA.md                 # canonical record schema (human docs)
├── DATA-LAYOUT.md            # this file
├── TOOLS.md                  # nytc CLI docs
├── schema/
│   ├── recipe.schema.json    # JSON Schema for one canonical record
│   └── ddl.sql               # SQLite DDL for the query store
├── data/
│   ├── manifest.json         # dataset-level metadata (see below)
│   ├── raw/                  # raw HTML cache — GITIGNORED (~7 GB), never pushed
│   └── shards/               # canonical JSONL shards, git-tracked, sorted by slug
│       ├── ab.jsonl          # canonical shard: key = first 2 chars of slug
│       ├── ab.a.jsonl        # per-crawler shard (crawler A) during crawl
│       ├── ab.b.jsonl        # per-crawler shard (crawler B) during crawl
│       └── ...
├── db/
│   └── recipes.db            # built SQLite artifact (gitignored; built locally or
│                             # downloaded from GitHub Releases)
├── tools/
│   ├── nytc                  # query CLI: python3, stdlib only (sqlite3, json, argparse)
│   ├── scrape.py             # crawler: URLs -> shards (raw HTML cached outside repo)
│   ├── validate.py           # validates every JSONL shard line against recipe.schema.json
│   ├── merge_shards.py       # post-crawl: unify <prefix>.<crawler>.jsonl -> <prefix>.jsonl
│   └── build_db.py           # data/shards/*.jsonl -> db/recipes.db (idempotent rebuild)
└── tools-manifest.json       # machine-readable manifest of the nytc tool surface
```

## Crawl-time vs canonical layout

- **During the crawl**, each crawler pushes only its own per-crawler shards
  (`<prefix>.<crawler>.jsonl`, e.g. `ab.a.jsonl`, `ab.b.jsonl`) plus its
  checkpoint and dead-letter files. Crawlers never touch each other's files,
  which keeps concurrent pushes conflict-free.
- **Raw HTML never enters the repo.** `data/raw/` (or any crawler cache dir)
  is gitignored — the full cache is ~7 GB. Optionally, as a final step, the
  raw HTML can be tarballed and attached to a GitHub Release as an asset.
- **Post-crawl**, `python3 tools/merge_shards.py` unifies per-crawler shards
  into the canonical `<prefix>.jsonl` layout (full-record replace keyed on
  slug, newest `scraped_at` wins, lines sorted by slug) and removes the
  per-crawler files.
- Tooling only reads shard-shaped filenames (`^[a-z0-9]{2}(\.[a-z])?\.jsonl$`);
  checkpoint and dead-letter files in the same directory are ignored by
  `validate.py` and `build_db.py`.

## Why this shape

- **JSONL shards are the canonical data.** Human-diffable, append-friendly,
  streamable (`head`, `tail`, `wc -l` all work), and the scraper can rewrite a
  single shard without touching the rest.
- **Shard by 2-char slug prefix.** ~26k recipes spread over a few hundred shards;
  each shard stays small (typically < 1 MB), git diffs from re-scrapes stay local,
  and lookup of one record = one shard file. Sort lines by slug within each shard
  so rebuilds are deterministic and diffs minimal.
- **SQLite is a derived artifact.** Never hand-edit it. `tools/build_db.py` drops
  and rebuilds from the shards; it is idempotent and order-independent (duplicate
  slugs across shards resolve to the newest `scraped_at`). Gitignore the db and
  attach it to GitHub Releases for agents that don't want to build (build is
  < 1 min for the full corpus).
- **Images are URLs only.** No binary assets in the repo.

## data/manifest.json

```json
{
  "dataset": "free-nyt-cooking",
  "recipe_count": 26000,
  "shard_count": 312,
  "schema_version": "1.0.0",
  "scraper_version": "0.1.0",
  "built_at": "2026-09-06T00:00:00Z",
  "source": "cooking.nytimes.com",
  "nutrition_coverage": { "nyt": 0.61, "computed": 0.0, "mixed": 0.0, "none": 0.39 },
  "sha256_by_shard": { "ab.jsonl": "...", "ac.jsonl": "..." }
}
```

`sha256_by_shard` lets an agent verify a shard without hashing the whole dataset,
and lets incremental re-scrapes detect drift.

## Record lifecycle

1. Scraper writes/updates one line in the shard matching the record's slug
   (full-record replace keyed on `slug`; never append duplicates).
2. `tools/validate.py data/shards/` — every line must pass `recipe.schema.json`.
   CI gate. Records from either parse path (Recipe JSON-LD or the richer
   `__NEXT_DATA__` scoopRecipe object) are held to the same schema — the
   validator is the arbiter.
3. `tools/merge_shards.py` — post-crawl unify to canonical shards.
4. `tools/build_db.py` — rebuilds `db/recipes.db` (recipes, ingredients, steps,
   tags, images, FTS index, summary view) and refreshes `data/manifest.json`.

## Sizes (sanity)

26k recipes × ~3 KB pretty-record ≈ 80 MB of JSONL across shards; SQLite with
indexes + FTS ≈ 120–150 MB. Raw HTML cache ≈ 7 GB (gitignored, optional
release tarball). The db only ever lives in Releases or local builds.
