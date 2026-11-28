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
DESC_BLOCK_WORDS = ["proxy", "fake", "replica", "replique", "reproduction", "orica", "non officiel", "unofficial", "fan art"]
LOT_WORDS = [" lot ", "lot de", " lote ", "bundle", "collection complete", "x cartes", "cartes pokemon en vrac", "bulk",
             "classeur", "binder", "pack de", "set de", "plusieurs cartes", "various cards", "varias cartas", "vrac"]
LOT_COUNT = re.compile(r"\b\d{2,4}\s*(?:cartes|cards|cartas|carte)\b")
ANY_NUMBER = re.compile(r"(?<![\d/])\d{1,3}[a-z]?\s*/\s*\d{1,3}(?![\d/])")


def looks_like_lot(text: str) -> bool:
    """Group sale: lot words, 'NN cartes', or several collector numbers in the same listing."""
    return (any(w in text for w in LOT_WORDS) or bool(LOT_COUNT.search(text))
            or len(set(ANY_NUMBER.findall(text))) >= 2)
GRADED_WORDS = ["psa", "pca", "cgc", "bgs", "beckett", "collect aura", "gradee", "graded", "slab"]
# Explicit card-language markers (what the seller says about the CARD).
LANG_HINTS = {
    "ja": ["japonais", "japanese", " jap ", " jp ", " jpn ", "japan", "giapponese", "japones", "japanisch"],
    "en": ["anglais", "english", " eng ", " en ", "inglese", "ingles", "englisch", " anglaise"],
    "fr": ["francais", "french", " fr ", " vf ", "francese", "frances", "franzosisch", "francaise"],
    "it": [" ita ", "italiano", "italiana", "italian", "italien", " it ", "versione italiana"],
    "de": ["deutsch", " deu ", " ger ", "german", "allemand", "tedesco", "aleman"],
    "es": ["espanol", "spanish", "espagnol", "spagnolo", " esp ", " spa "],
    "pt": ["portugues", "portuguese", "portugais", "portoghese", " pt-pt"],
    "zh": ["chinois", "chinese", "cinese", "chino", " chn ", " cn "],
    "ko": ["coreen", "korean", "coreano", " kor "],
}
# Seller-language clues: on Vinted a card is usually in its seller's language unless stated.
SELLER_WORDS = {
    "it": ["condizioni", "spedizione", "perfette", "ottime", "nuova", "nuovo", "come da foto", "carta pokemon", "originale",
           "vendo", "prezzo", "trattabile", "spedisco", "bustina", "per info"],
    "fr": ["etat", "neuve", "envoi", "tres bon", "carte pokemon", "jamais jouee", "protegee", "comme neuve", "lettre suivie"],
    "es": ["estado", "envio", "carta pokemon", "nueva", "perfecto", "vendo carta"],
    "de": ["zustand", "versand", "karte", "neuwertig", "sehr gut"],
    "pt": ["estado", "envio", "carta pokemon", "nova", "otimo", "portes"],
    "en": ["condition", "shipping", "mint", "pack fresh", "pulled"],
}

# Vinted "status" (state of the item), every language mapped to a rank 5 (new) … 1 (satisfactory).
CONDITIONS = {
    5: ["neuf avec etiquette", "new with tags", "novo com etiqueta", "nuovo con cartellino", "nuevo con etiqueta", "neu mit etikett"],
    4: ["neuf sans etiquette", "new without tags", "novo sem etiqueta", "nuovo senza cartellino", "nuevo sin etiqueta", "neu ohne etikett"],
    3: ["tres bon etat", "very good", "muito bom", "ottime condizioni", "ottimo", "muy bueno", "sehr gut"],
    2: ["bon etat", "good", "bom", "buone condizioni", "buono", "bueno", "gut"],
    1: ["satisfaisant", "satisfactory", "satisfatorio", "discrete condizioni", "discreto", "satisfactorio", "zufriedenstellend"],
}
CONDITION_LABEL = {5: "Neuf avec étiquette", 4: "Neuf sans étiquette", 3: "Très bon état", 2: "Bon état", 1: "Satisfaisant"}


def condition_rank(text: str) -> int | None:
    t = norm(text or "")
    if not t:
        return None
    for rank in (5, 4, 3, 1, 2):  # check "tres bon" before "bon", "satisfaisant" before "good"
        if any(w in t for w in CONDITIONS[rank]):
            return rank
    return None


def card_language(text: str, card: dict) -> tuple[str | None, bool]:
    """(language, explicit). Explicit marker first, then a French-only name, then seller language."""
    for lg, hints in LANG_HINTS.items():
        if any(h in text for h in hints):
            return lg, True
    fr, en = norm(card.get("name_fr", "")), norm(card.get("name_en", ""))
    fr_base, en_base = fr.replace(" ex", "").strip(), en.replace(" ex", "").strip()
    if fr_base and fr_base != en_base and fr_base in text:
        return "fr", True  # "Dracaufeu" only exists on French cards
    country = (card.get("_seller_country") or "").upper()
    by_country = {"IT": "it", "FR": "fr", "BE": "fr", "LU": "fr", "ES": "es", "DE": "de", "AT": "de", "PT": "pt",
                  "NL": "nl", "GB": "en", "UK": "en", "IE": "en", "US": "en", "PL": "pl", "CZ": "cs", "LT": "lt"}
    scores = {lg: sum(1 for w in words if w in text) for lg, words in SELLER_WORDS.items()}
    if country in by_country and scores.get(by_country[country], 0) >= 0 and max(scores.values()) < 2:
        return by_country[country], False
    best = max(scores, key=scores.get)
    if scores[best] >= 2 or (scores[best] == 1 and sorted(scores.values())[-2] == 0 and best != "en"):
        return best, False
    return None, False

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

    # Accessory words (sleeve, toploader…) only count in the title: descriptions of real cards
    # often say "sleevée" or "envoyée en toploader". In the description only fakes are blocking.
    title_txt = " " + norm(listing.get("title", "")) + " "
    desc_txt = " " + norm(listing.get("description", "")) + " "
    blocked = [w for w in BLOCK_WORDS if w in title_txt] + [w for w in DESC_BLOCK_WORDS if w in desc_txt and w not in title_txt]
    is_lot = looks_like_lot(text)
    is_graded = any(w in text for w in GRADED_WORDS)
    lang, lang_explicit = card_language(text, dict(card, _seller_country=listing.get("seller_country", "")))
    cond_rank = condition_rank(listing.get("condition", ""))

    reject = []
    if blocked:
        reject.append("blocked:" + ",".join(blocked))
    if is_lot and not card.get("allow_lots"):
        reject.append("lot")
    if is_graded and not card.get("allow_graded"):
        reject.append("graded")
    wanted = card.get("languages") or []
    if lang and wanted and lang not in wanted:
        # explicit marker = hard reject; seller-language guess = soft (goes to "À vérifier")
        reject.append(("language:" if lang_explicit else "language?:") + lang)
    min_cond = card.get("min_condition")
    if min_cond and cond_rank is not None and cond_rank < int(min_cond):
        reject.append("condition:" + CONDITION_LABEL[cond_rank])
    price = listing.get("price")
    if price is not None and card.get("max_price") and price > card["max_price"]:
        reject.append("over_budget")
    if "exact_number" not in flags and "number" not in flags:
        reject.append("no_number")

    # Where were the card's name and unique ID (199/165) found? Title and description separately.
    def hits(t: str) -> tuple[bool, bool]:
        t = " " + norm(t) + " "
        return (bool(pats and pats[0].search(t)), any(nb and nb in t for nb in name_base))
    id_t, name_t = hits(listing.get("title", ""))
    id_d, name_d = hits(listing.get("description", "")) if listing.get("description") else (False, False)
    id_hit, name_hit = id_t or id_d, name_t or name_d

    out = dict(listing)
    out.update(
        id_hit=id_hit, name_hit=name_hit,
        confirmed_in=("titre" if (id_t and name_t) else "description" if (id_hit and name_hit) else ""),
        card_id=card["id"],
        rule_score=score,
        rule_flags=flags,
        rule_reject=reject,
        guess_lang=lang, lang_explicit=lang_explicit, cond_rank=cond_rank,
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
