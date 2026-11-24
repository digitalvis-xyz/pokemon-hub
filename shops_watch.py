#!/usr/bin/env python3
"""Prototype 2 - watch online shops for new Pokémon products, restocks, pre-orders and price changes.

Works without scraping HTML for the two most common shop engines:
  - Shopify      -> public  /products.json
  - WooCommerce  -> public  /wp-json/wc/store/v1/products  (Store API)
The engine is detected automatically. Other shops are reported as "unsupported".

Usage:
  python3 shops_watch.py                     # live, shops from shops.json
  python3 shops_watch.py --detect URL        # just tell me if a shop is supported
  python3 shops_watch.py --fixtures --run 1  # offline baseline, then --run 2 to see the diff

First live run = baseline (no alerts). Following runs report only what changed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from common import ROOT, STATE_DIR, Http, ensure_profiles, load_json, norm, pfile, polite_sleep, pstate, save_json, stamp
from notify import telegram
from report import write_csv, write_html

PREORDER_WORDS = ["precommande", "pre-commande", "preorder", "pre-order", "pre-venda", "prevenda",
                  "pre venda", "reserva", "preventa", "pre-venta", "vorbestellung"]
SEALED_WORDS = ["display", "booster box", "etb", "elite trainer", "coffret", "booster", "tin", "pokebox",
                "collection", "blister", "bundle", "caixa", "pack"]


# ---------- engines ----------

def fetch_shopify(http: Http, base: str, max_pages: int = 4) -> list[dict]:
    out = []
    for page in range(1, max_pages + 1):
        data = http.get_json(base + "/products.json", {"limit": 250, "page": page})
        prods = data.get("products", [])
        out.extend(parse_shopify(p, base) for p in prods)
        if len(prods) < 250:
            break
    return out


def parse_shopify(p: dict, base: str) -> dict:
    variants = p.get("variants") or [{}]
    prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return {
        "product_id": str(p.get("id")),
        "title": p.get("title", ""),
        "url": "%s/products/%s" % (base, p.get("handle", "")),
        "price": min(prices) if prices else None,
        "available": any(v.get("available") for v in variants),
        "image": ((p.get("images") or [{}])[0] or {}).get("src", ""),
        "meta": " ".join([p.get("vendor", ""), p.get("product_type", "")] + tags),
        "created_at": p.get("created_at", ""),
    }


def fetch_woo(http: Http, base: str, max_pages: int = 5) -> list[dict]:
    out = []
    for page in range(1, max_pages + 1):
        prods = http.get_json(base + "/wp-json/wc/store/v1/products", {"per_page": 100, "page": page})
        out.extend(parse_woo(p) for p in prods)
        if len(prods) < 100:
            break
    return out


def parse_woo(p: dict) -> dict:
    pr = p.get("prices") or {}
    try:
        price = int(pr.get("price")) / (10 ** int(pr.get("currency_minor_unit", 2)))
    except (TypeError, ValueError):
        price = None
    return {
        "product_id": str(p.get("id")),
        "title": re.sub(r"<[^>]+>", "", p.get("name", "")),
        "url": p.get("permalink", ""),
        "price": price,
        "available": bool(p.get("is_in_stock")),
        "image": ((p.get("images") or [{}])[0] or {}).get("src", ""),
        "meta": " ".join(c.get("name", "") for c in p.get("categories", []))
                + " " + ((p.get("stock_availability") or {}).get("text") or ""),
        "created_at": "",
    }


SUPPORTED = ("shopify", "woocommerce")


def detect_verbose(http: Http, base: str) -> tuple[str, list[dict]]:
    """Try each known engine; return (engine, notes) where notes explain every probe."""
    notes = []
    for engine, path in (("shopify", "/products.json?limit=1"),
                         ("woocommerce", "/wp-json/wc/store/v1/products?per_page=1")):
        try:
            status, body = http.get(base + path)
        except Exception as e:
            notes.append({"probe": engine, "status": "erreur", "detail": str(e)[:200]})
            continue
        is_json = body.lstrip()[:1] in ("{", "[")
        notes.append({"probe": engine, "status": status,
                      "detail": "JSON OK" if (status == 200 and is_json) else re.sub(r"\s+", " ", body[:160])})
        if status == 200 and is_json:
            return engine, notes
    if all(n["status"] == "erreur" for n in notes):
        return "error", notes  # network / certificate problem, not the shop's fault
    blocked = any(n["status"] in (403, 429, 503) for n in notes)
    return ("blocked" if blocked else "unsupported"), notes


def detect(http: Http, base: str) -> str:
    return detect_verbose(http, base)[0]


# ---------- logic ----------

def is_pokemon(p: dict, keywords: list[str]) -> bool:
    text = norm(p["title"] + " " + p.get("meta", ""))
    return any(norm(k) in text for k in keywords)


def classify_product(p: dict) -> dict:
    text = norm(p["title"] + " " + p.get("meta", ""))
    p["preorder"] = any(w in text for w in PREORDER_WORDS)
    p["sealed"] = any(re.search(r"\b%s\b" % re.escape(w), text) for w in SEALED_WORDS)
    return p


def diff(shop: str, products: list[dict], previous: dict) -> list[dict]:
    events = []
    for p in products:
        old = previous.get(p["product_id"])
        ev = None
        if old is None:
            ev = "NEW_PREORDER" if p["preorder"] else "NEW"
        elif not old.get("available") and p["available"]:
            ev = "RESTOCK"
        elif old.get("available") and not p["available"]:
            ev = "SOLD_OUT"
        elif old.get("price") and p["price"] and p["price"] < old["price"] - 0.009:
            ev = "PRICE_DROP"
        elif old.get("price") and p["price"] and p["price"] > old["price"] + 0.009:
            ev = "PRICE_UP"
        if ev:
            e = dict(p, shop=shop, event=ev, old_price=(old or {}).get("price"))
            e["verdict"] = "match" if ev in ("NEW", "NEW_PREORDER", "RESTOCK", "PRICE_DROP") else "review"
            events.append(e)
    return events


EVENT_ORDER = {"NEW_PREORDER": 0, "RESTOCK": 1, "NEW": 2, "PRICE_DROP": 3, "PRICE_UP": 4, "SOLD_OUT": 5}
ALERT_EVENTS = ("NEW_PREORDER", "RESTOCK", "NEW", "PRICE_DROP")


def watch(cfg: dict, fixtures: bool = False, run_no: int = 1, delay: float = 2.0,
          log=print, send_alerts: bool = True, stop=None) -> dict:
    """Fetch every shop, diff against saved state. Returns {"events", "inventory", "shops"}."""
    http = Http()
    keywords = cfg.get("keywords", ["pokemon"])
    state_path = os.path.join(STATE_DIR, "shops_fixture_state.json") if fixtures else pstate("shops_state.json")
    state = load_json(state_path, {})
    all_events, inventory, summary = [], [], []

    for shop in cfg.get("shops", []):
        if stop and stop():
            log("Arrêt demandé.")
            break
        if not shop.get("url"):
            continue  # map-only shop (physical store, no website to track)
        name, base = shop["name"], shop["url"].rstrip("/")
        try:
            if fixtures:
                data = load_json(os.path.join(ROOT, "fixtures", "%s_run%d.json" % (shop["fixture"], run_no)), {})
                engine = shop["engine"]
                raw = data["products"]
                products = [parse_shopify(p, base) for p in raw] if engine == "shopify" else [parse_woo(p) for p in raw]
            else:
                engine = shop.get("engine") if shop.get("engine") in SUPPORTED else detect(http, base)
                if engine == "shopify":
                    products = fetch_shopify(http, base)
                elif engine == "woocommerce":
                    products = fetch_woo(http, base)
                else:
                    why = {"blocked": "bloqué par la protection anti-robots du site",
                           "error": "site injoignable (réseau ou certificat), voir le Diagnostic"}.get(
                        engine, "moteur non pris en charge (ni Shopify ni WooCommerce)")
                    log("  %-22s %s" % (name, why))
                    summary.append({"name": name, "engine": engine, "status": why, "count": 0})
                    continue
                polite_sleep(delay)
        except Exception as e:
            log("  %-22s error: %s" % (name, str(e)[:120]))
            summary.append({"name": name, "engine": shop.get("engine", "?"), "status": "error: %s" % str(e)[:80], "count": 0})
            continue

        products = [classify_product(p) for p in products if is_pokemon(p, keywords)]
        prev = state.get(name, {})
        first_run = not prev
        events = [] if first_run else diff(name, products, prev)
        state[name] = {p["product_id"]: {"price": p["price"], "available": p["available"], "title": p["title"]} for p in products}
        inventory.extend(dict(p, shop=name) for p in products)
        all_events.extend(events)
        status = "baseline saved" if first_run else "%d changes" % len(events)
        summary.append({"name": name, "engine": engine, "status": status, "count": len(products)})
        log("  %-22s %-11s %4d Pokémon products · %s" % (name, engine, len(products), status))

    save_json(state_path, state)
    all_events.sort(key=lambda e: EVENT_ORDER.get(e["event"], 9))
    alert = [e for e in all_events if e["event"] in ALERT_EVENTS]
    if alert and send_alerts and not fixtures:
        msg = "\n\n".join("%s · %s\n%s\n%s" % (e["event"], e["shop"], e["title"], e["url"]) for e in alert[:15])
        if telegram("Boutiques Pokémon\n\n" + msg):
            log("Telegram alert sent.")
    return {"events": all_events, "inventory": inventory, "shops": summary}


def run(args) -> int:
    if args.detect:
        print(args.detect, "->", detect(Http(), args.detect.rstrip("/")))
        return 0
    ensure_profiles()
    cfg = load_json(os.path.join(ROOT, "fixtures/shops_fixture.json") if args.fixtures else (args.shops or pfile("shops.json")), {})
    res = watch(cfg, fixtures=args.fixtures, run_no=args.run, delay=args.delay)
    events, inventory = res["events"], res["inventory"]
    tag = "shops_" + stamp() + ("_fixture%d" % args.run if args.fixtures else "")
    cols = ["event", "shop", "title", "price", "old_price", "available", "preorder", "sealed", "url"]
    if events:
        print("\nChanges:")
        for e in events:
            price = "%.2f €" % e["price"] if e["price"] is not None else "?"
            old = " (was %.2f €)" % e["old_price"] if e.get("old_price") and e["event"].startswith("PRICE") else ""
            print("  %-12s %-18s %-48s %s%s" % (e["event"], e["shop"][:18], e["title"][:48], price, old))
        print("\nCSV : " + write_csv(events, tag + "_changes", cols))
        print("HTML: " + write_html(events, tag + "_changes", "Shop changes · " + tag, cols[:-1],
                                    image_key="image", link_key="url"))
    else:
        print("\nNo changes.")
    inv_cols = ["shop", "title", "price", "available", "preorder", "sealed", "url"]
    print("Full inventory CSV: " + write_csv(inventory, tag + "_inventory", inv_cols))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shops", default="", help="default: shops of the active profile")
    ap.add_argument("--detect", metavar="URL", help="only detect the shop engine")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--run", type=int, default=1, help="fixture run number (1 = baseline, 2 = changes)")
    sys.exit(run(ap.parse_args()))
