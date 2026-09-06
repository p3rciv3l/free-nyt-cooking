#!/usr/bin/env python3
"""Rebuild db/recipes.db from data/recipes/*.jsonl and refresh data/manifest.json.

Idempotent and order-independent: the db is dropped and rebuilt from the
canonical shards every run. Stdlib only.

Usage:
    python3 tools/build_db.py [--data-dir data/recipes] [--db db/recipes.db]
                              [--manifest data/manifest.json] [--no-manifest]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_VERSION = "1.0.0"

NUTRITION_COLS = ["calories", "protein_g", "carbohydrates_g", "fat_g",
                  "saturated_fat_g", "unsaturated_fat_g", "trans_fat_g",
                  "cholesterol_mg", "sodium_mg", "fiber_g", "sugar_g"]


def load_records(data_dir):
    """Return {slug: record} from every shard (*.jsonl, including per-crawler
    <prefix>.<crawler>.jsonl). Duplicate slugs: newest scraped_at wins."""
    records = {}
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(data_dir, name)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                prev = records.get(r["slug"])
                if prev is None or r.get("scraped_at", "") >= prev.get("scraped_at", ""):
                    records[r["slug"]] = r
    return records


def build(data_dir, db_path):
    ddl = open(os.path.join(REPO_ROOT, "schema", "ddl.sql"), encoding="utf-8").read()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)

    conn = sqlite3.connect(db_path)
    conn.executescript(ddl)
    count = 0
    with conn:
        all_records = load_records(data_dir)
        for r in (all_records[s] for s in sorted(all_records)):
            nutrition = r.get("nutrition") or {}
            servings = r.get("servings") or {}
            time_ = r.get("time") or {}
            rating = r.get("rating") or {}
            ingredients = r.get("ingredients") or []
            steps = r.get("steps") or []
            ingredients_text = "\n".join(i.get("raw", "") for i in ingredients)
            steps_text = "\n".join(s.get("text", "") for s in steps)
            conn.execute(
                """INSERT INTO recipes (
                     slug, nyt_id, url, name, author, description,
                     servings_text, servings_min, servings_max, yield_type,
                     prep_minutes, cook_minutes, total_minutes,
                     calories, protein_g, carbohydrates_g, fat_g,
                     saturated_fat_g, unsaturated_fat_g, trans_fat_g,
                     cholesterol_mg, sodium_mg, fiber_g, sugar_g,
                     nutrition_source, nutrition_basis,
                     rating_average, rating_count, comment_count, video_url,
                     published_at, scraped_at, scraper_version,
                     ingredients_text, steps_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["slug"], r["nyt_id"], r["url"], r["name"],
                 r.get("author", ""), r.get("description", ""),
                 servings.get("text", ""), servings.get("min"), servings.get("max"),
                 servings.get("yield_type", "servings"),
                 time_.get("prep_minutes"), time_.get("cook_minutes"),
                 time_.get("total_minutes"),
                 nutrition.get("calories"), nutrition.get("protein_g"),
                 nutrition.get("carbohydrates_g"), nutrition.get("fat_g"),
                 nutrition.get("saturated_fat_g"), nutrition.get("unsaturated_fat_g"),
                 nutrition.get("trans_fat_g"), nutrition.get("cholesterol_mg"),
                 nutrition.get("sodium_mg"), nutrition.get("fiber_g"),
                 nutrition.get("sugar_g"),
                 nutrition.get("source"), nutrition.get("basis", "per_serving"),
                 rating.get("average"), rating.get("count"),
                 r.get("comment_count"), r.get("video_url"),
                 r.get("published_at"), r["scraped_at"], r["scraper_version"],
                 ingredients_text, steps_text))
            for ing in ingredients:
                conn.execute(
                    """INSERT INTO ingredients
                         (slug, position, raw, grp, quantity, quantity_max,
                          unit, item, preparation, optional)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (r["slug"], ing["position"], ing["raw"], ing.get("group"),
                     ing.get("quantity"), ing.get("quantity_max"), ing.get("unit"),
                     ing["item"], ing.get("preparation"),
                     1 if ing.get("optional") else 0))
            for st in steps:
                conn.execute("INSERT INTO steps (slug, position, text) VALUES (?,?,?)",
                             (r["slug"], st["position"], st["text"]))
            for category, values in (r.get("tags") or {}).items():
                for tag in values:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags (slug, category, tag) VALUES (?,?,?)",
                        (r["slug"], category, tag))
            for img in r.get("images") or []:
                conn.execute(
                    "INSERT INTO images (slug, url, alt, primary_) VALUES (?,?,?,?)",
                    (r["slug"], img["url"], img.get("alt", ""),
                     1 if img.get("primary") else 0))
            count += 1
    conn.execute("PRAGMA optimize")
    conn.close()
    return count


def refresh_manifest(data_dir, manifest_path):
    shards = {}
    recipe_count = 0
    nutrition = {"nyt": 0, "computed": 0, "mixed": 0, "none": 0}
    scraper_versions = set()
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(data_dir, name)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        shards[name] = h.hexdigest()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                recipe_count += 1
                r = json.loads(line)
                scraper_versions.add(r.get("scraper_version", ""))
                src = (r.get("nutrition") or {}).get("source")
                nutrition[src if src in ("nyt", "computed", "mixed") else "none"] += 1
    total = max(recipe_count, 1)
    manifest = {
        "dataset": "free-nyt-cooking",
        "recipe_count": recipe_count,
        "shard_count": len(shards),
        "schema_version": SCHEMA_VERSION,
        "scraper_version": sorted(scraper_versions)[-1] if scraper_versions else None,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "cooking.nytimes.com",
        "nutrition_coverage": {
            "nyt": round(nutrition["nyt"] / total, 4),
            "computed": round(nutrition["computed"] / total, 4),
            "mixed": round(nutrition["mixed"] / total, 4),
            "none": round(nutrition["none"] / total, 4),
        },
        "sha256_by_shard": shards,
    }
    os.makedirs(os.path.dirname(os.path.abspath(manifest_path)), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data", "recipes"))
    ap.add_argument("--db", default=os.path.join(REPO_ROOT, "db", "recipes.db"))
    ap.add_argument("--manifest", default=os.path.join(REPO_ROOT, "data", "manifest.json"))
    ap.add_argument("--no-manifest", action="store_true")
    args = ap.parse_args(argv)

    count = build(args.data_dir, args.db)
    print("built %s from %s: %d recipe(s)" % (args.db, args.data_dir, count),
          file=sys.stderr)
    if not args.no_manifest:
        m = refresh_manifest(args.data_dir, args.manifest)
        print("manifest: %d shard(s), coverage %s"
              % (m["shard_count"], m["nutrition_coverage"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
