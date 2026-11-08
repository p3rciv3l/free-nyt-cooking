#!/usr/bin/env python3
"""scrape.py - crawl NYT Cooking recipe pages into canonical JSONL shards.

Reads a URL manifest (one recipe URL per line), fetches each page politely
(raw HTML cached on disk so re-runs are cheap and re-parses need no network),
parses with recipe-scrapers (JSON-LD fallback), and writes canonical records
(SCHEMA.md) into data/shards/<2-char-slug-prefix>.jsonl, full-record replace
keyed on slug, lines sorted by slug.

Requires: pip install recipe-scrapers   (the only non-stdlib dependency)

Usage:
    python3 tools/scrape.py --urls urls.txt [--delay 2.0] [--limit N]
                            [--data-dir data/shards] [--cache-dir .cache/html]
                            [--checkpoint .cache/checkpoint.json] [--reparse-only]

Checkpointing: every fetched/parsed URL is recorded; re-running resumes where
it stopped. Shards are rewritten after every batch (default 25 records).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCRAPER_VERSION = "0.1.0"
USER_AGENT = ("free-nyt-cooking-research-bot/0.1 "
              "(personal recipe archive; polite 1 req / --delay seconds)")

UNITS = {
    "cup": "cup", "cups": "cup",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "tbsp.": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp", "tsp.": "tsp",
    "ounce": "oz", "ounces": "oz", "oz": "oz", "oz.": "oz",
    "pound": "lb", "pounds": "lb", "lb": "lb", "lb.": "lb", "lbs": "lb",
    "gram": "g", "grams": "g", "g": "g",
    "kilogram": "kg", "kilograms": "kg", "kg": "kg",
    "milliliter": "ml", "milliliters": "ml", "ml": "ml",
    "liter": "l", "liters": "l", "l": "l",
    "clove": "clove", "cloves": "clove",
    "can": "can", "cans": "can",
    "bunch": "bunch", "bunches": "bunch",
    "package": "package", "packages": "package", "pkg": "package",
    "slice": "slice", "slices": "slice",
    "sprig": "sprig", "sprigs": "sprig",
    "stalk": "stalk", "stalks": "stalk",
    "stick": "stick", "sticks": "stick",
    "pinch": "pinch", "pinches": "pinch",
    "dash": "dash", "dashes": "dash",
    "head": "head", "heads": "head",
    "quart": "quart", "quarts": "quart", "qt": "quart",
    "pint": "pint", "pints": "pint", "pt": "pint",
    "gallon": "gallon", "gallons": "gallon", "gal": "gallon",
    "jar": "jar", "jars": "jar",
    "bottle": "bottle", "bottles": "bottle",
    "sheet": "sheet", "sheets": "sheet",
    "piece": "piece", "pieces": "piece",
    "scoop": "scoop", "scoops": "scoop",
}

UNICODE_FRACTIONS = {
    "¼": 0.25, "½": 0.5, "¾": 0.75, "⅐": 1 / 7, "⅑": 1 / 9, "⅒": 0.1,
    "⅓": 1 / 3, "⅔": 2 / 3, "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

DIET_KEYWORDS = {"vegetarian", "vegan", "gluten-free", "dairy-free", "kosher",
                 "nut-free", "halal", "pescatarian", "low-carb", "low-fat",
                 "whole30", "paleo", "keto", "egg-free", "soy-free",
                 "shellfish-free", "low-sodium", "high-fiber"}

URL_RE = re.compile(r"cooking\.nytimes\.com/recipes/(\d+)-([A-Za-z0-9-]+)")


def slug_from_url(url):
    m = URL_RE.search(url)
    if not m:
        raise ValueError("not an NYT Cooking recipe URL: %s" % url)
    return m.group(2).lower(), int(m.group(1))  # NYT sometimes publishes uppercase slugs; canonical slug stays lowercase


def parse_number_prefix(text):
    """Parse a leading quantity: '1 ½', '1/2', '2', '1-2', '1 to 2'."""
    text = text.strip()
    m = re.match(r"^(\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\s*(?:-|–|—|\bto\b)\s*"
                 r"(\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\b", text)
    if m:
        lo = _num(m.group(1))
        hi = _num(m.group(2))
        if lo is not None and hi is not None:
            return lo, hi, text[m.end():].strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s+(\d+)\s*/\s*(\d+)\b", text)  # 1 1/2
    if m:
        val = int(m.group(1)) + int(m.group(2)) / int(m.group(3))
        return val, None, text[m.end():].strip()
    m = re.match(r"^(\d+)\s*/\s*(\d+)\b", text)
    if m:
        return int(m.group(1)) / int(m.group(2)), None, text[m.end():].strip()
    m = re.match(r"^(\d+)\s*([¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])", text)  # 1½ or 1 ½
    if m:
        return int(m.group(1)) + UNICODE_FRACTIONS[m.group(2)], None, text[m.end():].strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\b", text)
    if m:
        return float(m.group(1)), None, text[m.end():].strip()
    if text and text[0] in UNICODE_FRACTIONS:
        return UNICODE_FRACTIONS[text[0]], None, text[1:].strip()
    return None, None, text


def _num(s):
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def parse_ingredient(position, raw):
    raw = raw.strip()
    rest = raw
    preparation = None
    if "," in rest:
        rest, preparation = rest.split(",", 1)
        preparation = preparation.strip() or None
    optional = bool(re.search(r"\boptional\b", raw, re.I))
    if re.search(r"\b(to taste|as needed|as desired)\b", rest, re.I):
        quantity, quantity_max = None, None
        unit = None
        item = re.sub(r"\b(to taste|as needed|as desired)\b", "", rest,
                      flags=re.I).strip(" ,.")
    else:
        quantity, quantity_max, rest = parse_number_prefix(rest)
        words = rest.strip().split(None, 1)
        unit = None
        if words and words[0].lower().rstrip(".") in UNITS:
            unit = UNITS[words[0].lower().rstrip(".")]
            rest = words[1] if len(words) > 1 else ""
        item = rest.strip()
    item = re.sub(r"^(a|an|the)\s+", "", item.lower()).strip(" .")
    if not item:
        item = raw.lower().strip(" .")
    rec = {"position": position, "raw": raw, "group": None,
           "quantity": quantity, "unit": unit, "item": item,
           "preparation": preparation, "optional": optional}
    if quantity_max is not None:
        rec["quantity_max"] = quantity_max
    return rec


def parse_servings(text):
    text = (text or "").strip()
    out = {"text": text, "min": None, "max": None, "yield_type": "servings"}
    low = text.lower()
    if re.search(r"\b(cups?|liters?|milliliters?|pints?|quarts?|gallons?)\b", low):
        out["yield_type"] = "volume"
    elif re.search(r"\b(pieces?|cookies?|bars?|pancakes?|dozen|servings? of)\b", low) \
            and "serving" not in low:
        out["yield_type"] = "pieces"
    elif "serving" not in low and low:
        out["yield_type"] = "other"
    m = re.search(r"(\d+)\s*(?:-|–|—|\bto\b)\s*(\d+)", text)
    if m:
        out["min"], out["max"] = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        if m:
            v = float(m.group(1))
            out["min"] = out["max"] = int(v) if v == int(v) else None
    return out


def iso_duration_minutes(value):
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", value)
    if not m:
        return None
    d, h, mnt = (int(g) if g else 0 for g in m.groups())
    total = d * 1440 + h * 60 + mnt
    return total or None


def extract_jsonld(html):
    """Fallback: pull the schema.org Recipe JSON-LD block straight from HTML."""
    for m in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@graph"):
                candidates.extend(item["@graph"])
            if isinstance(item, dict) and "Recipe" in (
                    item.get("@type") if isinstance(item.get("@type"), list)
                    else [item.get("@type")]):
                return item
    return None


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _names(value):
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for it in items:
        if isinstance(it, dict):
            name = it.get("name")
            if name:
                out.append(str(name))
        elif it:
            out.append(str(it))
    return out


def _nutrient_number(value):
    if value is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(m.group(1)) if m else None


def _scraper_field(scraper, name, default=None):
    """recipe-scrapers per-field fallback; old recipes raise SchemaOrgException."""
    if scraper is None:
        return default
    try:
        value = getattr(scraper, name)()
        return value if value not in (None, "", []) else default
    except Exception:
        return default


def build_record(url, html, scraped_at):
    """Primary: Recipe JSON-LD from the page. Per-field fallback: recipe-scrapers."""
    slug, nyt_id = slug_from_url(url)
    ld = extract_jsonld(html) or {}
    scraper = None
    try:
        from recipe_scrapers import scrape_html
        scraper = scrape_html(html, url)
    except Exception:
        scraper = None

    name = ld.get("name") or _scraper_field(scraper, "title")
    if not name:
        raise ValueError("no recipe name at %s" % url)
    author = ", ".join(_names(ld.get("author"))) or (
        _scraper_field(scraper, "author", "") or "")

    raw_ingredients = ld.get("recipeIngredient") or ld.get("ingredients")
    if not raw_ingredients:
        raw_ingredients = _scraper_field(scraper, "ingredients", [])
    if isinstance(raw_ingredients, str):
        # old recipes sometimes ship one big string instead of a list
        raw_ingredients = [ln for ln in raw_ingredients.splitlines() if ln.strip()] \
            if "\n" in raw_ingredients else [raw_ingredients]
    ingredients = [parse_ingredient(i + 1, str(raw))
                   for i, raw in enumerate(raw_ingredients) if str(raw).strip()]
    if not ingredients:
        raise ValueError("no ingredients parsed at %s" % url)

    instructions = ld.get("recipeInstructions") or []
    step_texts = []
    for step in instructions:
        if isinstance(step, dict):
            if step.get("@type") == "HowToSection":
                for sub in step.get("itemListElement") or []:
                    if isinstance(sub, dict) and sub.get("text"):
                        step_texts.append(str(sub["text"]).strip())
            elif step.get("text"):
                step_texts.append(str(step["text"]).strip())
        elif str(step).strip():
            step_texts.append(str(step).strip())
    if not step_texts:
        step_texts = [t.strip() for t in
                      (_scraper_field(scraper, "instructions_list", []) or [])
                      if str(t).strip()]
    steps = [{"position": i + 1, "text": t}
             for i, t in enumerate(step_texts) if t]

    raw_time = {}
    for key, label in (("prepTime", "prep"), ("cookTime", "cook"),
                       ("totalTime", "total")):
        if ld.get(key):
            raw_time[label] = str(ld[key])
    prep = iso_duration_minutes(ld.get("prepTime"))
    cook = iso_duration_minutes(ld.get("cookTime"))
    total = iso_duration_minutes(ld.get("totalTime"))
    if prep is None:
        prep = _scraper_field(scraper, "prep_time")
    if cook is None:
        cook = _scraper_field(scraper, "cook_time")
    if total is None:
        total = _scraper_field(scraper, "total_time")
    time_obj = {
        "prep_minutes": int(prep) if prep is not None else None,
        "cook_minutes": int(cook) if cook is not None else None,
        "total_minutes": int(total) if total is not None else None,
        "raw": raw_time,
    }

    nutrition = None
    ld_nutrition = ld.get("nutrition")
    if not isinstance(ld_nutrition, dict) or not ld_nutrition:
        ld_nutrition = _scraper_field(scraper, "nutrients")
    if isinstance(ld_nutrition, dict) and ld_nutrition:
        nutrition = {
            "source": "nyt", "basis": "per_serving",
            "calories": _nutrient_number(ld_nutrition.get("calories")),
            "protein_g": _nutrient_number(ld_nutrition.get("proteinContent")),
            "carbohydrates_g": _nutrient_number(ld_nutrition.get("carbohydrateContent")),
            "fat_g": _nutrient_number(ld_nutrition.get("fatContent")),
            "saturated_fat_g": _nutrient_number(ld_nutrition.get("saturatedFatContent")),
            "unsaturated_fat_g": _nutrient_number(ld_nutrition.get("unsaturatedFatContent")),
            "trans_fat_g": _nutrient_number(ld_nutrition.get("transFatContent")),
            "cholesterol_mg": _nutrient_number(ld_nutrition.get("cholesterolContent")),
            "sodium_mg": _nutrient_number(ld_nutrition.get("sodiumContent")),
            "fiber_g": _nutrient_number(ld_nutrition.get("fiberContent")),
            "sugar_g": _nutrient_number(ld_nutrition.get("sugarContent")),
            "computed_with": None,
        }

    tags = {c: [] for c in ("cuisine", "diet", "course", "meal", "occasion", "other")}
    cuisines = _names(ld.get("recipeCuisine"))
    if not cuisines:
        c = _scraper_field(scraper, "cuisine")
        cuisines = [c] if c else []
    for c in cuisines:
        tags["cuisine"].append(c.lower().replace(" ", "-"))
    categories = _names(ld.get("recipeCategory"))
    if not categories:
        c = _scraper_field(scraper, "category")
        categories = [c] if c else []
    for c in categories:
        tags["course"].append(c.lower().replace(" ", "-"))
    keywords = ld.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",")]
    for kw in keywords:
        slug_kw = str(kw).lower().strip().replace(" ", "-")
        if not slug_kw:
            continue
        if slug_kw in DIET_KEYWORDS:
            tags["diet"].append(slug_kw)
        elif slug_kw not in tags["cuisine"] and slug_kw not in tags["course"]:
            tags["other"].append(slug_kw)
    for c in tags:
        tags[c] = sorted(set(tags[c]))

    images = []
    ld_images = ld.get("image")
    if isinstance(ld_images, dict):
        ld_images = [ld_images]
    if isinstance(ld_images, str):
        ld_images = [ld_images]
    if not ld_images:
        img = _scraper_field(scraper, "image")
        ld_images = [img] if img else []
    for i, img in enumerate(ld_images or []):
        img_url = img.get("url") if isinstance(img, dict) else img
        if img_url:
            images.append({"url": str(img_url),
                           "alt": (img.get("caption") if isinstance(img, dict) else "") or "",
                           "primary": i == 0})

    rating = None
    agg = ld.get("aggregateRating")
    if isinstance(agg, dict) and agg.get("ratingValue") is not None:
        rating = {"average": float(agg["ratingValue"]),
                  "count": int(agg.get("ratingCount") or agg.get("reviewCount") or 0)}
    elif _scraper_field(scraper, "ratings") is not None:
        rating = {"average": float(_scraper_field(scraper, "ratings")),
                  "count": int(_scraper_field(scraper, "ratings_count", 0) or 0)}

    published = None
    if ld.get("datePublished"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(ld["datePublished"]))
        if m:
            published = m.group(1)

    video_url = None
    video = _first(ld.get("video"))
    if isinstance(video, dict):
        video_url = video.get("contentUrl") or video.get("embedUrl")

    servings_text = str(_first(ld.get("recipeYield")) or "")
    if not servings_text:
        servings_text = _scraper_field(scraper, "yields", "") or ""

    return {
        "slug": slug,
        "nyt_id": nyt_id,
        "url": "https://cooking.nytimes.com/recipes/%d-%s" % (nyt_id, slug),
        "name": str(name).strip(),
        "author": author,
        "description": str(ld.get("description") or "").strip(),
        "servings": parse_servings(servings_text),
        "time": time_obj,
        "ingredients": ingredients,
        "steps": steps,
        "nutrition": nutrition,
        "tags": tags,
        "images": images,
        "rating": rating,
        "video_url": video_url,
        "comment_count": None,
        "published_at": published,
        "scraped_at": scraped_at,
        "scraper_version": SCRAPER_VERSION,
    }


def cache_path(cache_dir, url):
    return os.path.join(cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".html")


def fetch(url, cache_dir, delay):
    path = cache_path(cache_dir, url)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read(), True
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":  # server sometimes gzips despite identity
        import gzip
        raw = gzip.decompress(raw)
    html = raw.decode("utf-8", errors="replace")
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    time.sleep(delay)
    return html, False


def write_shards(records_by_id, data_dir):
    """Full-record replace keyed on nyt_id; lines sorted by (slug, nyt_id)
    within shard so duplicate slugs stay adjacent and both survive."""
    os.makedirs(data_dir, exist_ok=True)
    by_shard = {}
    for nyt_id, record in records_by_id.items():
        by_shard.setdefault(record["slug"][:2], {})[nyt_id] = record
    for shard_key, new_records in by_shard.items():
        path = os.path.join(data_dir, shard_key + ".jsonl")
        existing = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        existing[r["nyt_id"]] = r
        existing.update(new_records)
        ordered = sorted(existing.values(), key=lambda r: (r["slug"], r["nyt_id"]))
        with open(path, "w", encoding="utf-8") as fh:
            for r in ordered:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_checkpoint(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"done": [], "failed": {}}


def save_checkpoint(path, state):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--urls", required=True, help="file with one recipe URL per line")
    ap.add_argument("--data-dir", default="data/shards")
    ap.add_argument("--cache-dir", default=".cache/html")
    ap.add_argument("--checkpoint", default=".cache/checkpoint.json")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between network fetches (default 2.0)")
    ap.add_argument("--limit", type=int, default=0, help="max URLs this run (0 = all)")
    ap.add_argument("--batch", type=int, default=25,
                    help="rewrite shards every N records")
    ap.add_argument("--reparse-only", action="store_true",
                    help="never fetch; only (re)parse cached HTML")
    args = ap.parse_args(argv)

    with open(args.urls, encoding="utf-8") as fh:
        urls = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    state = load_checkpoint(args.checkpoint)
    done = set(state["done"])
    pending = [u for u in urls if u not in done]
    if args.limit:
        pending = pending[:args.limit]
    print("scrape: %d urls total, %d done, %d pending this run"
          % (len(urls), len(done), len(pending)), file=sys.stderr)

    batch = {}
    written = 0
    for i, url in enumerate(pending, 1):
        scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            if args.reparse_only and not os.path.exists(cache_path(args.cache_dir, url)):
                raise ValueError("no cached HTML (reparse-only)")
            html, from_cache = fetch(url, args.cache_dir,
                                     0 if args.reparse_only else args.delay)
            record = build_record(url, html, scraped_at)
            batch[record["nyt_id"]] = record
            done.add(url)
            state["failed"].pop(url, None)
            status = "cache" if from_cache else "fetch"
        except (ValueError, urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError) as e:
            state["failed"][url] = "%s: %s" % (type(e).__name__, e)
            done.add(url)  # don't retry deterministically-failing URLs forever
            print("scrape: FAIL %s (%s)" % (url, e), file=sys.stderr)
            continue
        if len(batch) >= args.batch:
            write_shards(batch, args.data_dir)
            written += len(batch)
            batch = {}
            state["done"] = sorted(done)
            save_checkpoint(args.checkpoint, state)
            print("scrape: %d/%d processed, %d written"
                  % (i, len(pending), written), file=sys.stderr)
    if batch:
        write_shards(batch, args.data_dir)
        written += len(batch)
    state["done"] = sorted(done)
    save_checkpoint(args.checkpoint, state)
    print("scrape: finished. %d record(s) written, %d failure(s)"
          % (written, len(state["failed"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
