"""In-app notification centre, stored in state/notifications.json."""
from __future__ import annotations

import os
import threading
import time

from common import load_json, pstate, save_json



def _path(pid: str | None = None) -> str:
    return pstate("notifications.json", pid)
MAX_ITEMS = 300
_lock = threading.Lock()


def _load(pid: str | None = None) -> list[dict]:
    return load_json(_path(pid), [])


def add(items: list[dict]) -> int:
    """items: {key, kind, title, body, url, image, price}. Same key never notified twice."""
    with _lock:
        data = _load()
        known = {n["key"] for n in data}
        added = 0
        now = time.time()
        for it in items:
            if it["key"] in known:
                continue
            known.add(it["key"])
            data.insert(0, dict(it, id="%d-%d" % (int(now * 1000), added), at=now, read=False))
            added += 1
        save_json(_path(), data[:MAX_ITEMS])
        return added


def list_all() -> dict:
    data = _load()
    return {"items": data, "unread": sum(1 for n in data if not n.get("read"))}


def mark_read(ids: list[str] | None = None) -> None:
    with _lock:
        data = _load()
        for n in data:
            if ids is None or n["id"] in ids:
                n["read"] = True
        save_json(_path(), data)


def purge_demo(pid: str | None = None) -> None:
    with _lock:
        save_json(_path(pid), [n for n in _load(pid) if not n.get("demo")])


def clear() -> None:
    with _lock:
        save_json(_path(), [])


def from_vinted(rows: list[dict], demo: bool) -> list[dict]:
    return [{
        "key": "vinted:%s%s" % (r["listing_id"], ":demo" if demo else ""),
        "kind": "vinted", "event": "MATCH",
        "title": "%s · %s €" % (r.get("query_card", ""), ("%.0f" % r["price"]) if r.get("price") is not None else "?"),
        "body": r.get("title", ""), "url": r.get("url", ""), "image": r.get("photo_url", ""),
        "demo": demo,
    } for r in rows if r.get("verdict") == "match"]


EVENT_LABEL = {"NEW_PREORDER": "Précommande", "RESTOCK": "Retour en stock", "NEW": "Nouveau produit",
               "PRICE_DROP": "Baisse de prix"}


def from_shops(events: list[dict], demo: bool) -> list[dict]:
    out = []
    for e in events:
        if e["event"] not in EVENT_LABEL:
            continue
        out.append({
            "key": "shop:%s:%s:%s:%s%s" % (e["shop"], e["product_id"], e["event"], e.get("price"), ":demo" if demo else ""),
            "kind": "shop", "event": e["event"],
            "title": "%s · %s" % (EVENT_LABEL[e["event"]], e["shop"]),
            "body": e.get("title", ""), "url": e.get("url", ""), "image": e.get("image", ""),
            "price": e.get("price"), "old_price": e.get("old_price"), "demo": demo,
        })
    return out
