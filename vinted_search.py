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
import re
import sys

from classify import claude_check, rule_check
from common import ROOT, STATE_DIR, Http, ensure_profiles, load_json, pfile, polite_sleep, pstate, save_json, stamp
from notify import telegram
from report import write_csv, write_html


def build_queries(card: dict) -> list[str]:
    """One query per language the card is wanted in. Vinted searches the title text as written by the
    seller (it doesn't translate the search), so a French title needs the French name and an English
    title the English name; the collector number 199/165 is the same everywhere and keeps it precise."""
    n, total = card["number"], card.get("set_total", "")
    num = "%s/%s" % (n, total) if total else n
    langs = card.get("languages") or ["fr"]
    names = []
    if "fr" in langs and card.get("name_fr"):
        names.append(card["name_fr"])
    if any(l in langs for l in ("en", "ja", "pt")) and card.get("name_en"):
        names.append(card["name_en"])  # JP/PT listings on Vinted are mostly titled in English
    if not names:
        names = [card.get("name_fr") or card.get("name_en") or ""]
    seen, out = set(), []
    for name in names:
        q = ("%s %s" % (name, num)).strip()
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


class Vinted:
    """Vinted catalogue search, as of the September 2026 change.

    1. api   : https://api.vinted.<tld>/svc-catalogue/items, with the anonymous session the
               www site hands out (access_token_web cookie as bearer token, X-Anon-Id, CSRF).
    2. html  : fallback, reads the server-rendered https://www.vinted.<tld>/catalog page.
    Every attempt is recorded in self.attempts so the Diagnostic tab can show what happened.
    """

    def __init__(self, domain: str = "fr"):
        self.domain = domain
        self.www = "https://www.vinted.%s" % domain
        self.api = "https://api.vinted.%s/svc-catalogue/items" % domain
        self.http = Http()
        self.ready = False
        self.token = self.anon = self.csrf = None
        self.attempts: list[dict] = []
        self.mode = None  # "api" or "html" once one works

    def _note(self, step: str, status, detail: str = "") -> None:
        self.attempts.append({"step": step, "status": status, "detail": detail[:300]})

    def _bootstrap(self) -> None:
        status, body = self.http.get(self.www + "/")
        hdrs = {k.lower(): v for k, v in (self.http.last_headers or {}).items()}
        self.token = self.http.cookie("access_token_web")
        self.anon = hdrs.get("x-anon-id") or self.http.cookie("anon_id")
        m = (re.search(r'name="csrf-token"\s+content="([^"]+)"', body)
             or re.search(r'CSRF_TOKEN\\?"\s*:\s*\\?"([0-9a-fA-F-]{20,})', body))
        self.csrf = m.group(1) if m else None
        self._note("session www.vinted.%s" % self.domain, status,
                   "token=%s anon_id=%s csrf=%s" % (bool(self.token), bool(self.anon), bool(self.csrf)))
        if status >= 400:
            raise RuntimeError("Vinted a refusé l'ouverture de session (HTTP %s)" % status)
        self.ready = True

    def _api_headers(self) -> dict:
        h = {"Accept": "application/json, text/plain, */*", "Origin": self.www, "Referer": self.www + "/catalog"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.anon:
            h["X-Anon-Id"] = self.anon
        if self.csrf:
            h["X-Csrf-Token"] = self.csrf
        return h

    def _api_search(self, text: str, price_to: float | None, per_page: int) -> list[dict]:
        base = {"search_text": text, "page": 1, "per_page": per_page}
        ladders = [dict(base, order="newest_first", currency="EUR", **({"price_to": int(price_to)} if price_to else {})),
                   dict(base, order="newest_first"),
                   base]
        last = None
        for i, params in enumerate(ladders):
            params = {k: v for k, v in params.items() if v not in (None, "")}  # blank values => 400
            status, body = self.http.get(self.api, params, self._api_headers())
            if status in (401, 403) and i == 0:
                self._note("api", status, "session expirée, nouvelle session")
                self._bootstrap()
                status, body = self.http.get(self.api, params, self._api_headers())
            self._note("api %s" % ",".join(sorted(params)), status, body[:200] if status != 200 else "")
            if status == 200:
                data = json.loads(body)
                items = (data.get("items") or data.get("catalog_items")
                         or (data.get("data") or {}).get("items") or [])
                return [normalise_item(it, self.www) for it in items]
            last = (status, body)
            if status != 400:
                break
        raise RuntimeError("API Vinted HTTP %s: %s" % (last[0], last[1][:150]))

    def _html_search(self, text: str, price_to: float | None) -> list[dict]:
        params = {"search_text": text, "order": "newest_first"}
        if price_to:
            params["price_to"] = int(price_to)
        status, body = self.http.get(self.www + "/catalog", params, {"Accept": "text/html"})
        items = parse_catalog_html(body, self.www) if status == 200 else []
        self._note("page catalogue", status, "%d annonces lues" % len(items) if status == 200 else body[:200])
        if status != 200:
            raise RuntimeError("Page catalogue Vinted HTTP %s" % status)
        return items

    def item_description(self, url: str) -> str | None:
        """Description of one listing, read from its public page (JSON-LD, then meta tags)."""
        try:
            status, page = self.http.get(url, headers={"Accept": "text/html"})
        except Exception as e:
            self._note("annonce", "erreur", str(e))
            return None
        if status != 200:
            self._note("annonce", status, url)
            return None
        for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
            try:
                data = json.loads(html_unescape(block.strip()))
            except Exception:
                continue
            for obj in (data if isinstance(data, list) else [data]):
                if isinstance(obj, dict) and obj.get("description"):
                    return str(obj["description"])
        m = (re.search(r'<meta[^>]+(?:name|property)="(?:og:)?description"[^>]+content="([^"]*)"', page)
             or re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){10,4000})"', page))
        if m:
            txt = m.group(1)
            try:
                txt = json.loads('"%s"' % txt) if "\\" in txt else txt
            except Exception:
                pass
            return html_unescape(txt)
        return ""

    def search(self, text: str, price_to: float | None = None, per_page: int = 48) -> list[dict]:
        if not self.ready:
            self._bootstrap()
        if self.mode in (None, "api"):
            try:
                items = self._api_search(text, price_to, per_page)
                self.mode = "api"
                return items
            except Exception as e:
                if self.mode == "api":
                    raise
                self._note("api", "échec", str(e))
        items = self._html_search(text, price_to)
        self.mode = "html"
        return items


def _price(it: dict) -> float | None:
    p = it.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    if p is None:
        p = it.get("price_numeric")
    if isinstance(p, str):
        p = p.replace("€", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def normalise_item(it: dict, base: str) -> dict:
    photo = it.get("photo") or ((it.get("photos") or [None])[0]) or {}
    if isinstance(photo, str):
        photo = {"url": photo}
    box = it.get("item_box") or {}
    status = it.get("status") or ""
    if not status and box.get("second_line"):
        status = box["second_line"].split("·")[-1].strip()
    url = it.get("url") or it.get("path") or "/items/%s" % it.get("id")
    if url.startswith("/"):
        url = base + url
    user = it.get("user") or {}
    return {
        "source": "vinted",
        "listing_id": str(it.get("id")),
        "title": it.get("title", ""),
        "description": it.get("description", ""),
        "price": _price(it),
        "condition": status if isinstance(status, str) else "",
        "seller": user.get("login", "") if isinstance(user, dict) else "",
        "url": url,
        "photo_url": photo.get("url") or photo.get("full_size_url") or "",
        "description_full": it.get("description_full", ""),  # only in offline fixtures
    }


_A_TAG = re.compile(r"<a\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z_:-]+)="([^"]*)"')
_IMG = re.compile(r'<img\b[^>]*src="(https://images\d*\.vinted\.net/[^"]+)"', re.I)


def parse_catalog_html(page: str, base: str) -> list[dict]:
    """Read listings from the server-rendered catalogue page (fallback when the API refuses us)."""
    out, seen = [], set()
    for m in _A_TAG.finditer(page):
        attrs = dict(_ATTR.findall(m.group(0)))
        href = attrs.get("href", "")
        idm = re.search(r"/items/(\d+)", href)
        title_attr = html_unescape(attrs.get("title", ""))
        if not idm or not title_attr or idm.group(1) in seen:
            continue
        seen.add(idm.group(1))
        # "Title, marque: X, état: Très bon état, taille: Y, 12,00 €, 13,20 € inclut ..."
        title = re.split(r",\s*(?:[^,:]{2,25}):\s", title_attr, maxsplit=1)[0].strip()
        pm = re.search(r"(\d[\d\s ]*[.,]\d{2})\s*€", title_attr)
        cond = re.search(r"(?:état|estado|condition|zustand|stato|staat)\s*:\s*([^,]+)", title_attr, re.I)
        img = _IMG.search(page, m.end(), m.end() + 4000)
        out.append({
            "source": "vinted", "listing_id": idm.group(1), "title": title, "description": title_attr,
            "price": _price({"price": pm.group(1)}) if pm else None,
            "condition": cond.group(1).strip() if cond else "", "seller": "",
            "url": href if href.startswith("http") else base + href,
            "photo_url": img.group(1) if img else "",
        })
    return out


def html_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


VERDICT_ORDER = {"match": 0, "review": 1, "lot": 2, "lot_weak": 3, "more": 4, "rejected": 5}


def decide(r: dict) -> str:
    """Verdict from the rules. A match needs BOTH the card's name and its unique ID (199/165),
    in the title or the description. Group sales are split the same way."""
    reject = [x for x in r["rule_reject"] if x not in ("lot", "no_number")]
    if any(x.startswith("blocked") for x in reject):
        return "rejected"                       # proxy, fake, custom…
    full = r.get("id_hit") and r.get("name_hit")
    if r["is_lot"]:
        return "lot" if full else "lot_weak"    # lot that provably contains the card / maybe
    if full and not reject:
        return "match"
    if r.get("name_hit") and ("number" in r["rule_flags"] or "exact_number" in r["rule_flags"]) and not reject:
        return "review"                         # name + a number that isn't clearly 199/165
    return "rejected"


def needs_description(r: dict) -> bool:
    """Worth opening the listing: the name is there but the ID isn't (often written in the
    description), or a lot that might contain the card."""
    if r.get("desc_checked") or any(x.startswith("blocked") for x in r["rule_reject"]):
        return False
    if r["verdict"] == "lot_weak":
        return True
    return r.get("name_hit") and not r.get("id_hit") and set(r["rule_reject"]) <= {"no_number"}


def search(cards: list[dict], domain: str = "fr", fixtures: bool = False, use_ai: bool | None = None,
           recheck: bool = False, delay: float = 3.0, log=print, send_alerts: bool = True,
           stop=None, top_k: int = 3, desc_checks: int = 6) -> list[dict]:
    """Search Vinted for every card and classify. Returns rows sorted best first."""
    seen_path = pstate("vinted_seen.json")
    seen = load_json(seen_path, {})
    if use_ai is None:
        use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_ai = use_ai and bool(os.environ.get("ANTHROPIC_API_KEY"))
    log("Wishlist: %d cards · AI classification: %s · vinted.%s%s" % (
        len(cards), "on" if use_ai else "off", domain, " · DEMO DATA" if fixtures else ""))

    fixture = load_json(os.path.join(ROOT, "fixtures", "vinted_sample.json"), {}) if fixtures else None
    client = None if fixtures else Vinted(domain)

    rows = []
    stopped = False
    for card in cards:
        if stop and stop():
            stopped = True
            log("Arrêt demandé : recherche interrompue.")
            break
        listings = {}
        for q in build_queries(card):
            if stop and stop():
                break
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

        card_rows = []
        for it in listings.values():
            r = rule_check(it, card)
            r["query_card"] = "%s %s/%s" % (card.get("name_fr") or card.get("name_en"), card["number"], card.get("set_total", ""))
            r["new"] = it["listing_id"] not in seen
            r["demo"] = fixtures
            r["verdict"] = decide(r)
            card_rows.append(r)

        # Open the most promising uncertain listings and read their description:
        # sellers often put the number (199/165) there, and lots list their cards there.
        def read_desc(x: dict) -> None:
            """Open one listing, re-run the rules with its description, replace it in card_rows."""
            if client is not None:
                desc = client.item_description(x["url"])
                polite_sleep(min(delay, 2.0))
            else:
                desc = listings[x["listing_id"]].get("description_full")  # offline fixtures
            if not desc:
                x["desc_checked"] = True
                return
            y = rule_check(dict(listings[x["listing_id"]], description=desc), card)
            y.update({k: x[k] for k in ("query_card", "new", "demo")}, desc_checked=True)
            y["verdict"] = decide(y)
            card_rows[card_rows.index(x)] = y

        # 1) uncertain listings: name without the ID (sellers often write 199/165 in the description), uncertain lots
        todo = sorted((x for x in card_rows if needs_description(x)), key=lambda x: x.get("price") or 1e9)[:desc_checks]
        for x in todo:
            if stop and stop():
                break
            read_desc(x)
        # 2) the offers you'll actually see: check the best ones aren't proxies/fakes in the description
        reads = len(todo)
        if desc_checks:
            for _ in range(top_k * 2):
                if stop and stop():
                    break
                tops = sorted((x for x in card_rows if x["verdict"] == "match"), key=lambda x: x.get("price") or 1e9)[:top_k]
                unchecked = [x for x in tops if not x.get("desc_checked")]
                if not unchecked:
                    break
                read_desc(unchecked[0])
                reads += 1
        if reads:
            confirmed = sum(1 for x in card_rows if x.get("desc_checked") and x.get("confirmed_in") == "description")
            dropped = sum(1 for x in card_rows if x.get("desc_checked") and x["verdict"] == "rejected")
            log("    %d description(s) lue(s) · %d confirmée(s) par la description · %d écartée(s)" % (reads, confirmed, dropped))

        for r in card_rows:
            if use_ai and r["verdict"] in ("match", "review") and (r["new"] or recheck):
                ai = claude_check(listings[r["listing_id"]], card) or {}
                r["ai_match"] = ai.get("match")
                r["ai_confidence"] = ai.get("confidence")
                r["ai_lang"] = ai.get("language")
                r["ai_reason"] = ai.get("reason")
                if ai.get("match") is False:
                    r["verdict"] = "rejected"
                elif ai.get("match") and (ai.get("confidence") or 0) >= 0.7:
                    r["verdict"] = "match"
        rows.extend(card_rows)

    # Keep it readable: per card, only the K cheapest matches stay "match", the next reviews/lots are capped too.
    by_card: dict[str, list[dict]] = {}
    for r in rows:
        by_card.setdefault(r["card_id"], []).append(r)
    for card_rows in by_card.values():
        for verdict, keep in (("match", top_k), ("review", 3), ("lot", 3), ("lot_weak", 3)):
            ranked = sorted((r for r in card_rows if r["verdict"] == verdict), key=lambda r: r.get("price") or 1e9)
            for i, r in enumerate(ranked):
                r["rank"] = i + 1
                if i >= keep:
                    r["hidden_from"] = verdict
                    r["verdict"] = "more"
    if client is not None:
        save_json(pstate("vinted_debug.json"), {"domain": domain, "mode": client.mode, "attempts": client.attempts[-60:]})
        log("Méthode Vinted utilisée : %s" % (client.mode or "aucune"))
    rows.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], r.get("deal_ratio") or 9))
    fresh = [r for r in rows if r["verdict"] == "match" and r["new"]]
    if not fixtures:
        for r in rows:
            seen[r["listing_id"]] = r["verdict"]
        save_json(seen_path, seen)

    stats = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICT_ORDER}
    log("%d annonces · %d meilleures offres (max %d par carte) · %d à vérifier · %d lots confirmés · %d lots incertains · %d autres · %d rejetées · %d nouvelles" % (
        len(rows), stats["match"], top_k, stats["review"], stats["lot"], stats["lot_weak"], stats["more"], stats["rejected"], len(fresh)))
    if fresh and send_alerts and not fixtures:
        msg = "\n\n".join("%s\n%.0f € (%s)\n%s" % (r["query_card"], r["price"] or 0, r["title"][:60], r["url"]) for r in fresh[:10])
        if telegram("Nouvelles annonces Vinted\n\n" + msg):
            log("Telegram alert sent.")
    return rows


def run(args) -> int:
    ensure_profiles()
    cards = load_json(args.wishlist or pfile("wishlist.json"), {"cards": []})["cards"]
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
    ap.add_argument("--wishlist", default="", help="default: wishlist of the active profile")
    ap.add_argument("--domain", default="fr", help="fr, pt, es, be, it, de ...")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--fixtures", action="store_true", help="offline test data")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--recheck", action="store_true", help="re-run AI on already seen listings")
    ap.add_argument("--show-rejected", action="store_true", help="include rejected rows in the HTML")
    sys.exit(run(ap.parse_args()))
