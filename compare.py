"""Price comparator: search one product in every tracked shop at once.

Shopify   : /search/suggest.json (predictive search, public), falling back to /products.json
WooCommerce: /wp-json/wc/store/v1/products?search=
Searches run in parallel; results are sorted in stock first, then by price.
"""
from __future__ import annotations

import concurrent.futures as cf
import re

from common import Http, norm


def _price(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("€", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _match(title: str, q: str) -> bool:
    t, words = norm(title), [w for w in norm(q).split() if len(w) > 1]
    return all(w in t for w in words)


def _shopify(http: Http, shop: dict, q: str) -> list[dict]:
    base = shop["url"].rstrip("/")
    out = []
    try:
        data = http.get_json(base + "/search/suggest.json",
                             {"q": q, "resources[type]": "product", "resources[limit]": 10})
        for p in ((data.get("resources") or {}).get("results") or {}).get("products", []):
            url = p.get("url", "")
            out.append({"title": p.get("title", ""), "price": _price(p.get("price")),
                        "available": bool(p.get("available")), "url": base + url.split("?")[0] if url.startswith("/") else url,
                        "image": p.get("image") or (p.get("featured_image") or {}).get("url", "")})
    except Exception:
        pass
    if not out:  # predictive search disabled: scan the catalogue
        from shops_watch import fetch_shopify
        for p in fetch_shopify(http, base, max_pages=4):
            if _match(p["title"], q):
                out.append({k: p[k] for k in ("title", "price", "available", "url", "image")})
    return [o for o in out if _match(o["title"], q)] or out[:0]


def _woo(http: Http, shop: dict, q: str) -> list[dict]:
    from shops_watch import parse_woo
    base = shop["url"].rstrip("/")
    prods = http.get_json(base + "/wp-json/wc/store/v1/products", {"search": q, "per_page": 20})
    return [{k: p[k] for k in ("title", "price", "available", "url", "image")} for p in map(parse_woo, prods)]


def compare(shops: list[dict], q: str) -> dict:
    tracked = [s for s in shops if s.get("url") and s.get("engine") in ("shopify", "woocommerce")]
    results, errors = [], []

    def one(shop):
        http = Http()
        fn = _shopify if shop["engine"] == "shopify" else _woo
        return shop, fn(http, shop, q)

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one, s) for s in tracked]
        for f in cf.as_completed(futs, timeout=60):
            try:
                shop, items = f.result()
                for it in items:
                    results.append(dict(it, shop=shop["name"], shop_url=shop["url"]))
            except Exception as e:
                errors.append(str(e)[:120])
    results.sort(key=lambda r: (not r["available"], r["price"] if r["price"] is not None else 1e9))
    best = next((r for r in results if r["available"] and r["price"] is not None), None)
    if best:
        best["best"] = True
    return {"query": q, "shops_searched": len(tracked), "results": results[:80], "errors": errors}
