#!/usr/bin/env python3
"""Merge per-crawler shards (<prefix>.a.jsonl, <prefix>.b.jsonl, ...) into the
canonical <prefix>.jsonl layout from DATA-LAYOUT.md.

Run once post-crawl. Full-record replace keyed on nyt_id (NYT reuses some
slugs for distinct recipes, so slug cannot be the key); on nyt_id conflicts
the record with the newest scraped_at wins. Slugs are lowercased at merge
time (NYT serves a handful of URLs with capitalized slugs; the url field
keeps the source casing). Canonical shards are bucketed by the first 2 chars
of the normalized slug (1 char for single-character slugs), lines sorted by
slug then nyt_id so duplicate slugs stay adjacent. Per-crawler files are
removed after a successful merge. Stdlib only.

Usage:
    python3 tools/merge_shards.py [--data-dir data/shards] [--dry-run]
"""
import argparse
import glob
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRAWLER_SHARD_RE = re.compile(r"^[a-z0-9-]{1,2}\.[a-z]\.jsonl$")
CANONICAL_SHARD_RE = re.compile(r"^[a-z0-9-]{1,2}\.jsonl$")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data", "shards"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    crawler_files = []
    canonical_files = []
    for path in sorted(glob.glob(os.path.join(args.data_dir, "*.jsonl"))):
        name = os.path.basename(path)
        if CRAWLER_SHARD_RE.match(name):
            crawler_files.append(path)
        elif CANONICAL_SHARD_RE.match(name):
            canonical_files.append(path)
    if not crawler_files:
        print("merge: no per-crawler shards found", file=sys.stderr)
        return 0

    records = {}
    for path in canonical_files + crawler_files:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                r["slug"] = r["slug"].lower()
                key = int(r["nyt_id"])
                prev = records.get(key)
                if prev is None or r.get("scraped_at", "") >= prev.get("scraped_at", ""):
                    records[key] = r

    shards = {}
    for rec in records.values():
        shards.setdefault(rec["slug"][:2], []).append(rec)

    merged_records = 0
    for shard_key in sorted(shards):
        recs = sorted(shards[shard_key],
                      key=lambda r: (r["slug"], int(r["nyt_id"])))
        canonical = os.path.join(args.data_dir, shard_key + ".jsonl")
        print("merge: %s (%d records)" % (canonical, len(recs)), file=sys.stderr)
        merged_records += len(recs)
        if not args.dry_run:
            with open(canonical, "w", encoding="utf-8") as fh:
                for rec in recs:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if not args.dry_run:
        for path in crawler_files:
            os.remove(path)
    print("merge: %d shard(s), %d record(s)%s"
          % (len(shards), merged_records, " (dry run)" if args.dry_run else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
