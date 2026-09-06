#!/bin/bash
# Crawler A recovery after container rebuild. Prereq: git auth (PAT) present in
# ~/.git-credentials; if push/clone fails, re-mint PAT via cloud browser
# (GitHub > settings > personal-access-tokens, repo-scoped Contents r/w) or read
# Bitwarden entry "GitHub PAT free-nyt-cooking" via browser, then re-run.
set -e
cd /home/sandbox
if [ ! -d free-nyt-cooking ]; then git clone -q https://github.com/p3rciv3l/free-nyt-cooking; fi
cd free-nyt-cooking && git pull -q --rebase origin main
mkdir -p /home/sandbox/work/{raw,shards,state,logs}
cp crawl-state/manifest-a.txt /home/sandbox/work/manifest.txt
cp crawl-state/checkpoint-a.done.log /home/sandbox/work/state/done.log 2>/dev/null || true
cp crawl-state/dead-letter-a.jsonl /home/sandbox/work/state/dead_letter.jsonl 2>/dev/null || true
cp crawl-state/crawl-a.py /home/sandbox/work/crawl.py
cp crawl-state/sync-a.sh /home/sandbox/work/sync_repo.sh
rsync -a data/shards/ /home/sandbox/work/shards/
[ -d /home/sandbox/crawlenv ] || python3 -m venv /home/sandbox/crawlenv
/home/sandbox/crawlenv/bin/pip -q install "recipe-scrapers==15.9.0" requests
chmod +x /home/sandbox/work/sync_repo.sh
nohup /home/sandbox/crawlenv/bin/python /home/sandbox/work/crawl.py --manifest /home/sandbox/work/manifest.txt --workdir /home/sandbox/work >> /home/sandbox/work/logs/run.out 2>&1 &
nohup /home/sandbox/work/sync_repo.sh > /dev/null 2>&1 &
echo "bootstrap complete: crawler + sync restarted"
