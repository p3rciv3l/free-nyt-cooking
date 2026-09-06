#!/bin/bash
REPO=/home/sandbox/free-nyt-cooking
WORK=/home/sandbox/work
cd "$REPO" || exit 1
while true; do
  sleep 900
  rsync -a --delete "$WORK/shards/" data/shards/
  cp -f "$WORK/state/done.log" crawl-state/checkpoint-a.done.log 2>/dev/null
  cp -f "$WORK/state/dead_letter.jsonl" crawl-state/dead-letter-a.jsonl 2>/dev/null
  cp -f "$WORK/state/status.json" crawl-state/status-a.json 2>/dev/null
  cp -f "$WORK/manifest.txt" crawl-state/manifest-a.txt 2>/dev/null
  for f in data/shards/*.a.jsonl; do [ -f "$f" ] && LC_ALL=C sort -u -o "$f" "$f"; done
  python3 tools/validate.py data/shards >> "$WORK/logs/validate.log" 2>&1
  for f in data/shards/*.a.jsonl; do [ -f "$f" ] && LC_ALL=C sort -u -o "$f" "$f"; done
  git add -A
  if ! git diff --cached --quiet; then
    n=$(wc -l < "$WORK/state/done.log" 2>/dev/null || echo 0)
    git commit -q -m "crawler A checkpoint: $n recipes scraped"
    git pull --rebase -q origin main >> "$WORK/logs/sync.log" 2>&1
    git push -q origin main >> "$WORK/logs/sync.log" 2>&1 && echo "$(date -u +%FT%TZ) pushed at $n" >> "$WORK/logs/sync.log"
  fi
done
