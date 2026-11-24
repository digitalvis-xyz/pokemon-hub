"""Agenda: curated releases/events + your own additions + automatic import from web pages.

Import reads schema.org Event data (JSON-LD) that many event and calendar sites embed in their pages.
Your additions and imports live in data/agenda_user.json (shared by all profiles).
"""
from __future__ import annotations

import json
import re
import time
from html import unescape

from common import Http, data_path, load_json, norm, save_json

USER_PATH = data_path("agenda_user.json")
DEFAULT_SOURCES = [
    {"url": "https://stadiumcardshow.com/", "kind": "show"},
    {"url": "https://belemcardshow.com/", "kind": "show"},
    {"url": "https://coimbracardshow.pt/pt", "kind": "show"},
    {"url": "https://lorenzone.fr/blogs/calendrier/pokemon-tcg", "kind": "release"},
]
SHOW_WORDS = ("card show", "cardshow", "tcg show", "feira", "salon", "convention", "trading card", "cartas", "cartes", "collect")
PT_WORDS = ("portugal", "lisboa", "lisbon", "porto", "coimbra", "braga", "faro", "setubal", "aveiro", "funchal", "almada", "oeiras", "sintra", "leiria", "evora")


def user_data() -> dict:
    d = load_json(USER_PATH, {})
    d.setdefault("events", [])
    d.setdefault("releases", [])
    d.setdefault("sources", list(DEFAULT_SOURCES))
    d.setdefault("hidden", [])
    return d


def save_user(d: dict) -> None:
    save_json(USER_PATH, d)


def _key(item: dict) -> str:
    return "%s|%s" % (item.get("date", ""), norm(item.get("title", ""))[:40])


def add(kind: str, item: dict, origin: str = "manuel") -> dict:
    d = user_data()
    item = {k: v for k, v in item.items() if v not in (None, "")}
    item["origin"] = origin
    item["id"] = "u%d" % int(time.time() * 1000)
    lst = d["events" if kind == "show" else "releases"]
    if any(_key(x) == _key(item) for x in lst):
        return item
    lst.append(item)
    save_user(d)
    return item


def remove(item_id: str) -> None:
    d = user_data()
    for k in ("events", "releases"):
        d[k] = [x for x in d[k] if x.get("id") != item_id]
    save_user(d)


# ---------- import from web pages ----------

def _jsonld_blocks(html: str):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(unescape(block.strip()))
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                if "@graph" in obj:
                    stack.extend(obj["@graph"])
                yield obj
                for v in obj.values():
                    if isinstance(v, (list, dict)):
                        stack.append(v)


def _addr(loc) -> tuple[str, str]:
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if isinstance(loc, str):
        return loc, ""
    if not isinstance(loc, dict):
        return "", ""
    a = loc.get("address")
    venue = loc.get("name", "")
    if isinstance(a, dict):
        city = a.get("addressLocality", "")
        parts = [a.get("streetAddress"), a.get("postalCode"), city, a.get("addressCountry") if isinstance(a.get("addressCountry"), str) else ""]
        return ", ".join(p for p in parts if p), city
    if isinstance(a, str):
        return a, ""
    return venue, ""


def extract_events(html: str, page_url: str) -> list[dict]:
    out = []
    og = re.search(r'property="og:image"\s+content="([^"]+)"', html)
    for obj in _jsonld_blocks(html):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if not any(isinstance(x, str) and x.endswith("Event") for x in types):
            continue
        start = str(obj.get("startDate", ""))[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}", start):
            continue
        end = str(obj.get("endDate", ""))[:10]
        address, city = _addr(obj.get("location"))
        img = obj.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url", "")
        out.append({
            "title": unescape(str(obj.get("name", "")).strip()), "date": start,
            "date_end": end if end and end != start else None,
            "address": address, "city": city, "venue": (obj.get("location") or {}).get("name", "") if isinstance(obj.get("location"), dict) else "",
            "details": unescape(str(obj.get("description", "")))[:200],
            "url": obj.get("url") or page_url, "image": img or (og.group(1) if og else ""),
        })
    return out


def refresh_sources(log=print) -> dict:
    """Read every source page, keep new Portugal card shows and Pokémon releases."""
    d = user_data()
    http = Http()
    added = {"show": 0, "release": 0}
    report = []
    today = time.strftime("%Y-%m-%d")
    for src in d["sources"]:
        url, kind = src["url"], src.get("kind", "show")
        try:
            status, html = http.get(url)
        except Exception as e:
            report.append({"url": url, "status": "erreur", "found": 0, "detail": str(e)[:120]})
            log("  %s : erreur %s" % (url, str(e)[:80]))
            continue
        evs = extract_events(html, url) if status == 200 else []
        kept = 0
        for e in evs:
            if (e.get("date_end") or e["date"]) < today:
                continue
            text = norm(" ".join(str(e.get(k, "")) for k in ("title", "address", "city", "details", "url")))
            if kind == "show" and not (any(w in text for w in PT_WORDS) and any(w in text for w in SHOW_WORDS)):
                continue
            if kind == "release" and "pokemon" not in text:
                continue
            before = len(user_data()["events" if kind == "show" else "releases"])
            item = add(kind, dict(e, region=src.get("region", "FR") if kind == "release" else None,
                                  status="importé", kind="extension" if kind == "release" else None),
                       origin="import")
            if len(user_data()["events" if kind == "show" else "releases"]) > before:
                kept += 1
                added[kind] += 1
        report.append({"url": url, "status": status, "found": len(evs), "added": kept})
        log("  %s : HTTP %s · %d événement(s) lus · %d ajouté(s)" % (url, status, len(evs), kept))
        time.sleep(1)
    d = user_data()
    d["last_refresh"] = {"at": time.time(), "report": report}
    save_user(d)
    return {"added": added, "report": report}
