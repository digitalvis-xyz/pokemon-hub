"""Card catalogue (TCGdex, open source, free, no key) + screenshot import with Claude vision.

TCGdex: https://tcgdex.dev  -  ids look like "sv03.5-199" (set id + local number).
Responses are cached in state/cache/ so the app stays fast and polite.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from common import STATE_DIR, Http, norm, post_json

API = "https://api.tcgdex.net/v2"
CACHE_DIR = os.path.join(STATE_DIR, "cache")
_http = Http(user_agent="TCGHub/0.2 (+personal collector tool)")


def _get(path: str, params: dict | None = None, ttl: int = 86400):
    """GET TCGdex JSON with a small file cache."""
    key = hashlib.sha1((path + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()[:20]
    fpath = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(fpath) and time.time() - os.path.getmtime(fpath) < ttl:
        with open(fpath, encoding="utf-8") as f:
            return json.load(f)
    data = _http.get_json(API + path, params)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def _img(base: str | None, quality: str = "low") -> str:
    return "%s/%s.webp" % (base, quality) if base else ""


# TCGdex also hosts the Pokémon TCG *Pocket* mobile game (sets A1, A2a, B1, P-A…): not physical cards.
POCKET_SET = re.compile(r"^(A\d|B\d|P-[A-Z])", re.I)


def sets(lang: str = "fr") -> list[dict]:
    """All physical-card sets, newest first: id, name, official/total counts, logo."""
    data = [s for s in _get("/%s/sets" % lang, ttl=7 * 86400) if not POCKET_SET.match(s.get("id") or "")]
    out = [{
        "id": s.get("id"),
        "name": s.get("name"),
        "official": (s.get("cardCount") or {}).get("official"),
        "total": (s.get("cardCount") or {}).get("total"),
        "logo": (s.get("logo") or "") + (".webp" if s.get("logo") else ""),
    } for s in data]
    return list(reversed(out))  # API returns oldest first


def _set_counts(lang: str) -> dict:
    return {s["id"]: s for s in sets(lang)}


def search(q: str = "", set_id: str = "", lang: str = "fr", page: int = 1, per_page: int = 48,
           secret_only: bool = False) -> dict:
    """Search cards by name (contains) and/or set. Returns {"cards": [...], "page", "has_more"}."""
    counts = _set_counts(lang)
    if set_id:
        s = _get("/%s/sets/%s" % (lang, set_id), ttl=7 * 86400)
        cards = s.get("cards") or []
        if q:
            nq = norm(q)
            cards = [c for c in cards if nq in norm(c.get("name", "")) or nq == norm(str(c.get("localId", "")))]
        total_n = len(cards)
        cards = cards[(page - 1) * per_page: page * per_page]
        has_more = page * per_page < total_n
    else:
        if not q or len(q.strip()) < 2:
            return {"cards": [], "page": page, "has_more": False}
        params = {"name": q.strip(), "pagination:page": page, "pagination:itemsPerPage": per_page}
        cards = _get("/%s/cards" % lang, params, ttl=86400)
        has_more = len(cards) == per_page
    out = []
    for c in cards:
        cid = c.get("id", "")
        set_key = cid.rsplit("-", 1)[0] if "-" in cid else set_id
        if POCKET_SET.match(set_key or ""):
            continue
        meta = counts.get(set_key, {})
        local = str(c.get("localId", ""))
        official = meta.get("official")
        secret = bool(official and local.isdigit() and int(local) > int(official))
        if secret_only and not secret:
            continue
        out.append({
            "id": cid, "name": c.get("name"), "number": local, "set_id": set_key,
            "set_name": meta.get("name", ""), "set_total": str(official or ""), "secret": secret,
            "image": _img(c.get("image")),
        })
    if not set_id:  # newest sets first
        order = {s: i for i, s in enumerate(counts.keys())}
        out.sort(key=lambda c: order.get(c["set_id"], 9999))
    return {"cards": out, "page": page, "has_more": has_more}


def to_wishlist_entry(card_id: str) -> dict:
    """Full wishlist row for one card id, with FR and EN names."""
    fr = _get("/fr/cards/%s" % card_id, ttl=30 * 86400)
    try:
        en = _get("/en/cards/%s" % card_id, ttl=30 * 86400)
    except Exception:
        en = {}
    s = fr.get("set") or en.get("set") or {}
    abbr = (s.get("abbreviation") or {}) if isinstance(s.get("abbreviation"), dict) else {}
    code = abbr.get("official") or ""
    return {
        "id": card_id,
        "name_fr": fr.get("name") or en.get("name") or "",
        "name_en": en.get("name") or "",
        "set_name": s.get("name", ""),
        "set_code": code,
        "number": str(fr.get("localId") or en.get("localId") or ""),
        "set_total": str((s.get("cardCount") or {}).get("official") or ""),
        "rarity": fr.get("rarity") or en.get("rarity") or "",
        "image": _img(fr.get("image") or en.get("image"), "high"),
        "languages": ["fr"],
        "max_price": None,
        "allow_graded": False,
        "allow_lots": False,
    }


# ---------- screenshot import ----------

VISION_PROMPT = """This is a screenshot from a Pokémon TCG collection app (often Pokécardex) or a photo of cards.
List every distinct card you can identify. For each card return:
- name: card name exactly as printed (keep the language of the screenshot, e.g. "Dracaufeu ex")
- number: collector number before the slash (e.g. "199"), or null
- set_total: number after the slash (e.g. "165"), or null
- set_name: expansion name if visible, else null
- language: "fr", "en", "ja" or null
Ignore UI elements, prices and totals. Do not invent cards you cannot read.
Return JSON only: {"cards": [ ... ]}"""


def read_screenshot(image_b64: str, media_type: str = "image/jpeg") -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Ajoute ta clé API Anthropic dans Réglages pour lire les captures d'écran.")
    model = os.environ.get("CLAUDE_VISION_MODEL", "claude-sonnet-5-5")
    payload = {
        "model": model,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": VISION_PROMPT},
        ]}],
    }
    resp = post_json("https://api.anthropic.com/v1/messages", payload,
                     {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=120)
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Réponse illisible de Claude : " + text[:120])
    return json.loads(m.group(0)).get("cards", [])


def _num(v) -> str:
    return (str(v or "").strip().lstrip("0")) or ""


def resolve(detected: dict, lang_order=("fr", "en")) -> dict:
    """Find the TCGdex card(s) matching what was read on the screenshot."""
    name, number, total = detected.get("name") or "", _num(detected.get("number")), _num(detected.get("set_total"))
    candidates = []
    for lang in ([detected["language"]] if detected.get("language") in ("fr", "en") else []) + list(lang_order):
        if not name:
            break
        try:
            res = search(q=name, lang=lang, per_page=100)["cards"]
        except Exception:
            continue
        for c in res:
            score = 0
            if number and _num(c["number"]) == number:
                score += 2
            if total and _num(c["set_total"]) == total:
                score += 1
            if norm(c["name"]) == norm(name):
                score += 1
            c["score"] = score
            candidates.append(c)
        if any(c["score"] >= 3 for c in candidates):
            break
    seen, uniq = set(), []
    for c in sorted(candidates, key=lambda c: -c["score"]):
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    best = None
    if uniq:
        top = uniq[0]["score"]
        tie = len(uniq) > 1 and uniq[1]["score"] == top
        if top >= 3 or (top >= 2 and not tie) or (len(uniq) == 1 and top >= 1):
            best = uniq[0]  # confident; otherwise the user picks among the alternatives
    return {"detected": detected, "best": best, "alternatives": uniq[:6]}


def _set_cards(set_id: str, lang: str) -> list[dict]:
    try:
        return _get("/%s/sets/%s" % (lang, set_id), ttl=7 * 86400).get("cards") or []
    except Exception:
        return []


def _name_score(read: str, card_name: str) -> int:
    a, b = norm(read), norm(card_name)
    if not a or not b:
        return 0
    if a == b or a in b or b in a:
        return 2
    return 1 if a.split()[0] == b.split()[0] else 0


def resolve_by_number(detected: dict, lang: str = "fr") -> dict:
    """OCR path: '199/165' identifies the set(s) by official size; the name breaks ties."""
    number, total = _num(detected.get("number")), _num(detected.get("set_total"))
    if not number or not total:
        return resolve(detected)
    read = detected.get("name") or ""
    cands = []
    for st in sets(lang):
        if str(st.get("official") or "") != total:
            continue
        for c in _set_cards(st["id"], lang):
            if _num(c.get("localId")) != number:
                continue
            en_name = next((e.get("name", "") for e in _set_cards(st["id"], "en") if e.get("id") == c.get("id")), "")
            score = 2 + max(_name_score(read, c.get("name", "")), _name_score(read, en_name))
            cands.append({"id": c.get("id"), "name": c.get("name"), "number": str(c.get("localId")),
                          "set_id": st["id"], "set_name": st["name"], "set_total": str(st["official"]),
                          "secret": int(number) > int(total), "image": _img(c.get("image")), "score": score})
    if not cands:
        return resolve(detected) if read else {"detected": detected, "best": None, "alternatives": []}
    cands.sort(key=lambda c: -c["score"])
    tie = len(cands) > 1 and cands[1]["score"] == cands[0]["score"]
    best = cands[0] if (len(cands) == 1 or (not tie and cands[0]["score"] >= 3)) else None
    return {"detected": detected, "best": best, "alternatives": cands[:6]}
