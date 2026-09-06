#!/usr/bin/env python3
"""Merge per-crawler shards (<prefix>.a.jsonl, <prefix>.b.jsonl, ...) into the
canonical <prefix>.jsonl layout from DATA-LAYOUT.md.

Run once post-crawl. Full-record replace keyed on slug; on slug conflicts the
record with the newest scraped_at wins. Lines are sorted by slug. Per-crawler
files are removed after a successful merge. Stdlib only.

Usage:
    python3 tools/merge_shards.py [--data-dir data/recipes] [--dry-run]
"""
import argparse
import glob
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRAWLER_SHARD_RE = re.compile(r"^([a-z0-9]{2})\.[a-z]\.jsonl$")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data", "recipes"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    prefixes = {}
    for path in sorted(glob.glob(os.path.join(args.data_dir, "*.jsonl"))):
        m = CRAWLER_SHARD_RE.match(os.path.basename(path))
        if m:
            prefixes.setdefault(m.group(1), []).append(path)
    if not prefixes:
        print("merge: no per-crawler shards found", file=sys.stderr)
        return 0

    merged_records = 0
    for prefix, crawler_files in sorted(prefixes.items()):
        canonical = os.path.join(args.data_dir, prefix + ".jsonl")
        records = {}
        for path in [canonical] + crawler_files:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    prev = records.get(r["slug"])
                    if prev is None or r.get("scraped_at", "") >= prev.get("scraped_at", ""):
                        records[r["slug"]] = r
        print("merge: %s <- %s (%d records)"
              % (canonical, ", ".join(os.path.basename(p) for p in crawler_files),
                 len(records)), file=sys.stderr)
        merged_records += len(records)
        if not args.dry_run:
            with open(canonical, "w", encoding="utf-8") as fh:
                for slug in sorted(records):
                    fh.write(json.dumps(records[slug], ensure_ascii=False) + "\n")
            for path in crawler_files:
                os.remove(path)
    print("merge: %d prefix(es), %d record(s)%s"
          % (len(prefixes), merged_records, " (dry run)" if args.dry_run else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
