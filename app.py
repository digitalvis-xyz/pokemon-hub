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
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import catalog
import notifications
import shops_watch
import vinted_search
from common import ROOT, STATE_DIR, Http, load_json, save_json
from notify import telegram
from urllib.parse import parse_qs, urlparse

SETTINGS_PATH = os.path.join(ROOT, "settings.json")
WISHLIST_PATH = os.path.join(ROOT, "wishlist.json")
SHOPS_PATH = os.path.join(ROOT, "shops.json")
DIRECTORY_PATH = os.path.join(ROOT, "shop_directory.json")
STATIC = os.path.join(ROOT, "static")

DEFAULT_SETTINGS = {
    "demo_mode": True,
    "vinted_domain": "fr",
    "vinted_delay": 3.0,
    "use_ai": True,
    "anthropic_api_key": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}
SECRET_KEYS = ("anthropic_api_key", "telegram_bot_token")
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

    def say(self, line: str) -> None:
        with self.lock:
            self.log.append(line)
            self.log = self.log[-400:]

    def start(self, kind: str, fn) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.kind, self.log, self.error = True, kind, [], None
            self.started, self.finished = time.time(), None

        def target():
            try:
                fn(self.say)
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


def results_path(kind: str, demo: bool) -> str:
    return os.path.join(STATE_DIR, "last_%s%s.json" % (kind, "_demo" if demo else ""))


def job_vinted(say) -> None:
    s = get_settings()
    apply_env(s)
    demo = bool(s["demo_mode"])
    cards = load_json(WISHLIST_PATH, {"cards": []})["cards"]
    rows = vinted_search.search(cards, domain=s["vinted_domain"], fixtures=demo, use_ai=bool(s["use_ai"]),
                                delay=float(s["vinted_delay"]), log=say)
    save_json(results_path("vinted", demo), {"at": time.time(), "demo": demo, "domain": s["vinted_domain"], "rows": rows})
    n = notifications.add(notifications.from_vinted(rows, demo))
    if n:
        say("%d nouvelle(s) notification(s)" % n)


def job_shops(say) -> None:
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
        cfg = load_json(SHOPS_PATH, {"shops": []})
        res = shops_watch.watch(cfg, log=say)
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


def job_detect(say) -> None:
    data = load_json(SHOPS_PATH, {"keywords": ["pokemon", "pokémon"], "shops": []})
    http = Http()
    for shop in data.get("shops", []):
        engine = shops_watch.detect(http, shop["url"].rstrip("/"))
        shop["engine"] = engine
        say("  %-26s %s" % (shop["name"][:26], engine))
    save_json(SHOPS_PATH, data)
    ok = sum(1 for s in data["shops"] if s["engine"] in ("shopify", "woocommerce"))
    say("%d/%d boutiques compatibles" % (ok, len(data["shops"])))


def job_all(say) -> None:
    say("== Vinted ==")
    job_vinted(say)
    say("== Shops ==")
    job_shops(say)


JOBS = {"vinted": job_vinted, "shops": job_shops, "all": job_all, "detect": job_detect}


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
                "wishlist": load_json(WISHLIST_PATH, {"cards": []}).get("cards", []),
                "shops": load_json(SHOPS_PATH, {"shops": []}).get("shops", []),
                "vinted": load_json(results_path("vinted", demo), {}),
                "shops_result": load_json(results_path("shops", demo), {}),
                "job": JOB.snapshot(),
                "unread": notifications.list_all()["unread"],
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
        if path == "/api/notifications":
            return self._send(200, notifications.list_all())
        if path == "/api/shops/directory":
            followed = {s["url"].rstrip("/").lower() for s in load_json(SHOPS_PATH, {"shops": []}).get("shops", [])}
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

        if path.startswith("/api/run/"):
            kind = path.rsplit("/", 1)[-1]
            if kind not in JOBS:
                return self._send(404, {"error": "unknown job"})
            ok = JOB.start(kind, JOBS[kind])
            return self._send(200 if ok else 409, {"started": ok, "job": JOB.snapshot()})

        if path == "/api/wishlist":
            cards = body.get("cards", [])
            for c in cards:
                for k in ("id", "number"):
                    if not str(c.get(k, "")).strip():
                        return self._send(400, {"error": "every card needs an id and a number"})
            data = load_json(WISHLIST_PATH, {})
            data["cards"] = cards
            save_json(WISHLIST_PATH, data)
            return self._send(200, {"ok": True})

        if path == "/api/shops":
            data = load_json(SHOPS_PATH, {"keywords": ["pokemon", "pokémon"]})
            data["shops"] = body.get("shops", [])
            save_json(SHOPS_PATH, data)
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
            for k in DEFAULT_SETTINGS:
                if k not in body:
                    continue
                v = body[k]
                if k in SECRET_KEYS and isinstance(v, str) and v.startswith("••••"):
                    continue  # unchanged masked value
                s[k] = v
            save_json(SETTINGS_PATH, s)
            apply_env(s)
            return self._send(200, {"ok": True, "settings": public_settings(s)})

        if path == "/api/catalog/add":
            data = load_json(WISHLIST_PATH, {"cards": []})
            have = {c["id"] for c in data.get("cards", [])}
            added, errors = [], []
            for cid in body.get("ids", [])[:50]:
                if cid in have:
                    continue
                try:
                    entry = catalog.to_wishlist_entry(cid)
                    entry.update({k: v for k, v in (body.get("defaults") or {}).items() if k in ("languages", "max_price")})
                    data.setdefault("cards", []).append(entry)
                    have.add(cid)
                    added.append(entry)
                except Exception as e:
                    errors.append("%s: %s" % (cid, str(e)[:80]))
            save_json(WISHLIST_PATH, data)
            return self._send(200, {"added": added, "errors": errors, "cards": data["cards"]})

        if path == "/api/wishlist/screenshot":
            apply_env(get_settings())
            img = body.get("image", "")
            if "," in img[:100]:
                img = img.split(",", 1)[1]
            try:
                detected = catalog.read_screenshot(img, body.get("media_type", "image/jpeg"))
            except Exception as e:
                return self._send(400, {"error": str(e)[:300]})
            results = []
            for d in detected[:60]:
                try:
                    results.append(catalog.resolve(d))
                except Exception as e:
                    results.append({"detected": d, "best": None, "alternatives": [], "error": str(e)[:100]})
            return self._send(200, {"results": results})

        if path == "/api/notifications/read":
            notifications.mark_read(body.get("ids"))
            return self._send(200, notifications.list_all())

        if path == "/api/notifications/clear":
            notifications.clear()
            return self._send(200, notifications.list_all())

        if path == "/api/shops/quick-add":
            url = (body.get("url") or "").strip()
            if url and not url.startswith("http"):
                url = "https://" + url
            p = urlparse(url)
            if not p.netloc:
                return self._send(400, {"error": "URL invalide"})
            url = "%s://%s" % (p.scheme, p.netloc)
            data = load_json(SHOPS_PATH, {"keywords": ["pokemon", "pokémon"], "shops": []})
            if any(s["url"].rstrip("/").lower() == url.lower() for s in data.get("shops", [])):
                return self._send(200, {"ok": True, "already": True, "shops": data["shops"]})
            try:
                engine = shops_watch.detect(Http(), url)
            except Exception:
                engine = "error"
            shop = {"name": body.get("name") or shop_name_from_url(url), "url": url, "engine": engine}
            data.setdefault("shops", []).append(shop)
            save_json(SHOPS_PATH, data)
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
