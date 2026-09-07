#!/usr/bin/env python3
"""One bounded re-fetch pass for the duplicate_slug collision losers.

Reads crawl-state/dead-letter-b.jsonl entries with reason=duplicate_slug,
re-fetches each loser URL with the same polite pacing/backoff as the crawlers,
parses with crawler B's NEXT_DATA-primary parser (verbatim), and classifies
each loser against the winner record that was kept under the same slug:

  - distinct recipe      -> append result with class=distinct (recovered)
  - same recipe re-issue -> class=reissue (stays out of the dataset)
  - fetch/parse failure  -> class=failed (noted, not papered over)

Results stream into crawl-state/refetch-results.jsonl (resumable: nyt_ids
already present are skipped). `--apply` turns a complete result set into
data/shards/<prefix>.c.jsonl records + a rewritten dead-letter-b.jsonl +
crawl-state/refetch-collisions-report.json.
"""
import argparse, datetime, json, os, random, re, sys, time
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate",
           "DNT": "1", "Upgrade-Insecure-Requests": "1"}
PACE_SECS = 2.0
BACKOFFS = [30, 120, 600]
RETRY_STATUSES = {429, 500, 502, 503, 504}

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DL_B = os.path.join(REPO, "crawl-state", "dead-letter-b.jsonl")
RESULTS = os.path.join(REPO, "crawl-state", "refetch-results.jsonl")
REPORT = os.path.join(REPO, "crawl-state", "refetch-collisions-report.json")
SHARDS = os.path.join(REPO, "data", "shards")
RAW_CACHE = "/tmp/refetch_raw"

# ---------------- crawler B's NEXT_DATA-primary parser (Sep 6 version, verbatim) ----------------
SCRAPER_VERSION = "1.0.0"
FRAC = {"½":0.5,"⅓":1/3,"¼":0.25,"¾":0.75,"⅔":2/3,"⅛":0.125,"⅜":0.375,"⅝":0.625,"⅞":0.875,
        "⅕":0.2,"⅖":0.4,"⅗":0.6,"⅘":0.8,"⅙":1/6,"⅚":5/6}
UNITS = {
 "cup":"cup","cups":"cup","tablespoon":"tbsp","tablespoons":"tbsp","tbsp":"tbsp",
 "teaspoon":"tsp","teaspoons":"tsp","tsp":"tsp","ounce":"oz","ounces":"oz","oz":"oz",
 "pound":"lb","pounds":"lb","lb":"lb","lbs":"lb","gram":"g","grams":"g","g":"g",
 "kilogram":"kg","kilograms":"kg","kg":"kg","milliliter":"ml","milliliters":"ml","ml":"ml",
 "liter":"l","liters":"l","l":"l","clove":"clove","cloves":"clove","can":"can","cans":"can",
 "bunch":"bunch","bunches":"bunch","package":"package","packages":"package",
 "pint":"pint","pints":"pint","quart":"quart","quarts":"quart","gallon":"gallon","gallons":"gallon",
 "stick":"stick","sticks":"stick","slice":"slice","slices":"slice","piece":"piece","pieces":"piece",
 "head":"head","heads":"head","stalk":"stalk","stalks":"stalk","sprig":"sprig","sprigs":"sprig",
 "pinch":"pinch","pinches":"pinch","dash":"dash","dashes":"dash","handful":"handful","handfuls":"handful",
 "jar":"jar","jars":"jar","bottle":"bottle","bottles":"bottle","box":"box","boxes":"box",
 "bag":"bag","bags":"bag","sheet":"sheet","sheets":"sheet","strip":"strip","strips":"strip",
 "scoop":"scoop","scoops":"scoop","envelope":"envelope","envelopes":"envelope",
 "container":"container","containers":"container","carton":"carton","cartons":"carton",
 "rack":"rack","racks":"rack","fillet":"fillet","fillets":"fillet",
}
VOLUME_UNITS = {"cup","cups","ml","l","liter","liters","milliliter","milliliters","pint","pints",
                "quart","quarts","gallon","gallons","tablespoon","tablespoons","teaspoon","teaspoons",
                "fluid ounce","fluid ounces"}
TAG_MAP = {"CUISINE":"cuisine","DIET":"diet","COURSE":"course","MEAL":"meal","MEAL_TYPE":"meal","OCCASION":"occasion"}

def iso_to_min(s):
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", s)
    if not m:
        return None
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    total = d*1440 + h*60 + mi
    return total if total > 0 else None

def doc_text(node):
    """Plain text from a ProseMirror-ish node."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [doc_text(c) for c in node.get("content", []) or []]
        return "".join(parts)
    return ""

def doc_paragraphs(doc):
    """List of paragraph texts from a body doc."""
    out = []
    for c in (doc.get("content") or []):
        t = doc_text(c).strip()
        if t:
            out.append(t)
    return out

def parse_ingredients_doc(idoc):
    """Return list of (group, raw_text) preserving order."""
    items = []
    group = None
    def walk(nodes):
        nonlocal group
        for n in nodes:
            if not isinstance(n, dict):
                continue
            t = n.get("type")
            if t == "list_item":
                txt = doc_text(n).strip()
                if txt:
                    items.append((group, txt))
            elif t in ("bullet_list", "ordered_list"):
                walk(n.get("content") or [])
            elif t in ("paragraph", "heading"):
                txt = doc_text(n).strip()
                if txt:
                    g = re.sub(r"^for\s+(the\s+)?", "", txt, flags=re.I).rstrip(":").strip()
                    group = g.lower() if g else group
            else:
                if n.get("content"):
                    walk(n["content"])
    walk((idoc.get("doc") or {}).get("content") or [])
    return items

def parse_quantity(s):
    """Parse leading quantity from ingredient line. Returns (qty, qty_max, rest)."""
    s = s.strip()
    frac_class = "".join(FRAC.keys())
    # mixed number + fraction, range: "1 ½ - 2 ½", "1-2", "1 to 2", "½ to ¾"
    pat = re.compile(
        rf"^(\d+\s+[{frac_class}]|\d*\.\d+|\d+|[{frac_class}]|\d+/\d+)"
        rf"(?:\s*(?:–|-|to)\s*(\d+\s+[{frac_class}]|\d*\.\d+|\d+|[{frac_class}]|\d+/\d+))?"
        rf"\s*(.*)$", re.I)
    m = pat.match(s)
    if not m:
        return None, None, s
    def val(x):
        x = x.strip()
        if "/" in x and re.match(r"^\d+/\d+$", x):
            a, b = x.split("/"); return float(a)/float(b)
        for f, v in FRAC.items():
            if f in x:
                whole = x.replace(f, "").strip()
                return (float(whole) if whole else 0.0) + v
        return float(x)
    try:
        q = val(m.group(1))
        qmax = val(m.group(2)) if m.group(2) else None
    except Exception:
        return None, None, s
    return q, qmax, m.group(3)

def parse_ingredient(position, group, raw):
    optional = False
    line = raw.strip()
    if re.search(r",\s*optional\s*\.?$", line, re.I) or re.search(r"\(optional\)", line, re.I):
        optional = True
        line = re.sub(r",?\s*\(?optional\)?\s*\.?$", "", line, flags=re.I).strip()
    qty = qmax = None
    unit = None
    rest = line
    if not re.search(r"\bto taste\b|\bas needed\b|\bfor serving\b|\bfor garnish\b", line, re.I) or re.match(r"^[\d½⅓¼¾⅔⅛⅜⅝⅞]", line):
        q, qm, r = parse_quantity(line)
        if q is not None:
            qty, qmax, rest = q, qm, r
            words = rest.strip().split(None, 1)
            if words:
                w0 = words[0].lower().rstrip(".")
                if w0 in UNITS:
                    unit = UNITS[w0]
                    rest = words[1] if len(words) > 1 else ""
    # split item / preparation on first comma
    item, prep = rest, None
    if "," in rest:
        item, prep = rest.split(",", 1)
        prep = prep.strip().rstrip(".") or None
    item = re.sub(r"\s+", " ", item).strip().strip(".").lower()
    if not item:
        item = re.sub(r"\s+", " ", line).strip().lower()
    return {"position": position, "raw": raw, "group": group, "quantity": qty,
            "quantity_max": qmax, "unit": unit, "item": item, "preparation": prep,
            "optional": optional}

def servings_from_yield(y):
    text, mn, mx, ytype = "", None, None, "servings"
    if isinstance(y, dict):
        text = y.get("notes") or ""
        sv = (y.get("servings") or [])
        if sv:
            s0 = sv[0]
            cnt = s0.get("count")
            rng = s0.get("rangeEnd")
            u = (s0.get("unit") or "").lower()
            if cnt is not None:
                mn = int(cnt) if float(cnt).is_integer() else cnt
                mx = int(rng) if rng else mn
            if not text:
                text = f"{s0.get('count')}" + (f"-{rng}" if rng else "") + f" {s0.get('unit') or 'servings'}"
            if u in ("serving", "servings"):
                ytype = "servings"
            elif u in VOLUME_UNITS:
                ytype = "volume"
            else:
                ytype = "pieces"
    return {"text": text, "min": mn, "max": mx, "yield_type": ytype}

def nutrition_from_scoop(ni):
    if not isinstance(ni, dict):
        return None
    nuts = ni.get("perServingNutrients") or []
    if not nuts:
        return None
    code_map = {"CALORIE":"calories","PROTEIN":"protein_g","CARBOHYDRATE":"carbohydrates_g",
                "FAT":"fat_g","SATURATED_FAT":"saturated_fat_g","TRANS_FAT":"trans_fat_g",
                "CHOLESTEROL":"cholesterol_mg","SODIUM":"sodium_mg","FIBER":"fiber_g","SUGAR":"sugar_g"}
    out = {v: None for v in code_map.values()}
    out["unsaturated_fat_g"] = None
    mono = poly = None
    for n in nuts:
        c, q = n.get("code"), n.get("quantity")
        if q is None:
            continue
        if c in code_map:
            out[code_map[c]] = round(float(q), 1)
        elif c == "MONOUNSATURATED_FAT":
            mono = float(q)
        elif c == "POLYUNSATURATED_FAT":
            poly = float(q)
    if mono is not None or poly is not None:
        out["unsaturated_fat_g"] = round((mono or 0) + (poly or 0), 1)
    out.update({"source": "nyt", "basis": "per_serving", "computed_with": None})
    return out

def image_url(img):
    if not isinstance(img, dict):
        return None
    for key in ("ogCrops", "crops"):
        for crop in img.get(key) or []:
            rends = crop.get("renditions") or []
            if rends and rends[0].get("url"):
                return rends[0]["url"]
    for key in ("url", "contentUrl"):
        if img.get(key):
            return img[key]
    return None

def parse_record(html, url, nyt_id, slug, scraped_at):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    sr = None
    if m:
        try:
            sr = json.loads(m.group(1))["props"]["pageProps"]["scoopRecipe"]
        except Exception:
            sr = None
    # JSON-LD fallback source
    ld = {}
    for b in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(b)
            if isinstance(d, dict) and d.get("@type") == "Recipe":
                ld = d
                break
        except Exception:
            continue
    if sr is None and not ld:
        raise ValueError("no scoopRecipe and no JSON-LD Recipe")

    name = (sr or {}).get("title") or ld.get("name") or ""
    if not name:
        raise ValueError("no name")
    # author
    author = ""
    if sr is not None:
        names = [b.get("displayName") for b in sr.get("bylines") or [] if b.get("displayName")]
        author = ", ".join(names)
    if not author:
        a = ld.get("author")
        if isinstance(a, dict):
            author = a.get("name", "")
        elif isinstance(a, list):
            author = ", ".join(x.get("name", "") if isinstance(x, dict) else str(x) for x in a)
        elif isinstance(a, str):
            author = a
    # description
    desc = ""
    if sr is not None:
        hn = sr.get("headnote") or {}
        desc = "\
\
".join(doc_paragraphs(hn.get("doc") or {}))
    if not desc:
        desc = ld.get("description") or ""
    # servings
    servings = servings_from_yield((sr or {}).get("yield")) if sr is not None else {"text":"","min":None,"max":None,"yield_type":"servings"}
    if not servings["text"]:
        ry = ld.get("recipeYield")
        if isinstance(ry, list):
            ry = ry[0] if ry else None
        if ry:
            servings = {"text": str(ry), "min": None, "max": None, "yield_type": "servings"}
            mm = re.match(r"(\d+)", str(ry))
            if mm:
                servings["min"] = servings["max"] = int(mm.group(1))
    # time
    raw_t, prep_m, cook_m, tot_m = {}, None, None, None
    if sr is not None:
        t = sr.get("time") or {}
        lt = sr.get("legacyTime") or {}
        prep_m = iso_to_min(t.get("prepTime"))
        cook_m = iso_to_min(t.get("cookTime"))
        tot_m = iso_to_min(t.get("totalTime"))
        if t.get("readyIn"):
            raw_t["total"] = t["readyIn"]
        for k_src, k_dst in (("prepTime","prep"),("cookTime","cook"),("totalTime","total")):
            if t.get(k_src):
                raw_t.setdefault(k_dst, t[k_src])
        if tot_m is None and isinstance(lt, dict):
            tt = lt.get("totalTime") or {}
            if tt.get("minutes"):
                tot_m = int(tt["minutes"])
            if tt.get("display"):
                raw_t.setdefault("total", tt["display"])
            pt = lt.get("prepTime") or {}
            if pt.get("minutes"):
                prep_m = prep_m or int(pt["minutes"])
            if pt.get("display"):
                raw_t.setdefault("prep", pt["display"])
            ct = (lt.get("cookTime") or lt.get("cookingTime")) or {}
            if ct.get("minutes"):
                cook_m = cook_m or int(ct["minutes"])
            if ct.get("display"):
                raw_t.setdefault("cook", ct["display"])
    if tot_m is None:
        tot_m = iso_to_min(ld.get("totalTime"))
        prep_m = prep_m if prep_m is not None else iso_to_min(ld.get("prepTime"))
        cook_m = cook_m if cook_m is not None else iso_to_min(ld.get("cookTime"))
        for k in ("prepTime","cookTime","totalTime"):
            if ld.get(k):
                raw_t.setdefault(k.replace("Time",""), ld[k])
    time_obj = {"prep_minutes": prep_m, "cook_minutes": cook_m, "total_minutes": tot_m, "raw": raw_t}
    # ingredients
    ingredients = []
    if sr is not None:
        for pos, (grp, raw) in enumerate(parse_ingredients_doc(sr.get("ingredients") or {}), 1):
            ingredients.append(parse_ingredient(pos, grp, raw))
    if not ingredients:
        for pos, raw in enumerate(ld.get("recipeIngredient") or [], 1):
            ingredients.append(parse_ingredient(pos, None, raw))
    if not ingredients:
        raise ValueError("no ingredients")
    # steps
    steps = []
    if sr is not None:
        prep_doc = (sr.get("preparation") or {}).get("doc") or {}
        pos = 0
        for c in prep_doc.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "step":
                txt = doc_text(c).strip()
                if txt:
                    pos += 1
                    steps.append({"position": pos, "text": txt})
    if not steps:
        ri = ld.get("recipeInstructions") or []
        pos = 0
        for st in ri:
            txt = st.get("text") if isinstance(st, dict) else str(st)
            txt = (txt or "").strip()
            if txt:
                pos += 1
                steps.append({"position": pos, "text": txt})
    # nutrition
    nutrition = nutrition_from_scoop((sr or {}).get("nutritionalInfo")) if sr is not None else None
    if nutrition is None and isinstance(ld.get("nutrition"), dict):
        n = ld["nutrition"]
        def num(key):
            v = n.get(key)
            if v is None: return None
            mm = re.search(r"[\d.]+", str(v))
            return float(mm.group()) if mm else None
        nutrition = {"source":"nyt","basis":"per_serving","calories":num("calories"),
                     "protein_g":num("proteinContent"),"carbohydrates_g":num("carbohydrateContent"),
                     "fat_g":num("fatContent"),"saturated_fat_g":num("saturatedFatContent"),
                     "unsaturated_fat_g":num("unsaturatedFatContent"),"trans_fat_g":num("transFatContent"),
                     "cholesterol_mg":num("cholesterolContent"),"sodium_mg":num("sodiumContent"),
                     "fiber_g":num("fiberContent"),"sugar_g":num("sugarContent"),"computed_with":None}
    # tags
    tags = {"cuisine":[],"diet":[],"course":[],"meal":[],"occasion":[],"other":[]}
    if sr is not None:
        for t in sr.get("tags") or []:
            cat = TAG_MAP.get(t.get("type"), "other")
            nm = t.get("normalizedName") or (t.get("name") or "").lower().replace(" ", "-")
            if nm and nm not in tags[cat]:
                tags[cat].append(nm)
    else:
        cat = ld.get("recipeCategory")
        if cat:
            for c in (cat if isinstance(cat, list) else [cat]):
                tags["course"].append(str(c).lower().replace(" ", "-"))
        kw = ld.get("keywords")
        if isinstance(kw, str):
            kw = [k.strip() for k in kw.split(",")]
        for k in kw or []:
            tags["other"].append(str(k).lower().replace(" ", "-"))
    # images
    images = []
    if sr is not None:
        med = sr.get("media") or {}
        hero = med.get("heroImage")
        u = image_url(hero)
        if u:
            images.append({"url": u, "alt": (hero.get("alt") or hero.get("caption") or ""), "primary": True})
        for img in med.get("images") or []:
            u = image_url(img)
            if u and all(x["url"] != u for x in images):
                images.append({"url": u, "alt": (img.get("alt") or ""), "primary": not images})
    if not images:
        li = ld.get("image")
        for img in (li if isinstance(li, list) else [li] if li else []):
            if isinstance(img, dict):
                u = img.get("contentUrl") or img.get("url")
                alt = img.get("name") or ""
            else:
                u, alt = img, ""
            if u and "recipe-wallpaper" not in u:
                images.append({"url": u, "alt": alt, "primary": not images})
    # rating
    rating = None
    if sr is not None:
        r = sr.get("rating")
        if isinstance(r, dict) and r.get("count"):
            rating = {"average": float(r.get("roundedAverage") or 0), "count": int(r["count"])}
    if rating is None and isinstance(ld.get("aggregateRating"), dict):
        ar = ld["aggregateRating"]
        if ar.get("ratingCount"):
            rating = {"average": float(ar.get("ratingValue") or 0), "count": int(ar["ratingCount"])}
    # video
    video_url = None
    if sr is not None:
        vids = (sr.get("media") or {}).get("videos") or []
        for v in vids:
            if isinstance(v, dict):
                video_url = v.get("url") or v.get("contentUrl") or v.get("embedUrl")
                if video_url:
                    break
    if not video_url:
        v = ld.get("video")
        if isinstance(v, dict):
            video_url = v.get("contentUrl") or v.get("embedUrl") or v.get("url")
    # published
    published = None
    if sr is not None:
        pi = (sr.get("publishInfo") or {}).get("firstPublishedAt")
        if pi:
            published = pi[:10]
    if not published and ld.get("datePublished"):
        published = str(ld["datePublished"])[:10]
    if published and not re.match(r"^\d{4}-\d{2}-\d{2}$", published):
        published = None

    return {
        "slug": slug, "nyt_id": int(nyt_id), "url": url, "name": name, "author": author,
        "description": desc, "servings": servings, "time": time_obj,
        "ingredients": ingredients, "steps": steps, "nutrition": nutrition,
        "tags": tags, "images": images, "rating": rating,
        "video_url": video_url, "comment_count": None,
        "published_at": published, "scraped_at": scraped_at,
        "scraper_version": SCRAPER_VERSION,
    }

def parse_html(html, fallback_nid):
    """B's parse path: canonical url + slug recovered from the page, then
    parse_record (NEXT_DATA primary, JSON-LD fallback)."""
    nid = str(fallback_nid)
    m = re.search(r'"url"\s*:\s*"//cooking\.nytimes\.com/recipes/(\d+)-([a-z0-9-]+)"', html)
    if m:
        url = "https://cooking.nytimes.com/recipes/%s-%s" % (m.group(1), m.group(2))
        slug = m.group(2)
    else:
        # some pages carry a capitalized slug; fall back to the canonical link
        mc = re.search(r'<link rel="canonical" href="https://cooking\.nytimes\.com/recipes/(\d+)-([^"]+)"', html)
        if mc:
            url = "https://cooking.nytimes.com/recipes/%s-%s" % (mc.group(1), mc.group(2))
            slug = mc.group(2)
        else:
            slug = "unknown"
            url = "https://cooking.nytimes.com/recipes/%s" % nid
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", slug):
        cleaned = re.sub(r"[^a-z0-9-]", "", slug.lower().replace(" ", "-"))
        slug = cleaned or slug or "unknown"  # keep original case if cleaning empties
    scraped_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return parse_record(html, url, nid, slug, scraped_at)

# ---------------- pass ----------------
def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_losers():
    out = []
    for line in open(DL_B):
        line = line.strip()
        if not line: continue
        r = json.loads(line)
        if r.get("reason") == "duplicate_slug":
            out.append(r)
    return out

def load_winners():
    """nyt_id -> record, from every shard file."""
    winners = {}
    for name in os.listdir(SHARDS):
        if not name.endswith(".jsonl"): continue
        for line in open(os.path.join(SHARDS, name)):
            line = line.strip()
            if not line: continue
            r = json.loads(line)
            winners[int(r["nyt_id"])] = r
    return winners

def norm_ingredients(rec):
    return [ (i.get("raw") or "").strip().lower() for i in rec.get("ingredients") or [] ]

def classify(loser_rec, winner_rec):
    """Crawler B's rule: same name AND same author as the kept record means a
    same-recipe re-issue (NYT republished it under a new id, sometimes with
    edited ingredient lists). Different name or author = distinct recipe."""
    if winner_rec is None:
        return "distinct"  # no comparison record; keep (keyed by nyt_id)
    same_name = (loser_rec.get("name") or "").strip().lower() == (winner_rec.get("name") or "").strip().lower()
    same_author = (loser_rec.get("author") or "").strip().lower() == (winner_rec.get("author") or "").strip().lower()
    return "reissue" if (same_name and same_author) else "distinct"

def fetch_one(sess, url, state):
    attempts = 0
    while True:
        wait = PACE_SECS + random.uniform(0, 0.4) - (time.time() - state["last_req"])
        if wait > 0: time.sleep(wait)
        try:
            state["last_req"] = time.time()
            r = sess.get(url, timeout=30)
            if r.status_code == 200:
                return r.text, r.url, 200, attempts
            if r.status_code in RETRY_STATUSES and attempts < len(BACKOFFS):
                pause = BACKOFFS[attempts]; attempts += 1
                print("[%s] RETRY %s http %d -> %ds" % (now_iso(), url, r.status_code, pause), flush=True)
                time.sleep(pause); continue
            return None, url, r.status_code, attempts
        except requests.RequestException as e:
            if attempts < len(BACKOFFS):
                pause = BACKOFFS[attempts]; attempts += 1
                print("[%s] RETRY %s neterr %s -> %ds" % (now_iso(), url, type(e).__name__, pause), flush=True)
                time.sleep(pause); continue
            return None, url, None, attempts

def run_pass():
    losers = load_losers()
    done_ids = set()
    if os.path.exists(RESULTS):
        for line in open(RESULTS):
            line = line.strip()
            if line:
                try: done_ids.add(int(json.loads(line)["nyt_id"]))
                except Exception: pass
    todo = [l for l in losers if int(l["nyt_id"]) not in done_ids]
    print("[%s] %d losers, %d already fetched, %d todo" % (now_iso(), len(losers), len(done_ids), len(todo)), flush=True)
    if not todo: return
    winners = load_winners()
    os.makedirs(RAW_CACHE, exist_ok=True)
    sess = requests.Session(); sess.headers.update(HEADERS)
    state = {"last_req": 0.0}
    out = open(RESULTS, "a", buffering=1)
    for i, l in enumerate(todo, 1):
        nid = int(l["nyt_id"]); url = l["url"]
        html, final_url, status, attempts = fetch_one(sess, url, state)
        if html is None:
            rec_out = {"nyt_id": nid, "url": url, "class": "failed",
                       "reason": "http_%s" % status if status else "network_error",
                       "ts": now_iso()}
            out.write(json.dumps(rec_out) + "\n")
            print("[%s] (%d/%d) FAILED %s status=%s" % (now_iso(), i, len(todo), url, status), flush=True)
            continue
        with open(os.path.join(RAW_CACHE, "%d.html" % nid), "w", encoding="utf-8") as f:
            f.write(html)
        try:
            rec = parse_html(html, nid)
            rec["nyt_id"] = nid  # key by the loser's own id, never the page's canonical id
            cls = classify(rec, winners.get(int(l["record_kept_under_nyt_id"])) if l.get("record_kept_under_nyt_id") else None)
            out.write(json.dumps({"nyt_id": nid, "url": url, "class": cls, "record": rec, "ts": now_iso()}, ensure_ascii=False) + "\n")
            print("[%s] (%d/%d) %s %s (%s)" % (now_iso(), i, len(todo), cls.upper(), url, rec.get("name","")[:60]), flush=True)
        except Exception as e:
            out.write(json.dumps({"nyt_id": nid, "url": url, "class": "failed",
                                  "reason": "parse_error", "detail": "%s: %s" % (type(e).__name__, e),
                                  "ts": now_iso()}) + "\n")
            print("[%s] (%d/%d) PARSE-FAIL %s %s" % (now_iso(), i, len(todo), url, e), flush=True)
    out.close()

def apply_results():
    results = {}
    for line in open(RESULTS):
        line = line.strip()
        if line:
            r = json.loads(line); results[int(r["nyt_id"])] = r
    losers = load_losers()
    missing = [int(l["nyt_id"]) for l in losers if int(l["nyt_id"]) not in results]
    if missing:
        print("apply: %d losers have no result yet: %s" % (len(missing), missing[:10]), file=sys.stderr)
        return 1
    # recompute classification with the current rule (results file classes may
    # be from an older rule); fetch/parse failures stay failed
    winners_by_loser = {}
    for l in losers:
        if l.get("record_kept_under_nyt_id"):
            winners_by_loser[int(l["nyt_id"])] = int(l["record_kept_under_nyt_id"])
    winners = load_winners()
    for nid, r in results.items():
        if r["class"] == "failed": continue
        r["class"] = classify(r["record"], winners.get(winners_by_loser.get(nid, -1)))
    distinct = sorted(n for n, r in results.items() if r["class"] == "distinct")
    reissue = sorted(n for n, r in results.items() if r["class"] == "reissue")
    failed = sorted(n for n, r in results.items() if r["class"] == "failed")
    # 1. write recovered records into per-crawler shards <prefix>.c.jsonl, keyed by nyt_id
    by_shard = {}
    for nid in distinct:
        rec = results[nid]["record"]
        by_shard.setdefault(rec["slug"][:2].lower(), {})[nid] = rec
    for prefix, recs in sorted(by_shard.items()):
        path = os.path.join(SHARDS, "%s.c.jsonl" % prefix)
        existing = {}
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line:
                    r = json.loads(line); existing[int(r["nyt_id"])] = r
        existing.update(recs)
        with open(path, "w", encoding="utf-8") as f:
            for r in sorted(existing.values(), key=lambda x: (x["slug"], int(x["nyt_id"]))):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("apply: wrote %s (%d records)" % (path, len(existing)), file=sys.stderr)
    # 2. rewrite dead-letter-b.jsonl: drop recovered, annotate re-issues, update failures
    kept = []
    for line in open(DL_B):
        line = line.strip()
        if not line: continue
        r = json.loads(line)
        if r.get("reason") != "duplicate_slug":
            kept.append(r); continue
        nid = int(r["nyt_id"]); res = results[nid]
        if res["class"] == "distinct":
            continue  # recovered into shards
        if res["class"] == "reissue":
            r["classification"] = "same_recipe_reissue"
            r["refetch_ts"] = res["ts"]
            kept.append(r)
        else:
            r["reason"] = "duplicate_slug_refetch_%s" % res["reason"]
            if res.get("detail"): r["detail"] = res["detail"]
            r["refetch_ts"] = res["ts"]
            kept.append(r)
    with open(DL_B, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 3. report
    reissue_identical = 0
    for line in open(DL_B):
        line = line.strip()
        if not line: continue
        r = json.loads(line)
        if r.get("reason") != "duplicate_slug": continue
        nid = int(r["nyt_id"])
        if results[nid]["class"] == "reissue":
            w = winners.get(int(r["record_kept_under_nyt_id"]))
            if w and norm_ingredients(results[nid]["record"]) == norm_ingredients(w):
                reissue_identical += 1
    report = {
        "generated_at": now_iso(),
        "classification_rule": "same name AND same author as kept record = same-recipe re-issue (crawler B's crawl-time rule)",
        "reissues_with_identical_ingredients": reissue_identical,
        "duplicate_slug_losers": len(results),
        "distinct_recovered": distinct,
        "same_recipe_reissues": reissue,
        "failed": [{"nyt_id": n, "url": results[n]["url"], "reason": results[n]["reason"],
                    **({"detail": results[n]["detail"]} if results[n].get("detail") else {})} for n in failed],
    }
    json.dump(report, open(REPORT, "w"), indent=2)
    print("apply: distinct=%d reissue=%d failed=%d" % (len(distinct), len(reissue), len(failed)), file=sys.stderr)
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply:
        sys.exit(apply_results())
    run_pass()
