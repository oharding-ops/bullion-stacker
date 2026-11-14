#!/usr/bin/env python3
"""Keep the Bullion Stacker hosted Costco watch list fresh.

Usage (from ~/workspace/stacktracker):
    python3 tools/update-costco-list.py [--list costco-watch-list.json] [--dry-run]

What it does:
  1. Fetches Costco's product sitemaps (sitemap_lw_p_001.xml and
     sitemap_lw_p_mod_001.xml). These are plain XML files that load with
     a normal HTTP GET — no rate-limiting seen so far.
  2. Extracts bullion product URLs (gold-bar | silver-bar | gold-coin |
     silver-coin | bullion | silver-round | gold-round) while excluding
     jewelry noise (14kt | 18kt | 22kt | 24kt | diamond | bracelet |
     hoop | earring | pendant | necklace).
  3. Diffs the product IDs against the current costco-watch-list.json.
  4. For each NEW candidate, fetches its product page and parses the
     schema.org ProductSchema JSON-LD block. Only candidates with a
     valid ProductSchema block AND a sku (the Costco item number) are
     kept — the app cross-checks the sku on every check, so a product
     without a verified sku can never be watched.
  5. Appends verified products to the JSON (existing entries are
     preserved in order; baked-in items are never removed). If nothing
     new verified, the file is left untouched (idempotent).

RATE-LIMIT CAUTION: Costco rate-limits product-page fetches hard
(HTTP 429, even after a pause — this VM was blocked for hours after a
burst on Nov 14, 2026). Page fetches here use a 12-second gap and a
single attempt each. If a candidate 429s, it is SKIPPED with reason
"rate-limited" and waits for the next run — never force it, never
retry in a tight loop. The sitemap itself does not 429, so discovery
always works; verification is what throttles.

Do NOT point this at the search API or the batched product API:
both are proven unusable (search 429s even with session cookies;
product API 401s on every credential variant tried Nov 14, 2026).
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

SITEMAPS = [
    "https://www.costco.com/sitemap_lw_p_001.xml",
    "https://www.costco.com/sitemap_lw_p_mod_001.xml",
]
LIST_PATH_DEFAULT = "costco-watch-list.json"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SM-F976U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
# Gap between product-page fetches. Costco 429s bursts; 12s is the
# slowest the app-side checker can afford and still the floor here.
# See the rate-limit caution above — never lower this to chase speed.
FETCH_GAP_S = 12

BULLION_RE = re.compile(
    r"gold-bar|silver-bar|gold-coin|silver-coin|bullion|silver-round|gold-round",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"14kt|18kt|22kt|24kt|diamond|bracelet|hoop|earring|pendant|necklace",
    re.IGNORECASE,
)
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
PRODUCT_ID_RE = re.compile(r"/(\d+)(?:\.html)?$|\.product\.(\d+)\.html$")
SCHEMA_RE = re.compile(
    r'<script[^>]*id="ProductSchema"[^>]*>(.*?)</script>', re.DOTALL
)


def fetch_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read().decode("utf-8", "replace")


def sitemap_product_urls():
    urls = {}
    for sm in SITEMAPS:
        try:
            _, xml = fetch_text(sm)
        except Exception as e:
            print(f"sitemap {sm}: FAILED ({e}) — continuing with the other")
            continue
        for loc in LOC_RE.findall(xml):
            if BULLION_RE.search(loc) and not NOISE_RE.search(loc):
                m = PRODUCT_ID_RE.search(loc)
                if not m:
                    continue
                pid = m.group(1) or m.group(2)
                urls.setdefault(pid, loc)
    return urls


def parse_product_schema(html):
    """Return (sku, name, availability) from the ProductSchema block,
    or (None, None, None) when the block is missing/unparseable."""
    m = SCHEMA_RE.search(html)
    if not m:
        return None, None, None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None, None, None
    items = data if isinstance(data, list) else [data]
    for it in items:
        if not isinstance(it, dict) or it.get("@type") != "Product":
            continue
        sku = it.get("sku")
        name = it.get("name")
        offers = it.get("offers") or {}
        avail = offers.get("availability", "")
        if isinstance(avail, str):
            avail = avail.rsplit("/", 1)[-1]
        if sku:
            return str(sku).strip(), (
                name.strip() if isinstance(name, str) else None
            ), (avail or None)
    return None, None, None


def slug_to_name(url):
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.product\.\d+\.html$", "", slug)
    slug = re.sub(r"[-_]+", " ", slug)
    slug = slug.replace("%2c", ",").replace("%26apos%3b", "'")
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug[:120].title() if slug else None


def verify_candidate(pid, sitemap_url):
    """Fetch the product page, follow redirects to the canonical
    /p/-/ URL, and read the ProductSchema block. Returns
    (entry_dict, reason) — entry_dict is None when not verifiable."""
    try:
        final_url, html = fetch_text(sitemap_url, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "rate-limited (429)"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"fetch failed ({e})"
    if not final_url.startswith("https://www.costco.com/p/-/"):
        return None, f"unexpected final URL {final_url[:80]}"
    sku, name, avail = parse_product_schema(html)
    if not sku:
        return None, "no ProductSchema/sku on page"
    low = (final_url + " " + (name or "")).lower()
    if "gold" in low:
        metal = "gold"
    elif "silver" in low:
        metal = "silver"
    else:
        return None, "metal unclear (not gold/silver)"
    entry = {
        "item": sku,
        "name": name or slug_to_name(final_url) or f"Costco item {sku}",
        "metal": metal,
        "url": final_url,
        "productId": pid,
    }
    return entry, f"verified (sku {sku}, {avail or 'availability unknown'})"


def main():
    list_path = LIST_PATH_DEFAULT
    dry_run = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--list" and i + 1 < len(args):
            list_path = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"unknown arg: {args[i]}")
            return 2

    with open(list_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    products = data.get("products", [])
    known_ids = {str(p.get("productId")) for p in products}
    known_items = {str(p.get("item")) for p in products}
    print(f"current list: {len(products)} products")

    found = sitemap_product_urls()
    print(f"sitemap bullion URLs: {len(found)}")
    new = {pid: u for pid, u in found.items() if pid not in known_ids}
    print(f"new candidates (not in list): {len(new)}")

    added, skipped = [], []
    for pid, url in sorted(new.items()):
        entry, reason = verify_candidate(pid, url)
        if entry is None:
            skipped.append((pid, url, reason))
            print(f"  SKIP {pid}: {reason}")
        elif entry["item"] in known_items:
            skipped.append((pid, url, f"item {entry['item']} already listed"))
            print(f"  SKIP {pid}: item {entry['item']} already listed")
        else:
            added.append(entry)
            known_items.add(entry["item"])
            print(f"  ADD  {pid}: {entry['name']} "
                  f"(item {entry['item']}, {entry['metal']}) — {reason}")
        time.sleep(FETCH_GAP_S)

    print(f"\nsummary: added {len(added)}, skipped {len(skipped)}")
    if not added:
        print("no changes — list file untouched")
        return 0
    products.extend(added)
    data["products"] = products
    data["updated"] = date.today().isoformat()
    if dry_run:
        print("dry run — not writing")
        return 0
    with open(list_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"wrote {list_path}: now {len(products)} products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
