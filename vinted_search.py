#!/usr/bin/env python3
"""Prototype 1 - search Vinted for every card in wishlist.json and classify the results.

Usage:
  python3 vinted_search.py                 # live, vinted.fr
  python3 vinted_search.py --domain pt     # vinted.pt
  python3 vinted_search.py --fixtures      # offline test with fixtures/vinted_sample.json
  python3 vinted_search.py --no-ai         # rules only, even if ANTHROPIC_API_KEY is set

Uses Vinted's undocumented public catalog endpoint (the one the website calls).
It is not an official API: keep the frequency low, personal use only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from classify import claude_check, rule_check
from common import ROOT, STATE_DIR, Http, data_path, ensure_data, load_json, polite_sleep, save_json, stamp
from notify import telegram
from report import write_csv, write_html


def build_queries(card: dict) -> list[str]:
    """Several phrasings per card, because sellers write titles in many ways."""
    n, total, code = card["number"], card.get("set_total", ""), card.get("set_code", "")
    q = []
    for name in (card.get("name_fr"), card.get("name_en")):
        if not name:
            continue
        q.append("%s %s/%s" % (name, n, total) if total else "%s %s" % (name, n))
    if code and card.get("name_fr"):
        q.append("%s %s %s" % (card["name_fr"], code, n))
    seen, out = set(), []
    for s in q:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


class Vinted:
    def __init__(self, domain: str = "fr"):
        self.base = "https://www.vinted.%s" % domain
        self.http = Http()
        self._ready = False

    def _session(self) -> None:
        # Visiting the home page sets the anonymous session cookies the API expects.
        status, _ = self.http.get(self.base + "/")
        if status >= 400:
            raise RuntimeError("Vinted home page returned HTTP %s (blocked? try later or another network)" % status)
        self._ready = True

    def search(self, text: str, price_to: float | None = None, per_page: int = 48) -> list[dict]:
        if not self._ready:
            self._session()
        params = {"search_text": text, "order": "newest_first", "per_page": per_page, "page": 1}
        if price_to:
            params.update(price_to=int(price_to), currency="EUR")
        url = self.base + "/api/v2/catalog/items"
        status, body = self.http.get(url, params, {"Accept": "application/json", "Referer": self.base + "/catalog"})
        if status == 401:  # session expired: refresh once
            self._session()
            status, body = self.http.get(url, params, {"Accept": "application/json"})
        if status != 200:
            raise RuntimeError("Vinted search HTTP %s: %s" % (status, body[:150]))
        return [normalise_item(it, self.base) for it in json.loads(body).get("items", [])]


def _price(it: dict) -> float | None:
    p = it.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def normalise_item(it: dict, base: str) -> dict:
    photo = it.get("photo") or {}
    return {
        "source": "vinted",
        "listing_id": str(it.get("id")),
        "title": it.get("title", ""),
        "description": it.get("description", ""),
        "price": _price(it),
        "condition": it.get("status") or "",
        "seller": (it.get("user") or {}).get("login", ""),
        "url": it.get("url") or "%s/items/%s" % (base, it.get("id")),
        "photo_url": photo.get("url") or photo.get("full_size_url") or "",
    }


VERDICT_ORDER = {"match": 0, "review": 1, "rejected": 2}


def search(cards: list[dict], domain: str = "fr", fixtures: bool = False, use_ai: bool | None = None,
           recheck: bool = False, delay: float = 3.0, log=print, send_alerts: bool = True) -> list[dict]:
    """Search Vinted for every card and classify. Returns rows sorted best first."""
    seen_path = os.path.join(STATE_DIR, "vinted_seen.json")
    seen = load_json(seen_path, {})
    if use_ai is None:
        use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_ai = use_ai and bool(os.environ.get("ANTHROPIC_API_KEY"))
    log("Wishlist: %d cards · AI classification: %s · vinted.%s%s" % (
        len(cards), "on" if use_ai else "off", domain, " · DEMO DATA" if fixtures else ""))

    fixture = load_json(os.path.join(ROOT, "fixtures", "vinted_sample.json"), {}) if fixtures else None
    client = None if fixtures else Vinted(domain)

    rows = []
    for card in cards:
        listings = {}
        for q in build_queries(card):
            try:
                if fixture is not None:
                    items = [normalise_item(it, "https://www.vinted.fr") for it in fixture.get("items", [])]
                else:
                    items = client.search(q, price_to=card.get("max_price"))
                    polite_sleep(delay)
            except Exception as e:
                log("  ! %s -> %s" % (q, e))
                continue
            for it in items:
                listings.setdefault(it["listing_id"], it)
            log("  %-40s %3d results" % (q[:40], len(items)))

        for it in listings.values():
            r = rule_check(it, card)
            r["query_card"] = "%s %s/%s" % (card.get("name_fr") or card.get("name_en"), card["number"], card.get("set_total", ""))
            r["new"] = it["listing_id"] not in seen
            r["demo"] = fixtures
            if not r["rule_reject"] and r["rule_score"] >= 55:
                r["verdict"] = "match"
            elif not r["rule_reject"] and r["rule_score"] >= 25:
                r["verdict"] = "review"
            else:
                r["verdict"] = "rejected"

            if use_ai and r["verdict"] in ("match", "review") and (r["new"] or recheck):
                ai = claude_check(it, card) or {}
                r["ai_match"] = ai.get("match")
                r["ai_confidence"] = ai.get("confidence")
                r["ai_lang"] = ai.get("language")
                r["ai_reason"] = ai.get("reason")
                if ai.get("match") is False:
                    r["verdict"] = "rejected"
                elif ai.get("match") and (ai.get("confidence") or 0) >= 0.7:
                    r["verdict"] = "match"
            rows.append(r)

    rows.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], r.get("deal_ratio") or 9))
    fresh = [r for r in rows if r["verdict"] == "match" and r["new"]]
    if not fixtures:
        for r in rows:
            seen[r["listing_id"]] = r["verdict"]
        save_json(seen_path, seen)

    stats = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICT_ORDER}
    log("%d listings · %d match · %d review · %d rejected · %d new matches" % (
        len(rows), stats["match"], stats["review"], stats["rejected"], len(fresh)))
    if fresh and send_alerts and not fixtures:
        msg = "\n\n".join("%s\n%.0f € (%s)\n%s" % (r["query_card"], r["price"] or 0, r["title"][:60], r["url"]) for r in fresh[:10])
        if telegram("Nouvelles annonces Vinted\n\n" + msg):
            log("Telegram alert sent.")
    return rows


def run(args) -> int:
    ensure_data()
    cards = load_json(args.wishlist or data_path("wishlist.json"), {"cards": []})["cards"]
    use_ai = False if args.no_ai else None
    rows = search(cards, args.domain, args.fixtures, use_ai, args.recheck, args.delay)
    name = "vinted_" + stamp()
    cols = ["verdict", "new", "query_card", "title", "price", "deal_ratio", "condition", "guess_lang",
            "rule_score", "rule_flags", "rule_reject", "ai_match", "ai_confidence", "ai_reason", "seller", "url"]
    csv_path = write_csv(rows, name, cols)
    shown = rows if args.show_rejected else [r for r in rows if r["verdict"] != "rejected"]
    html_path = write_html(shown, name, "Vinted × wishlist · " + name, [c for c in cols if c != "url"],
                           image_key="photo_url", link_key="url")
    print("CSV : %s\nHTML: %s" % (csv_path, html_path))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wishlist", default="", help="default: data/wishlist.json")
    ap.add_argument("--domain", default="fr", help="fr, pt, es, be, it, de ...")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--fixtures", action="store_true", help="offline test data")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--recheck", action="store_true", help="re-run AI on already seen listings")
    ap.add_argument("--show-rejected", action="store_true", help="include rejected rows in the HTML")
    sys.exit(run(ap.parse_args()))
