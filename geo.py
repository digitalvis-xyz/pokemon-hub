"""Free geocoding with OpenStreetMap Nominatim (no key). Results cached forever in state/cache/geocode.json.
Nominatim asks for max 1 request/second and a real User-Agent: we respect both."""
from __future__ import annotations

import os
import threading
import time

from common import STATE_DIR, Http, load_json, save_json

CACHE = os.path.join(STATE_DIR, "cache", "geocode.json")
_lock = threading.Lock()
_last = [0.0]
_http = Http(user_agent="TCGHub/0.3 (personal Pokemon TCG collector app)")


def geocode(query: str) -> dict | None:
    """'Rua X, Lisboa' -> {"lat", "lng", "label"} or None."""
    q = (query or "").strip()
    if not q:
        return None
    with _lock:
        cache = load_json(CACHE, {})
        if q in cache:
            return cache[q]
        wait = 1.1 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            res = _http.get_json("https://nominatim.openstreetmap.org/search",
                                 {"format": "json", "q": q, "limit": 1, "addressdetails": 0})
        except Exception:
            return None  # not cached: retried next time
        hit = {"lat": float(res[0]["lat"]), "lng": float(res[0]["lon"]), "label": res[0].get("display_name", q)} if res else None
        cache[q] = hit
        save_json(CACHE, cache)
        return hit


def shop_query(shop: dict) -> str:
    """Best text to geocode a shop: full address, else city + country."""
    if shop.get("address"):
        return shop["address"]
    city = (shop.get("city") or "").split("·")[0].strip()
    if city and city.lower() not in ("online", "nacional", "en ligne"):
        return city + ", Portugal"
    return ""


# ---------- shop lookup: type a name, a website or an address ----------
import json as _json
import re as _re
from html import unescape as _unescape
from urllib.parse import urlparse as _urlparse

from common import post_json as _post_json


def _city_from(addr: dict) -> str:
    return addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""


def _nominatim(query: str, limit: int = 5) -> list[dict]:
    wait = 1.1 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    try:
        res = _http.get_json("https://nominatim.openstreetmap.org/search",
                             {"format": "json", "q": query, "limit": limit, "addressdetails": 1, "extratags": 1,
                              "namedetails": 1, "countrycodes": "pt,es,fr"})
    except Exception:
        return []
    out = []
    for r in res:
        tags = r.get("extratags") or {}
        name = (r.get("namedetails") or {}).get("name") or r.get("name") or r.get("display_name", "").split(",")[0]
        out.append({"name": name, "address": r.get("display_name", ""), "city": _city_from(r.get("address") or {}),
                    "lat": float(r["lat"]), "lng": float(r["lon"]),
                    "website": tags.get("website") or tags.get("contact:website") or "", "source": "OpenStreetMap"})
    return out


def _google(query: str, key: str) -> list[dict]:
    try:
        res = _post_json("https://places.googleapis.com/v1/places:searchText",
                         {"textQuery": query, "languageCode": "pt", "maxResultCount": 5},
                         {"X-Goog-Api-Key": key,
                          "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,places.websiteUri,places.addressComponents"})
    except Exception as e:
        return [{"error": "Google Places : %s" % str(e)[:160]}]
    out = []
    for p in res.get("places", []):
        city = next((c.get("longText") for c in p.get("addressComponents", []) if "locality" in c.get("types", [])), "")
        loc = p.get("location") or {}
        out.append({"name": (p.get("displayName") or {}).get("text", ""), "address": p.get("formattedAddress", ""),
                    "city": city, "lat": loc.get("latitude"), "lng": loc.get("longitude"),
                    "website": p.get("websiteUri", ""), "source": "Google"})
    return out


def _from_website(url: str) -> dict | None:
    """Read a shop's home page: name (og:site_name / <title>) and address (schema.org JSON-LD) if published."""
    try:
        status, html = _http.get(url)
    except Exception:
        return None
    if status != 200:
        return None
    m = _re.search(r'property="og:site_name"\s+content="([^"]+)"', html) or _re.search(r"<title[^>]*>([^<]+)</title>", html, _re.I)
    name = _unescape(m.group(1)).split("|")[0].split(" – ")[0].split(" - ")[0].strip() if m else ""
    address = city = ""
    for block in _re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, _re.S | _re.I):
        try:
            data = _json.loads(block.strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else data.get("@graph", [data])):
            a = obj.get("address") if isinstance(obj, dict) else None
            if isinstance(a, dict) and (a.get("streetAddress") or a.get("addressLocality")):
                city = a.get("addressLocality", "")
                address = ", ".join(x for x in (a.get("streetAddress"), a.get("postalCode"), city, a.get("addressCountry") if isinstance(a.get("addressCountry"), str) else "") if x)
                break
        if address:
            break
    return {"name": name, "website": url, "address": address, "city": city, "source": "site web"}


def lookup(query: str, google_key: str = "") -> list[dict]:
    """Candidates for a shop from any one clue: website, name or address."""
    q = (query or "").strip()
    if not q:
        return []
    is_url = bool(_re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(:\d+)?(/.*)?$", q)) and " " not in q
    results = []
    if is_url:
        url = q if q.startswith("http") else "https://" + q
        url = "%s://%s" % (_urlparse(url).scheme, _urlparse(url).netloc)
        site = _from_website(url) or {"name": "", "website": url, "address": "", "city": "", "source": "site web"}
        if not site["name"]:
            site["name"] = _urlparse(url).netloc.replace("www.", "").split(".")[0].replace("-", " ").title()
        if site["address"]:
            g = geocode(site["address"])
            if g:
                site.update(lat=g["lat"], lng=g["lng"])
        results.append(site)
        q = site["name"]  # also look the shop up by name to find its physical location
    found = _google(q, google_key) if google_key else _nominatim(q)
    for f in found:
        if is_url and not f.get("website"):
            f["website"] = results[0]["website"]
    return results + found
