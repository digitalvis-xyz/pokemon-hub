#!/usr/bin/env python3
"""TCG Hub - local web app.

    python3 app.py            -> opens http://127.0.0.1:8765 in your browser
    python3 app.py --port 9000 --no-browser

Only listens on your own machine (127.0.0.1). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64

import agenda
import catalog
import compare
import geo
import notifications
import ocr
import shops_watch
import vinted_search
from common import (ROOT, STATE_DIR, Http, active_profile, create_profile, data_path, ensure_profiles,
                    get_profile, list_profiles, load_json, pfile, pstate, save_json, save_profile,
                    set_active_profile)
from notify import telegram
from urllib.parse import parse_qs, urlparse

SETTINGS_PATH = data_path("settings.json")


def WISHLIST_PATH() -> str:
    return pfile("wishlist.json")


def SHOPS_PATH() -> str:
    return pfile("shops.json")
DIRECTORY_PATH = os.path.join(ROOT, "shop_directory.json")
RELEASES_PATH = os.path.join(ROOT, "releases.json")
EVENTS_PATH = os.path.join(ROOT, "events.json")
STATIC = os.path.join(ROOT, "static")

DEFAULT_SETTINGS = {
    "demo_mode": False,
    "vinted_domain": "fr",
    "vinted_delay": 3.0,
    "use_ai": True,
    "screenshot_engine": "auto",
    "vinted_top_k": 3,          # best offers kept per card
    "vinted_desc_checks": 6,    # listings per card whose description is read (0 = off)
    "google_places_key": "",    # optional: better shop autofill  # auto = Claude if an API key is set, else free macOS OCR
    "anthropic_api_key": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}
SECRET_KEYS = ("anthropic_api_key", "telegram_bot_token", "google_places_key")
ENV_MAP = {"anthropic_api_key": "ANTHROPIC_API_KEY", "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
           "telegram_chat_id": "TELEGRAM_CHAT_ID"}


# ---------- settings ----------

def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(load_json(SETTINGS_PATH, {}))
    return s


def apply_env(s: dict) -> None:
    """Settings saved in the app win; otherwise keys exported in your shell still work."""
    for k, env in ENV_MAP.items():
        if s.get(k):
            os.environ[env] = s[k]


def public_settings(s: dict) -> dict:
    out = dict(s)
    for k in SECRET_KEYS:
        v = s.get(k) or ""
        out[k] = ("••••" + v[-4:]) if v else ""
        out[k + "_set"] = bool(v)
    return out


# ---------- background jobs ----------

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.kind = None
        self.log: list[str] = []
        self.started = None
        self.finished = None
        self.error = None
        self._stop = threading.Event()

    def say(self, line: str) -> None:
        with self.lock:
            self.log.append(line)
            self.log = self.log[-400:]

    def stop(self) -> bool:
        with self.lock:
            if not self.running:
                return False
            self._stop.set()
            self.log.append("Arrêt demandé…")
            return True

    def stopping(self) -> bool:
        return self._stop.is_set()

    def start(self, kind: str, fn, params: dict | None = None) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.kind, self.log, self.error = True, kind, [], None
            self._stop = threading.Event()
            self.started, self.finished = time.time(), None

        def target():
            try:
                fn(self.say, params or {}, self.stopping)
            except Exception as e:
                self.error = str(e)
                self.say("ERROR: %s" % e)
                traceback.print_exc()
            finally:
                with self.lock:
                    self.running = False
                    self.finished = time.time()

        threading.Thread(target=target, daemon=True).start()
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "kind": self.kind, "log": list(self.log),
                    "started": self.started, "finished": self.finished, "error": self.error}


JOB = Job()


def purge_demo() -> None:
    """Remove every trace of demo data: results, fixture state, demo notifications."""
    paths = [os.path.join(STATE_DIR, "shops_fixture_state.json")]
    for prof in list_profiles():
        paths += [pstate("last_vinted_demo.json", prof["id"]), pstate("last_shops_demo.json", prof["id"])]
        notifications.purge_demo(prof["id"])
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def results_path(kind: str, demo: bool) -> str:
    return pstate("last_%s%s.json" % (kind, "_demo" if demo else ""))


def job_vinted(say, params=None, stop=None) -> None:
    s = get_settings()
    apply_env(s)
    demo = bool(s["demo_mode"])
    cards = load_json(WISHLIST_PATH(), {"cards": []})["cards"]
    owned = [c for c in cards if c.get("owned")]
    cards = [c for c in cards if not c.get("owned")]
    if owned:
        say("%d carte(s) marquée(s) « Je l'ai » ignorée(s)" % len(owned))
    ids = set((params or {}).get("ids") or [])
    if ids:
        cards = [c for c in cards if c["id"] in ids]
        say("Recherche limitée à %d carte(s) sélectionnée(s)" % len(cards))
    domain = get_profile().get("vinted_domain") or s["vinted_domain"]
    rows = vinted_search.search(cards, domain=domain, fixtures=demo, use_ai=bool(s["use_ai"]),
                                delay=float(s["vinted_delay"]), log=say, stop=stop,
                                top_k=int(s.get("vinted_top_k") or 3),
                                desc_checks=int(s.get("vinted_desc_checks", 6)))
    if ids:  # partial search: keep previous results for the other cards
        prev = load_json(results_path("vinted", demo), {}).get("rows", [])
        rows = [r for r in prev if r.get("card_id") not in ids] + rows
    save_json(results_path("vinted", demo), {"at": time.time(), "demo": demo, "domain": domain, "rows": rows})
    n = notifications.add(notifications.from_vinted(rows, demo))
    if n:
        say("%d nouvelle(s) notification(s)" % n)


def job_shops(say, params=None, stop=None) -> None:
    s = get_settings()
    apply_env(s)
    demo = bool(s["demo_mode"])
    if demo:
        cfg = load_json(os.path.join(ROOT, "fixtures", "shops_fixture.json"), {})
        save_json(os.path.join(STATE_DIR, "shops_fixture_state.json"), {})
        say("Demo: first pass (baseline)…")
        shops_watch.watch(cfg, fixtures=True, run_no=1, log=say)
        say("Demo: second pass (what changed)…")
        res = shops_watch.watch(cfg, fixtures=True, run_no=2, log=say)
    else:
        cfg = load_json(SHOPS_PATH(), {"shops": []})
        res = shops_watch.watch(cfg, log=say, stop=stop)
        prev = load_json(results_path("shops", False), {})
        # keep a short history of events across runs so they don't vanish on the next quiet run
        history = (res["events"] + prev.get("history", []))[:300]
        res["history"] = history
    res["history"] = res.get("history") or res["events"]
    say("%d changes · %d Pokémon products in total" % (len(res["events"]), len(res["inventory"])))
    save_json(results_path("shops", demo), dict(res, at=time.time(), demo=demo))
    n = notifications.add(notifications.from_shops(res["events"], demo))
    if n:
        say("%d nouvelle(s) notification(s)" % n)


def job_detect(say, params=None, stop=None) -> None:
    data = load_json(SHOPS_PATH(), {"keywords": ["pokemon", "pokémon"], "shops": []})
    http = Http()
    for shop in data.get("shops", []):
        engine = shops_watch.detect(http, shop["url"].rstrip("/"))
        shop["engine"] = engine
        say("  %-26s %s" % (shop["name"][:26], engine))
    save_json(SHOPS_PATH(), data)
    ok = sum(1 for s in data["shops"] if s["engine"] in ("shopify", "woocommerce"))
    say("%d/%d boutiques compatibles" % (ok, len(data["shops"])))


def job_all(say, params=None, stop=None) -> None:
    say("== Vinted ==")
    job_vinted(say, params, stop)
    if stop and stop():
        return
    say("== Boutiques ==")
    job_shops(say, params, stop)


def job_agenda(say, params=None, stop=None) -> None:
    say("Lecture des sources de l'agenda…")
    r = agenda.refresh_sources(log=say)
    say("Terminé : %d card show(s) et %d sortie(s) ajouté(s)" % (r["added"]["show"], r["added"]["release"]))


def job_diag(say, params=None, stop=None) -> None:
    """Test every external dependency and keep a report you can copy and send."""
    import platform
    rep = {"at": time.time(), "python": platform.python_version(), "os": platform.platform(), "checks": []}

    def add(name, ok, detail):
        rep["checks"].append({"name": name, "ok": ok, "detail": detail})
        say("%s %s : %s" % ("OK " if ok else "KO ", name, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:300]))

    try:
        n = len(catalog.sets("fr"))
        add("Catalogue TCGdex", n > 0, "%d extensions" % n)
    except Exception as e:
        add("Catalogue TCGdex", False, str(e)[:300])
    prof = get_profile()
    domain = prof.get("vinted_domain") or get_settings().get("vinted_domain", "fr")
    v = vinted_search.Vinted(domain)
    try:
        items = v.search("Pikachu", per_page=10)
        add("Vinted (vinted.%s)" % domain, bool(items), {"méthode": v.mode, "annonces": len(items), "étapes": v.attempts})
    except Exception as e:
        add("Vinted (vinted.%s)" % domain, False, {"erreur": str(e)[:200], "étapes": v.attempts})
    http = Http()
    for sh in load_json(SHOPS_PATH(), {"shops": []}).get("shops", []):
        engine, notes = shops_watch.detect_verbose(http, sh["url"].rstrip("/"))
        add("Boutique %s" % sh["name"], engine in shops_watch.SUPPORTED, {"moteur": engine, "tests": notes})
    if not load_json(SHOPS_PATH(), {"shops": []}).get("shops"):
        add("Boutiques", False, "aucune boutique suivie dans ce profil")
    save_json(pstate("diagnostic.json"), rep)
    say("Diagnostic terminé : %d/%d OK" % (sum(c["ok"] for c in rep["checks"]), len(rep["checks"])))


JOBS = {"vinted": job_vinted, "shops": job_shops, "all": job_all, "detect": job_detect, "diag": job_diag, "agenda": job_agenda}


def all_events() -> dict:
    base = load_json(EVENTS_PATH, {"events": []})
    u = agenda.user_data()
    evs = [dict(e, origin="curated") for e in base.get("events", [])] + u["events"]
    hidden = set(u.get("hidden", []))
    return {"updated": base.get("updated"), "events": [e for e in evs if agenda._key(e) not in hidden],
            "sources": u["sources"], "last_refresh": u.get("last_refresh")}


def all_releases() -> dict:
    base = load_json(RELEASES_PATH, {"releases": []})
    u = agenda.user_data()
    rels = [dict(r, origin="curated") for r in base.get("releases", [])] + u["releases"]
    hidden = set(u.get("hidden", []))
    return dict(base, releases=[r for r in rels if agenda._key(r) not in hidden], sources=u["sources"],
                last_refresh=u.get("last_refresh"))


def dedupe_wishlist() -> list[dict]:
    """Return the wishlist without duplicate card ids (and fix the file if needed)."""
    data = load_json(WISHLIST_PATH(), {"cards": []})
    seen_ids, uniq = set(), []
    for c in data.get("cards", []):
        if c.get("id") and c["id"] in seen_ids:
            continue
        seen_ids.add(c.get("id"))
        uniq.append(c)
    if len(uniq) != len(data.get("cards", [])):
        data["cards"] = uniq
        save_json(WISHLIST_PATH(), data)
    return uniq


def ics(releases: list[dict]) -> str:
    """iCalendar feed of releases: subscribe to http://127.0.0.1:8765/calendar.ics or import it."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TCG Hub//Sorties Pokemon//FR", "X-WR-CALNAME:Sorties Pokémon TCG"]
    for r in releases:
        d = r.get("date", "")
        if len(d) != 10:
            continue  # month-only dates can't go in a calendar
        start = d.replace("-", "")
        end_d = r.get("date_end") or d
        import datetime as _dt
        end = (_dt.date.fromisoformat(end_d) + _dt.timedelta(days=1)).strftime("%Y%m%d")
        import hashlib as _h
        uid = "%s-%s@tcghub" % (start, _h.md5(r.get("title", "").encode("utf-8")).hexdigest()[:10])
        summ = "%s [%s]%s" % (r.get("title", ""), r.get("region", ""), "" if r.get("status") == "officiel" else " (" + r.get("status", "") + ")")
        desc = (r.get("products") or "") + ("\\nSource : " + r["source"] if r.get("source") else "")
        lines += ["BEGIN:VEVENT", "UID:" + uid, "DTSTART;VALUE=DATE:" + start, "DTEND;VALUE=DATE:" + end,
                  "SUMMARY:" + summ.replace(",", "\\,"), "DESCRIPTION:" + desc.replace(",", "\\,"), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def shop_name_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    base = host.split(".")[0]
    return " ".join(w.capitalize() for w in base.replace("_", "-").split("-")) or host


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "TCGHub/0.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html")
        if path == "/api/state":
            s = get_settings()
            demo = bool(s["demo_mode"])
            return self._send(200, {
                "settings": public_settings(s),
                "wishlist": dedupe_wishlist(),
                "shops": load_json(SHOPS_PATH(), {"shops": []}).get("shops", []),
                "vinted": load_json(results_path("vinted", demo), {}),
                "shops_result": load_json(results_path("shops", demo), {}),
                "job": JOB.snapshot(),
                "unread": notifications.list_all()["unread"],
                "profile": get_profile(),
                "profiles": list_profiles(),
            })
        if path == "/api/job":
            return self._send(200, JOB.snapshot())
        qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if path == "/api/catalog/sets":
                return self._send(200, {"sets": catalog.sets(qs.get("lang", "fr"))})
            if path == "/api/catalog/search":
                return self._send(200, catalog.search(
                    q=qs.get("q", ""), set_id=qs.get("set", ""), lang=qs.get("lang", "fr"),
                    page=int(qs.get("page", 1)), secret_only=qs.get("secret") == "1"))
        except Exception as e:
            return self._send(502, {"error": "Catalogue TCGdex injoignable : %s" % str(e)[:160]})
        if path == "/api/events":
            return self._send(200, all_events())
        if path == "/api/releases":
            return self._send(200, all_releases())
        if path == "/calendar.ics":
            evs = [dict(e, region="PT", status="officiel", kind="card show",
                        products=" · ".join(x for x in (e.get("venue"), e.get("address") or e.get("city"), e.get("details")) if x),
                        source=e.get("url", "")) for e in all_events()["events"]]
            return self._send(200, ics(all_releases()["releases"] + evs).encode("utf-8"),
                              "text/calendar")
        if path == "/api/map":
            prof = get_profile()
            home = None
            if prof.get("lat") is not None:
                home = {"lat": prof["lat"], "lng": prof["lng"], "label": prof.get("city", "")}
            followed = load_json(SHOPS_PATH(), {"shops": []}).get("shops", [])
            fkeys = {(x.get("url") or x["name"]).rstrip("/").lower() for x in followed}
            pins = []
            for sh, kind in [(x, "followed") for x in followed] + \
                    [(x, "directory") for x in load_json(DIRECTORY_PATH, {"shops": []}).get("shops", [])
                     if (x.get("url") or x["name"]).rstrip("/").lower() not in fkeys]:
                q = geo.shop_query(sh)
                g = {"lat": sh["lat"], "lng": sh["lng"]} if sh.get("lat") is not None else (geo.geocode(q) if q else None)
                pins.append({"name": sh["name"], "url": sh.get("url", ""), "kind": kind, "query": q,
                             "approx": not sh.get("address") and sh.get("lat") is None, "note": sh.get("note", ""),
                             "city": sh.get("city", ""), "address": sh.get("address", ""), "engine": sh.get("engine", ""),
                             "lat": g and g["lat"], "lng": g and g["lng"]})
            today = time.strftime("%Y-%m-%d")
            for e in all_events()["events"]:
                if (e.get("date_end") or e["date"]) < today:
                    continue
                q = e.get("address") or (e.get("city", "").split("(")[0].strip() + ", Portugal")
                g = geo.geocode(q)
                pins.append({"name": e["title"], "url": e.get("url", ""), "kind": "event", "query": q,
                             "approx": not e.get("address"), "note": e.get("details", ""), "city": e.get("city", ""),
                             "address": e.get("address", ""), "date": e["date"], "date_end": e.get("date_end"),
                             "lat": g and g["lat"], "lng": g and g["lng"]})
            return self._send(200, {"home": home, "pins": pins})
        if path == "/api/diagnostic":
            return self._send(200, load_json(pstate("diagnostic.json"), {}))
        if path == "/api/notifications":
            return self._send(200, notifications.list_all())
        if path == "/api/shops/directory":
            followed = {s["url"].rstrip("/").lower() for s in load_json(SHOPS_PATH(), {"shops": []}).get("shops", [])}
            d = load_json(DIRECTORY_PATH, {"shops": []})
            for sh in d.get("shops", []):
                sh["followed"] = sh["url"].rstrip("/").lower() in followed
            return self._send(200, d)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except Exception:
            return self._send(400, {"error": "bad json"})

        if path == "/api/wishlist/owned":
            data = load_json(WISHLIST_PATH(), {"cards": []})
            for c in data.get("cards", []):
                if c["id"] == body.get("id"):
                    c["owned"] = bool(body.get("owned"))
            save_json(WISHLIST_PATH(), data)
            return self._send(200, {"ok": True})

        if path == "/api/shops/compare":
            q = (body.get("q") or "").strip()
            if len(q) < 2:
                return self._send(400, {"error": "Tape au moins 2 lettres"})
            return self._send(200, compare.compare(load_json(SHOPS_PATH(), {"shops": []}).get("shops", []), q))

        if path == "/api/agenda/add":
            kind = body.get("kind", "show")
            item = {k: body.get(k) for k in ("title", "date", "date_end", "city", "address", "venue", "details", "url",
                                             "region", "status", "image", "products")}
            if not item.get("title") or not re.match(r"^\d{4}-\d{2}(-\d{2})?$", item.get("date") or ""):
                return self._send(400, {"error": "Titre et date (AAAA-MM-JJ) obligatoires"})
            if kind == "release":
                item.setdefault("status", "annoncé")
                item["kind"] = body.get("type") or "produits"
            return self._send(200, {"item": agenda.add(kind, item)})

        if path == "/api/agenda/remove":
            if body.get("id"):
                agenda.remove(body["id"])
            elif body.get("title"):  # hide a curated item
                u = agenda.user_data()
                u["hidden"] = list(set(u.get("hidden", [])) | {agenda._key(body)})
                agenda.save_user(u)
            return self._send(200, {"ok": True})

        if path == "/api/agenda/sources":
            u = agenda.user_data()
            srcs = [x for x in body.get("sources", []) if (x.get("url") or "").startswith("http")]
            u["sources"] = srcs
            agenda.save_user(u)
            return self._send(200, {"sources": srcs})

        if path == "/api/vinted/clear":
            for name in ("last_vinted.json", "last_vinted_demo.json", "vinted_seen.json"):
                fp = pstate(name)
                if os.path.exists(fp):
                    os.remove(fp)
            notifications.purge_kind("vinted")
            return self._send(200, {"ok": True})

        if path == "/api/job/stop":
            return self._send(200, {"stopping": JOB.stop(), "job": JOB.snapshot()})

        if path.startswith("/api/run/"):
            kind = path.rsplit("/", 1)[-1]
            if kind not in JOBS:
                return self._send(404, {"error": "unknown job"})
            ok = JOB.start(kind, JOBS[kind], body)
            return self._send(200 if ok else 409, {"started": ok, "job": JOB.snapshot()})

        if path == "/api/wishlist":
            cards = body.get("cards", [])
            for c in cards:
                for k in ("id", "number"):
                    if not str(c.get(k, "")).strip():
                        return self._send(400, {"error": "every card needs an id and a number"})
            data = load_json(WISHLIST_PATH(), {})
            seen_ids, uniq = set(), []
            for c in cards:
                if c["id"] not in seen_ids:
                    seen_ids.add(c["id"])
                    uniq.append(c)
            data["cards"] = uniq
            save_json(WISHLIST_PATH(), data)
            return self._send(200, {"ok": True})

        if path == "/api/shops":
            data = load_json(SHOPS_PATH(), {"keywords": ["pokemon", "pokémon"]})
            data["shops"] = body.get("shops", [])
            save_json(SHOPS_PATH(), data)
            return self._send(200, {"ok": True})

        if path == "/api/shops/detect":
            url = (body.get("url") or "").strip().rstrip("/")
            if not url.startswith("http"):
                return self._send(400, {"error": "url must start with http(s)://"})
            try:
                return self._send(200, {"engine": shops_watch.detect(Http(), url)})
            except Exception as e:
                return self._send(200, {"engine": "error", "error": str(e)[:200]})

        if path == "/api/settings":
            s = get_settings()
            was_demo = bool(s.get("demo_mode"))
            for k in DEFAULT_SETTINGS:
                if k not in body:
                    continue
                v = body[k]
                if k in SECRET_KEYS and isinstance(v, str) and v.startswith("••••"):
                    continue  # unchanged masked value
                s[k] = v
            save_json(SETTINGS_PATH, s)
            apply_env(s)
            if was_demo and not s.get("demo_mode"):
                purge_demo()
            return self._send(200, {"ok": True, "settings": public_settings(s)})

        if path == "/api/demo/purge":
            purge_demo()
            return self._send(200, {"ok": True})

        if path == "/api/catalog/add":
            data = load_json(WISHLIST_PATH(), {"cards": []})
            have = {c["id"] for c in data.get("cards", [])}
            added, errors = [], []
            for cid in body.get("ids", [])[:50]:
                if cid in have:
                    continue
                try:
                    entry = catalog.to_wishlist_entry(cid)
                    entry["languages"] = get_profile().get("languages") or ["fr"]
                    entry.update({k: v for k, v in (body.get("defaults") or {}).items() if k in ("languages", "max_price")})
                    data.setdefault("cards", []).append(entry)
                    have.add(cid)
                    added.append(entry)
                except Exception as e:
                    errors.append("%s: %s" % (cid, str(e)[:80]))
            save_json(WISHLIST_PATH(), data)
            return self._send(200, {"added": added, "errors": errors, "cards": data["cards"]})

        if path == "/api/wishlist/screenshot":
            s = get_settings()
            apply_env(s)
            img = body.get("image", "")
            if "," in img[:100]:
                img = img.split(",", 1)[1]
            engine = s.get("screenshot_engine", "auto")
            use_claude = engine == "claude" or (engine == "auto" and os.environ.get("ANTHROPIC_API_KEY"))
            try:
                if use_claude:
                    detected = catalog.read_screenshot(img, body.get("media_type", "image/jpeg"))
                    resolver = catalog.resolve_any
                else:
                    shot = os.path.join(STATE_DIR, "last_screenshot.jpg")
                    with open(shot, "wb") as f:
                        f.write(base64.b64decode(img))
                    detected = ocr.parse(ocr.read_lines(shot))
                    resolver = catalog.resolve_any
            except Exception as e:
                return self._send(400, {"error": str(e)[:400]})
            results = []
            for d in detected[:60]:
                try:
                    results.append(resolver(d))
                except Exception as e:
                    results.append({"detected": d, "best": None, "alternatives": [], "error": str(e)[:100]})
            return self._send(200, {"results": results, "engine": "claude" if use_claude else "ocr"})

        if path == "/api/wishlist/parse-text":
            results = []
            for d in catalog.parse_text_list(body.get("text", ""))[:200]:
                try:
                    results.append(catalog.resolve_any(d))
                except Exception as e:
                    results.append({"detected": d, "best": None, "alternatives": [], "error": str(e)[:100]})
            return self._send(200, {"results": results, "engine": "texte"})

        if path == "/api/profiles":
            if not (body.get("name") or "").strip():
                return self._send(400, {"error": "Donne un nom au profil"})
            p = create_profile(body["name"], body.get("city", ""))
            set_active_profile(p["id"])
            return self._send(200, {"profile": p, "profiles": list_profiles()})

        if path == "/api/profiles/switch":
            try:
                set_active_profile(body.get("id", ""))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"profile": get_profile()})

        if path == "/api/profile":
            data = {k: body[k] for k in ("name", "city", "vinted_domain", "languages", "country") if k in body}
            if "city" in data and data["city"] != get_profile().get("city"):
                g = geo.geocode(data["city"]) if data["city"] else None
                data["lat"], data["lng"] = (g["lat"], g["lng"]) if g else (None, None)
            return self._send(200, {"profile": save_profile(data)})

        if path == "/api/notifications/read":
            notifications.mark_read(body.get("ids"))
            return self._send(200, notifications.list_all())

        if path == "/api/notifications/clear":
            notifications.clear()
            return self._send(200, notifications.list_all())

        if path == "/api/shops/lookup":
            key = get_settings().get("google_places_key", "")
            return self._send(200, {"results": geo.lookup(body.get("query", ""), key)[:8], "google": bool(key)})

        if path == "/api/shops/add":
            # add from a lookup result: website optional (map-only shop when missing)
            data = load_json(SHOPS_PATH(), {"keywords": ["pokemon", "pokémon"], "shops": []})
            shop = {k: body[k] for k in ("name", "address", "city", "lat", "lng") if body.get(k) not in (None, "")}
            url = (body.get("website") or body.get("url") or "").strip().rstrip("/")
            if url:
                p = urlparse(url if url.startswith("http") else "https://" + url)
                url = "%s://%s" % (p.scheme, p.netloc)
                if any((x.get("url") or "").rstrip("/").lower() == url.lower() for x in data.get("shops", [])):
                    return self._send(200, {"ok": True, "already": True, "shops": data["shops"]})
                try:
                    shop["engine"] = shops_watch.detect(Http(), url)
                except Exception:
                    shop["engine"] = "error"
            shop["url"] = url
            shop.setdefault("name", shop_name_from_url(url) if url else "Boutique")
            data.setdefault("shops", []).append(shop)
            save_json(SHOPS_PATH(), data)
            return self._send(200, {"ok": True, "shop": shop, "shops": data["shops"]})

        if path == "/api/shops/quick-add":
            url = (body.get("url") or "").strip()
            if url and not url.startswith("http"):
                url = "https://" + url
            p = urlparse(url)
            if not p.netloc:
                return self._send(400, {"error": "URL invalide"})
            url = "%s://%s" % (p.scheme, p.netloc)
            data = load_json(SHOPS_PATH(), {"keywords": ["pokemon", "pokémon"], "shops": []})
            if any(s["url"].rstrip("/").lower() == url.lower() for s in data.get("shops", [])):
                return self._send(200, {"ok": True, "already": True, "shops": data["shops"]})
            try:
                engine = shops_watch.detect(Http(), url)
            except Exception:
                engine = "error"
            shop = {"name": body.get("name") or shop_name_from_url(url), "url": url, "engine": engine}
            for k in ("address", "city"):
                if body.get(k):
                    shop[k] = body[k]
            data.setdefault("shops", []).append(shop)
            save_json(SHOPS_PATH(), data)
            return self._send(200, {"ok": True, "shop": shop, "shops": data["shops"]})

        if path == "/api/telegram/test":
            apply_env(get_settings())
            ok = telegram("TCG Hub: test message ✅")
            return self._send(200, {"ok": ok})

        return self._send(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    for line in ensure_profiles():
        print("Migration:", line)
    if not get_settings().get("demo_mode"):
        purge_demo()
    apply_env(get_settings())
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d" % args.port
    print("TCG Hub running on %s  (Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
