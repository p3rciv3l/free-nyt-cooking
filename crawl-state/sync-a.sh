#!/bin/bash
# Crawler A sync - ONLY ever touches crawler-A-owned paths:
#   data/shards/*.a.jsonl and crawl-state/*-a.* (+ crawl-a.py/sync-a.sh/bootstrap-a.sh)
# Never: git add -A, git rm, git clean, rsync --delete into shared dirs.
REPO=/home/sandbox/free-nyt-cooking
WORK=/home/sandbox/work
cd "$REPO" || exit 1
A_PATHS="data/shards crawl-state"
while true; do
  sleep 900
  # copy only my shards (no --delete: B's *.b.jsonl share this dir)
  rsync -a --include='*.a.jsonl' --exclude='*' "$WORK/shards/" data/shards/
  cp -f "$WORK/state/done.log" crawl-state/checkpoint-a.done.log 2>/dev/null
  cp -f "$WORK/state/dead_letter.jsonl" crawl-state/dead-letter-a.jsonl 2>/dev/null
  cp -f "$WORK/state/status.json" crawl-state/status-a.json 2>/dev/null
  cp -f "$WORK/manifest.txt" crawl-state/manifest-a.txt 2>/dev/null
  cp -f "$WORK/crawl.py" crawl-state/crawl-a.py 2>/dev/null
  cp -f "$WORK/sync_repo.sh" crawl-state/sync-a.sh 2>/dev/null
  for f in data/shards/*.a.jsonl; do [ -f "$f" ] && LC_ALL=C sort -u -o "$f" "$f"; done
  python3 tools/validate.py data/shards >> "$WORK/logs/validate.log" 2>&1
  # stage ONLY crawler-A files, never the whole tree
  git add -- 'data/shards/*.a.jsonl' \
    crawl-state/checkpoint-a.done.log crawl-state/dead-letter-a.jsonl \
    crawl-state/status-a.json crawl-state/manifest-a.txt \
    crawl-state/crawl-a.py crawl-state/sync-a.sh crawl-state/bootstrap-a.sh 2>/dev/null
  if ! git diff --cached --quiet; then
    # safety: refuse any commit that deletes files we don't own
    if git diff --cached --name-status | grep -E '^D' | grep -vE '\.a\.jsonl|-a\.|crawl-a\.py|sync-a\.sh|bootstrap-a\.sh' ; then
      echo "$(date -u +%FT%TZ) REFUSED commit containing foreign deletions" >> "$WORK/logs/sync.log"
      git reset -q
      continue
    fi
    n=$(wc -l < "$WORK/state/done.log" 2>/dev/null || echo 0)
    git commit -q -m "crawler A checkpoint: $n recipes scraped"
    git pull --rebase -q origin main >> "$WORK/logs/sync.log" 2>&1
    git push -q origin main >> "$WORK/logs/sync.log" 2>&1 && echo "$(date -u +%FT%TZ) pushed at $n" >> "$WORK/logs/sync.log"
  fi
done
