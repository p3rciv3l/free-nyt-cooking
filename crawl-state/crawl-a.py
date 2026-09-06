#!/usr/bin/env python3
"""Crawler A: NYT Cooking recipe fetch+parse pipeline.
Fetch (paced) -> raw gz cache -> parse -> JSONL shards -> checkpoint."""
import argparse, gzip, html as htmlmod, json, os, random, re, sys, time, traceback
from datetime import datetime, timezone
import requests

SCRAPER_VERSION = "1.0.0"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate",
           "DNT": "1", "Upgrade-Insecure-Requests": "1"}
PACE_SECS = 2.0
BACKOFFS = [30, 120, 600]  # 30s -> 2m -> 10m, max 3 retries
RETRY_STATUSES = {429, 500, 502, 503, 504}

# ---------- parsing helpers ----------
FRAC = {"\u00bd":0.5,"\u2153":1/3,"\u2154":2/3,"\u00bc":0.25,"\u00be":0.75,"\u2155":0.2,"\u2156":0.4,"\u2157":0.6,"\u2158":0.8,"\u2159":1/6,"\u215a":5/6,"\u215b":0.125,"\u215c":0.375,"\u215d":0.625,"\u215e":0.875}
UNIT_MAP = {
 "cup":"cup","cups":"cup","tablespoon":"tbsp","tablespoons":"tbsp","tbsp":"tbsp","tbs":"tbsp",
 "teaspoon":"tsp","teaspoons":"tsp","tsp":"tsp","ounce":"oz","ounces":"oz","oz":"oz",
 "pound":"lb","pounds":"lb","lb":"lb","lbs":"lb","gram":"g","grams":"g","g":"g",
 "kilogram":"kg","kilograms":"kg","kg":"kg","milliliter":"ml","milliliters":"ml","ml":"ml",
 "liter":"l","liters":"l","litre":"l","litres":"l","l":"l",
 "clove":"clove","cloves":"clove","can":"can","cans":"can","bunch":"bunch","bunches":"bunch",
 "pinch":"pinch","pinches":"pinch","sprig":"sprig","sprigs":"sprig","stalk":"stalk","stalks":"stalk",
 "slice":"slice","slices":"slice","package":"package","packages":"package","pkg":"package",
 "head":"head","heads":"head","stick":"stick","sticks":"stick","dash":"dash","dashes":"dash",
 "handful":"handful","handfuls":"handful","jar":"jar","jars":"jar","bottle":"bottle","bottles":"bottle",
 "bag":"bag","bags":"bag","box":"box","boxes":"box","pint":"pint","pints":"pint",
 "quart":"quart","quarts":"quart","gallon":"gallon","gallons":"gallon","scoop":"scoop","scoops":"scoop",
 "sheet":"sheet","sheets":"sheet","rib":"rib","ribs":"rib","ear":"ear","ears":"ear",
 "fillet":"fillet","fillets":"fillet","filet":"fillet","filets":"fillet","loaf":"loaf","loaves":"loaf",
 "wedge":"wedge","wedges":"wedge","bunch":"bunch","piece":"piece","pieces":"piece",
}
QTY_RE = re.compile(r"""^\s*(?:about|approx\.?|approximately|around|roughly)?\s*
    (?:(\d+)\s+)?                      # whole number before fraction
    (?:(\d+)\s*/\s*(\d+)|([\u00bd\u2153\u2154\u00bc\u00be\u2155-\u215e])|(\d+(?:\.\d+)?))?  # fraction/decimal
    \s*(?:(?:\u2013|\u2014|-|to)\s*(\d+(?:\.\d+)?))?\s*  # range upper
    (?:\(([^)]*)\))?\s*                # parenthetical e.g. (14-ounce)
    ([a-zA-Z.]+)?\s*                   # unit word candidate
    """, re.X)

def parse_qty(qstr):
    """Return (quantity, quantity_max) from leading qty text."""
    return None, None

def parse_ingredient(raw, position, group):
    r = raw.strip()
    optional = bool(re.search(r'\boptional\b', r, re.I))
    quantity = qmax = None
    unit = None
    rest = r
    m = QTY_RE.match(r)
    if m:
        whole, fn, fd, uf, dec, rng, paren, uword = m.groups()
        val = 0.0; got = False
        if whole: val += float(whole); got = True
        if fn and fd: val += float(fn)/float(fd); got = True
        if uf: val += FRAC[uf]; got = True
        if dec: val += float(dec); got = True
        if got:
            quantity = val if val else None
            if rng: qmax = float(rng)
            if uword:
                uw = uword.lower().rstrip('.')
                if uw in UNIT_MAP:
                    unit = UNIT_MAP[uw]
                    rest = r[m.end():]
                else:
                    rest = r[m.start(8):] if m.start(8) < len(r) else r[m.end():]
            else:
                rest = r[m.end():]
    # split off preparation after first comma
    prep = None
    if ',' in rest:
        item_part, prep_part = rest.split(',', 1)
        prep = re.sub(r'\s+', ' ', prep_part.strip()) or None
    else:
        item_part = rest
    item = re.sub(r'\([^)]*\)', '', item_part)
    item = re.sub(r'\s+', ' ', item).strip(' ,;-').lower()
    if not item:
        item = re.sub(r'\s+', ' ', r).strip().lower()
    return {"position": position, "raw": r, "group": group,
            "quantity": quantity, "unit": unit, "item": item,
            "preparation": prep, "optional": optional,
            **({"quantity_max": qmax} if qmax else {})}

GROUP_RE = re.compile(r'^\s*(?:for\s+(?:the\s+)?)?([^:0-9]{2,60}?)\s*:\s*$')
def parse_ingredients(lines):
    out, group, pos = [], None, 1
    for line in lines:
        if not isinstance(line, str):
            continue
        s = line.strip()
        if not s:
            continue
        gm = GROUP_RE.match(s)
        if gm and not re.search(r'\d', s):
            group = re.sub(r'\s+', ' ', gm.group(1)).strip().lower() or None
            continue
        out.append(parse_ingredient(s, pos, group)); pos += 1
    return out

DUR_RE = re.compile(r'^P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$', re.I)
def dur_minutes(s):
    if not s or not isinstance(s, str): return None
    m = DUR_RE.match(s.strip())
    if not m: return None
    d,h,mi,sec = (float(x) if x else 0 for x in m.groups())
    return int(round(d*1440 + h*60 + mi + sec/60)) or None

def parse_servings(y):
    if not y: return {"text": "", "min": None, "max": None, "yield_type": "servings"}
    if isinstance(y, list): y = y[0] if y else ""
    text = str(y).strip()
    mn = mx = None
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:to|\u2013|-)\s*(\d+(?:\.\d+)?)', text)
    if m: mn, mx = int(float(m.group(1))), int(float(m.group(2)))
    else:
        m = re.search(r'(\d+(?:\.\d+)?)', text)
        if m: mn = mx = int(float(m.group(1)))
    tl = text.lower()
    if re.search(r'\bcups?\b|\bpints?\b|\bquarts?\b|\bgallons?\b|\bliters?\b|\blitres?\b|\bml\b|quart', tl): yt = "volume"
    elif re.search(r'serving|portion', tl): yt = "servings"
    elif re.search(r'dozen|cookie|piece|bar|muffin|roll|bun|tart|cake|loaf|loaves|sandwich|dumpling|pancake|waffle|biscuit|croque|slider|ball|patty|square|slice', tl): yt = "pieces"
    elif mn is not None: yt = "other" if not re.search(r'serving', tl) else "servings"
    else: yt = "servings"
    return {"text": text, "min": mn, "max": mx, "yield_type": yt}

NUM_RE = re.compile(r'([-+]?[0-9]*\.?[0-9]+)')
def nut_num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    m = NUM_RE.search(str(v))
    return float(m.group(1)) if m else None

def parse_nutrition(n):
    if not isinstance(n, dict): return None
    rec = {"source": "nyt", "basis": "per_serving",
           "calories": nut_num(n.get("calories")),
           "protein_g": nut_num(n.get("proteinContent")),
           "carbohydrates_g": nut_num(n.get("carbohydrateContent")),
           "fat_g": nut_num(n.get("fatContent")),
           "saturated_fat_g": nut_num(n.get("saturatedFatContent")),
           "unsaturated_fat_g": nut_num(n.get("unsaturatedFatContent")),
           "trans_fat_g": nut_num(n.get("transFatContent")),
           "cholesterol_mg": nut_num(n.get("cholesterolContent")),
           "sodium_mg": nut_num(n.get("sodiumContent")),
           "fiber_g": nut_num(n.get("fiberContent")),
           "sugar_g": nut_num(n.get("sugarContent")),
           "computed_with": None}
    if all(v is None for k, v in rec.items() if k not in ("source", "basis", "computed_with")):
        return None
    return rec

def slugify_tag(t):
    t = t.strip().lower()
    t = re.sub(r'&', ' and ', t)
    t = re.sub(r'[^a-z0-9]+', '-', t).strip('-')
    return t

def parse_tags(j):
    def aslist(x):
        if not x: return []
        if isinstance(x, str): return [x]
        return [str(i) for i in x]
    DIET = {"vegetarian","vegan","gluten-free","dairy-free","kosher","nut-free","low-fat","low-carb","low-sodium","halal","pescatarian","paleo","keto","whole30","low-calorie","healthy","high-fiber","diabetic","egg-free","soy-free","shellfish-free","wheat-free","fat-free","sugar-free"}
    cuisine, course, diet, other = [], [], [], []
    for c in aslist(j.get("recipeCuisine")):
        s = slugify_tag(c)
        if s: cuisine.append(s)
    for c in aslist(j.get("recipeCategory")):
        s = slugify_tag(c)
        if s: course.append(s)
    kw = j.get("keywords")
    kws = []
    if isinstance(kw, str): kws = [k.strip() for k in kw.split(",")]
    elif isinstance(kw, list): kws = [str(k).strip() for k in kw]
    for k in kws:
        s = slugify_tag(k)
        if not s: continue
        (diet if s in DIET else other).append(s)
    ded = lambda l: sorted(set(l))
    return {"cuisine": ded(cuisine), "diet": ded(diet), "course": ded(course),
            "meal": [], "occasion": [], "other": ded(other)}

def parse_images(j):
    imgs = []
    im = j.get("image")
    items = im if isinstance(im, list) else ([im] if im else [])
    seen = set()
    for it in items:
        url = None; alt = ""
        if isinstance(it, str): url = it
        elif isinstance(it, dict):
            url = it.get("contentUrl") or it.get("url")
            alt = it.get("caption") or it.get("name") or ""
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            imgs.append({"url": url, "alt": str(alt), "primary": len(imgs) == 0})
    return imgs

def parse_steps(instr):
    out = []
    def add(text):
        t = re.sub(r'\s+', ' ', str(text)).strip()
        if t: out.append({"position": len(out)+1, "text": t})
    if isinstance(instr, str):
        for part in re.split(r'\n+', instr):
            add(part)
    elif isinstance(instr, list):
        for el in instr:
            if isinstance(el, str): add(el)
            elif isinstance(el, dict):
                t = el.get("@type")
                if t == "HowToSection":
                    for sub in el.get("itemListElement") or []:
                        if isinstance(sub, dict): add(sub.get("text") or sub.get("name") or "")
                        else: add(sub)
                else:
                    add(el.get("text") or el.get("name") or "")
    return out

JSONLD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
def extract_recipe_jsonld(html):
    for block in JSONLD_RE.findall(html):
        try:
            d = json.loads(block)
        except Exception:
            continue
        cands = []
        if isinstance(d, dict):
            if "@graph" in d: cands = d["@graph"]
            else: cands = [d]
        elif isinstance(d, list): cands = d
        for c in cands:
            if isinstance(c, dict) and c.get("@type") == "Recipe":
                return c
    return None

def build_record(url, html_text, scraped_at):
    m = re.match(r'^https?://cooking\.nytimes\.com/recipes/(\d+)-([a-z0-9-]*)', url)
    if not m: raise ValueError("bad url " + url)
    nyt_id = int(m.group(1))
    slug = (m.group(2) or "").lower()
    if not re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', slug):
        slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
    j = extract_recipe_jsonld(html_text)
    if j is None: raise ValueError("no Recipe JSON-LD")
    if not slug:
        ju = str(j.get("url") or "")
        jm = re.match(r'^https?://cooking\.nytimes\.com/recipes/(\d+)-([a-z0-9-]+)', ju)
        if jm: slug = jm.group(2).lower()
    if not slug:
        nm = re.sub(r'[^a-z0-9]+', '-', str(j.get("name") or "").lower()).strip('-')
        slug = nm if nm else "recipe-%d" % nyt_id
    # recipe-scrapers as fallback for core text fields
    rs_title = rs_author = None; rs_yields = None; rs_ing = None; rs_instr = None
    try:
        from recipe_scrapers import scrape_html
        s = scrape_html(html_text, url)
        for attr, setter in (("title","rs_title"),("author","rs_author")):
            try:
                v = getattr(s, attr)()
                if setter == "rs_title": rs_title = v
                else: rs_author = v
            except Exception: pass
        try: rs_yields = s.yields()
        except Exception: pass
        try: rs_ing = s.ingredients()
        except Exception: pass
        try: rs_instr = s.instructions()
        except Exception: pass
    except Exception:
        pass
    name = j.get("name") or rs_title or slug.replace("-", " ")
    name = htmlmod.unescape(str(name)).strip()
    au = j.get("author") or rs_author or ""
    if isinstance(au, dict): au = au.get("name", "")
    elif isinstance(au, list): au = ", ".join(str(a.get("name","") if isinstance(a,dict) else a) for a in au)
    desc = htmlmod.unescape(str(j.get("description") or "")).strip()
    # times
    raw_t = {}
    for k, lbl in (("prepTime","prep"),("cookTime","cook"),("totalTime","total")):
        if j.get(k): raw_t[lbl] = str(j[k])
    timings = {"prep_minutes": dur_minutes(j.get("prepTime")),
               "cook_minutes": dur_minutes(j.get("cookTime")),
               "total_minutes": dur_minutes(j.get("totalTime")), "raw": raw_t}
    ing_lines = j.get("recipeIngredient") or rs_ing or []
    ingredients = parse_ingredients(ing_lines)
    steps = parse_steps(j.get("recipeInstructions"))
    if not steps and rs_instr:
        steps = parse_steps(rs_instr)
    ar = j.get("aggregateRating")
    rating = None
    if isinstance(ar, dict):
        avg, cnt = nut_num(ar.get("ratingValue")), ar.get("ratingCount")
        try: cnt = int(float(cnt))
        except Exception: cnt = None
        if avg is not None and cnt is not None:
            rating = {"average": round(float(avg), 2), "count": cnt}
    vid = j.get("video")
    video_url = None
    if isinstance(vid, dict):
        video_url = vid.get("contentUrl") or vid.get("embedUrl") or vid.get("url")
    elif isinstance(vid, str): video_url = vid
    cc = j.get("commentCount")
    try: cc = int(cc) if cc is not None else None
    except Exception: cc = None
    pub = j.get("datePublished")
    published_at = str(pub)[:10] if pub and re.match(r'^\d{4}-\d{2}-\d{2}', str(pub)) else None
    rec = {"slug": slug, "nyt_id": nyt_id,
           "url": "https://cooking.nytimes.com/recipes/%d-%s" % (nyt_id, slug),
           "name": name, "author": str(au).strip(), "description": desc,
           "servings": parse_servings(j.get("recipeYield") or rs_yields),
           "time": timings, "ingredients": ingredients, "steps": steps,
           "nutrition": parse_nutrition(j.get("nutrition")),
           "tags": parse_tags(j), "images": parse_images(j), "rating": rating,
           "video_url": video_url, "comment_count": cc,
           "published_at": published_at, "scraped_at": scraped_at,
           "scraper_version": SCRAPER_VERSION}
    if not rec["name"]: raise ValueError("empty name")
    if not rec["ingredients"]: raise ValueError("no ingredients")
    return rec

# ---------- crawl loop ----------
def now_iso(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-urls", default="")
    args = ap.parse_args()
    wd = args.workdir
    for d in ("raw", "shards", "state", "logs"): os.makedirs(os.path.join(wd, d), exist_ok=True)
    done_path = os.path.join(wd, "state", "done.log")
    dead_path = os.path.join(wd, "state", "dead_letter.jsonl")
    status_path = os.path.join(wd, "state", "status.json")
    done = set()
    if os.path.exists(done_path):
        done = set(x.strip() for x in open(done_path) if x.strip())
    dead_urls = set()
    if os.path.exists(dead_path):
        for line in open(dead_path):
            try: dead_urls.add(json.loads(line)["url"])
            except Exception: pass
    urls = [u.strip() for u in open(args.manifest) if u.strip()]
    if args.only_urls:
        keep = set(args.only_urls.split(",")); urls = [u for u in urls if u in keep]
    todo = [u for u in urls if u not in done and u not in dead_urls]
    if args.limit: todo = todo[:args.limit]
    total_all = len(urls)
    log = open(os.path.join(wd, "logs", "crawl.log"), "a", buffering=1)
    def L(msg):
        line = "[%s] %s" % (now_iso(), msg)
        print(line, flush=True); log.write(line + "\n")
    L("start: %d urls total, %d done, %d dead, %d todo" % (total_all, len(done), len(dead_urls), len(todo)))
    sess = requests.Session(); sess.headers.update(HEADERS)
    last_req = 0.0; n_done = len(done); n_dead = len(dead_urls); t_start = time.time(); n_session = 0
    done_f = open(done_path, "a", buffering=1)
    dead_f = open(dead_path, "a", buffering=1)
    for i, url in enumerate(todo, 1):
        mid = re.match(r'^https?://cooking\.nytimes\.com/recipes/(\d+)-', url)
        rid = mid.group(1) if mid else re.sub(r'\W+', '_', url)
        html_text = None; status = None; attempts = 0
        while True:
            wait = PACE_SECS + random.uniform(0, 0.4) - (time.time() - last_req)
            if wait > 0: time.sleep(wait)
            try:
                last_req = time.time()
                r = sess.get(url, timeout=30)
                status = r.status_code
                if status == 200:
                    html_text = r.text
                    fr = re.match(r'^https?://cooking\.nytimes\.com/recipes/(\d+)-([a-z0-9-]+)', r.url)
                    if fr: url = r.url
                    break
                if status in RETRY_STATUSES and attempts < len(BACKOFFS):
                    pause = BACKOFFS[attempts]; attempts += 1
                    L("RETRY %s http %d -> backoff %ds (attempt %d)" % (rid, status, pause, attempts))
                    time.sleep(pause); continue
                break  # other status -> dead letter
            except requests.RequestException as e:
                status = None
                if attempts < len(BACKOFFS):
                    pause = BACKOFFS[attempts]; attempts += 1
                    L("RETRY %s neterr %s -> backoff %ds (attempt %d)" % (rid, type(e).__name__, pause, attempts))
                    time.sleep(pause); continue
                break
        if html_text is None:
            dead_f.write(json.dumps({"url": url, "nyt_id": rid, "reason": "http_%s" % status if status else "network_error", "attempts": attempts + 1, "ts": now_iso()}) + "\n"); dead_f.flush()
            n_dead += 1
            L("DEAD %s %s (status=%s)" % (n_dead, url, status))
            continue
        try:
            raw_path = os.path.join(wd, "raw", "%s.html.gz" % rid)
            with gzip.open(raw_path, "wt", encoding="utf-8") as f: f.write(html_text)
            rec = build_record(url, html_text, now_iso())
            shard = os.path.join(wd, "shards", "%s.a.jsonl" % rec["slug"][:2])
            with open(shard, "a", buffering=1) as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done_f.write(url + "\n"); done_f.flush()
            n_done += 1; n_session += 1
            if n_done % 25 == 0 or i == 1:
                rate = n_session / max(time.time() - t_start, 1) * 60
                eta = (total_all - n_done - n_dead) / max(rate, 0.01)
                json.dump({"done": n_done, "dead": n_dead, "total": total_all,
                           "rate_per_min": round(rate, 1), "eta_min": round(eta, 1),
                           "last_url": url, "ts": now_iso()}, open(status_path, "w"))
                L("PROGRESS %d/%d done, %d dead, %.1f/min, ETA %.0f min" % (n_done, total_all, n_dead, rate, eta))
        except Exception as e:
            dead_f.write(json.dumps({"url": url, "nyt_id": rid, "reason": "parse_error", "detail": "%s: %s" % (type(e).__name__, e), "attempts": attempts + 1, "ts": now_iso()}) + "\n"); dead_f.flush()
            n_dead += 1
            L("DEAD %s %s parse_error %s" % (n_dead, url, e))
    L("finished loop: %d done, %d dead" % (n_done, n_dead))

if __name__ == "__main__":
    main()
