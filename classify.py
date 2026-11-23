"""Map a raw listing back to a canonical card ID.

Two stages:
  1. rules  - free, instant: card number present, no proxy/fake words, price ceiling.
  2. claude - optional (needs ANTHROPIC_API_KEY): reads title + photo and returns
              structured JSON (card id, language, lot, graded, confidence).
Only listings that survive stage 1 are sent to stage 2, so the API cost stays tiny.
"""
from __future__ import annotations

import json
import os
import re

from common import norm, post_json

BLOCK_WORDS = [
    "proxy", "custom", "fake", "replica", "replique", "reproduction", "fan art", "fanart",
    "orica", "metal card", "carte metal", "gold metal", "non officiel", "unofficial",
    "jumbo", "oversize", "sticker", "autocollant", "toploader vide", "sleeve", "pochette vide",
]
LOT_WORDS = ["lot ", "lot de", "bundle", "collection complete", "x cartes", "cartes pokemon en vrac", "bulk"]
GRADED_WORDS = ["psa", "pca", "cgc", "bgs", "beckett", "collect aura", "gradee", "graded", "slab"]
LANG_HINTS = {
    "ja": ["japonais", "japanese", "jap ", " jp", "japan"],
    "en": ["anglais", "english", " eng", " en "],
    "fr": ["francais", "french", " fr ", " vf"],
    "pt": ["portugues", "portuguese"],
}

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")


def _number_patterns(card: dict) -> list[re.Pattern]:
    n = card["number"].lstrip("0") or "0"
    total = card.get("set_total", "")
    pats = [re.compile(r"(?<!\d)0*%s\s*/\s*0*%s(?!\d)" % (re.escape(n), re.escape(total)))] if total else []
    # looser: "#199", "n°199", "199 " next to the set code
    pats.append(re.compile(r"(?:#|n°|no\.?\s?|%s\s?)0*%s(?!\d)" % (re.escape(norm(card.get("set_code", "zzz"))), re.escape(n))))
    pats.append(re.compile(r"(?<![\d/])0*%s(?![\d/])" % re.escape(n)))
    return pats


def rule_check(listing: dict, card: dict) -> dict:
    """Return listing enriched with rule flags and a 0-100 rule score."""
    text = " " + norm(listing.get("title", "") + " " + listing.get("description", "")) + " "
    names = [norm(card.get("name_fr", "")), norm(card.get("name_en", ""))]
    name_base = [nm.replace(" ex", "").strip() for nm in names if nm]

    flags = []
    score = 0
    pats = _number_patterns(card)
    if pats and pats[0].search(text):
        score += 50
        flags.append("exact_number")
    elif any(p.search(text) for p in pats[1:]):
        score += 25
        flags.append("number")
    if any(nb and nb in text for nb in name_base):
        score += 30
        flags.append("name")
    if norm(card.get("set_code", "")) and (" %s " % norm(card["set_code"])) in text:
        score += 10
        flags.append("set_code")

    blocked = [w for w in BLOCK_WORDS if w in text]
    is_lot = any(w in text for w in LOT_WORDS)
    is_graded = any(w in text for w in GRADED_WORDS)
    lang = next((lg for lg, hints in LANG_HINTS.items() if any(h in text for h in hints)), None)

    reject = []
    if blocked:
        reject.append("blocked:" + ",".join(blocked))
    if is_lot and not card.get("allow_lots"):
        reject.append("lot")
    if is_graded and not card.get("allow_graded"):
        reject.append("graded")
    if lang and card.get("languages") and lang not in card["languages"]:
        reject.append("language:" + lang)
    price = listing.get("price")
    if price is not None and card.get("max_price") and price > card["max_price"]:
        reject.append("over_budget")
    if "exact_number" not in flags and "number" not in flags:
        reject.append("no_number")

    out = dict(listing)
    out.update(
        card_id=card["id"],
        rule_score=score,
        rule_flags=flags,
        rule_reject=reject,
        guess_lang=lang,
        is_lot=is_lot,
        is_graded=is_graded,
        deal_ratio=round(price / card["max_price"], 2) if price and card.get("max_price") else None,
    )
    return out


SYSTEM = (
    "You classify second-hand Pokemon TCG listings. You receive one wanted card and one listing "
    "(title, optional description, optional photo). Decide if the listing is a single, genuine copy "
    "of exactly that card (same set and same collector number). Answer with JSON only, no prose."
)


def claude_check(listing: dict, card: dict) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    wanted = {k: card.get(k) for k in ("id", "name_fr", "name_en", "set_name", "set_code", "number", "set_total")}
    content = []
    if listing.get("photo_url"):
        content.append({"type": "image", "source": {"type": "url", "url": listing["photo_url"]}})
    content.append({
        "type": "text",
        "text": (
            "Wanted card: %s\nListing title: %s\nListing description: %s\nPrice: %s EUR\n\n"
            "Return exactly this JSON shape:\n"
            '{"match": true|false, "card_id": "<wanted id or null>", "language": "fr|en|ja|pt|other|unknown", '
            '"single_card": true|false, "graded": true|false, "grade": "<e.g. PSA 10 or null>", '
            '"condition": "<short or unknown>", "authentic_looking": true|false|null, '
            '"confidence": 0.0-1.0, "reason": "<max 15 words>"}'
        ) % (json.dumps(wanted, ensure_ascii=False), listing.get("title", ""),
             (listing.get("description") or "")[:800], listing.get("price")),
    })
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 300,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        resp = post_json("https://api.anthropic.com/v1/messages", payload, headers)
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {"match": False, "reason": "unparseable: " + text[:80]}
    except Exception as e:  # keep the run going if one call fails
        return {"match": None, "reason": "api error: %s" % str(e)[:120]}
