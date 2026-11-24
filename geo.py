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
