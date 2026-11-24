"""Shared helpers: HTTP with cookies, JSON state files, text normalisation.
Standard library only, works on Python 3.8+ (macOS 12 default python3)."""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
RESULTS_DIR = os.path.join(ROOT, "results")
DATA_DIR = os.path.join(ROOT, "data")  # your own data (gitignored): wishlist, shops, settings


# ---------- HTTPS certificates ----------
# Python installed from python.org on macOS ships without CA certificates until you run
# "Install Certificates.command", which gives CERTIFICATE_VERIFY_FAILED. We fix it ourselves:
# 1) certifi if installed, 2) otherwise export the macOS system root certificates once.
_SSL_CTX = None


def _macos_roots_pem() -> str | None:
    path = os.path.join(STATE_DIR, "macos-roots.pem")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
        return path
    pems = []
    for kc in ("/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain"):
        try:
            out = subprocess.run(["security", "find-certificate", "-a", "-p", kc],
                                 capture_output=True, text=True, timeout=30).stdout
            pems.append(out)
        except Exception:
            pass
    pem = "\n".join(p for p in pems if "BEGIN CERTIFICATE" in p)
    if not pem:
        return None
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(pem)
    return path


def ssl_context() -> ssl.SSLContext:
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    has_default = bool((paths.cafile and os.path.exists(paths.cafile)) or
                       (paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath)))
    if not has_default or ctx.cert_store_stats().get("x509_ca", 0) == 0:
        cafile = None
        try:
            import certifi  # type: ignore
            cafile = certifi.where()
        except Exception:
            if sys.platform == "darwin":
                cafile = _macos_roots_pem()
        if cafile:
            ctx = ssl.create_default_context(cafile=cafile)
    _SSL_CTX = ctx
    return ctx

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class Http:
    """Tiny HTTP client that keeps cookies between calls (needed for Vinted)."""

    def __init__(self, user_agent: str = BROWSER_UA, timeout: int = 20):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.HTTPSHandler(context=ssl_context()))
        self.ua = user_agent
        self.timeout = timeout

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        h = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip",
        }
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        self.last_headers = {}
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                self.last_headers = dict(r.headers.items())
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self.last_headers = dict(e.headers.items()) if e.headers else {}
            body = e.read()
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
            return e.code, body.decode("utf-8", "replace")

    def cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        status, body = self.get(url, params, headers)
        if status != 200:
            raise RuntimeError("HTTP %s on %s: %s" % (status, url, body[:200]))
        return json.loads(body)


def post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s on %s: %s" % (e.code, url, e.read()[:300].decode("utf-8", "replace")))


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def norm(text: str) -> str:
    """Lowercase, strip accents, collapse spaces: 'Dracaufeu EX' -> 'dracaufeu ex'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M")


def polite_sleep(seconds: float = 2.5) -> None:
    time.sleep(seconds)


# ---------- your data (data/, never committed) ----------
# The repo's wishlist.json / shops.json are examples only. Your real data lives in data/,
# so `git pull` never conflicts with it and the examples never pollute it.
EXAMPLE_CARDS = {("sv03.5-199", 150), ("sv03.5-205", 60), ("sv08.5-161", 900)}
EXAMPLE_SHOP_HOSTS = ("boutique-exemple.fr", "loja-exemplo.pt")


def data_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def ensure_data() -> list[str]:
    """Create data/ on first run, migrating anything real from the old root files. Returns log lines."""
    log = []
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(data_path("wishlist.json")):
        legacy = load_json(os.path.join(ROOT, "wishlist.json"), {}).get("cards", [])
        cards = [c for c in legacy if (c.get("id"), c.get("max_price")) not in EXAMPLE_CARDS]
        save_json(data_path("wishlist.json"), {"cards": cards})
        log.append("wishlist: %d carte(s) reprise(s), %d exemple(s) ignoré(s)" % (len(cards), len(legacy) - len(cards)))
    if not os.path.exists(data_path("shops.json")):
        legacy = load_json(os.path.join(ROOT, "shops.json"), {})
        shops = [s for s in legacy.get("shops", []) if not any(h in s.get("url", "") for h in EXAMPLE_SHOP_HOSTS)]
        save_json(data_path("shops.json"), {"keywords": legacy.get("keywords", ["pokemon", "pokémon"]), "shops": shops})
        log.append("boutiques: %d reprise(s)" % len(shops))
    if not os.path.exists(data_path("settings.json")):
        legacy = load_json(os.path.join(ROOT, "settings.json"), {})
        legacy["demo_mode"] = False
        save_json(data_path("settings.json"), legacy)
        log.append("réglages repris, mode démo désactivé")
    return log


# ---------- profiles ----------
# Each profile has its own wishlist, shops, city and results: data/profiles/<id>/
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
_ACTIVE_FILE = os.path.join(DATA_DIR, "active_profile.txt")
_PROFILE_STATE = ("last_vinted.json", "last_shops.json", "vinted_seen.json", "shops_state.json",
                  "notifications.json", "last_vinted_demo.json", "last_shops_demo.json")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", norm(name)).strip("-")
    return s[:30] or "profil"


def active_profile() -> str:
    try:
        with open(_ACTIVE_FILE, encoding="utf-8") as f:
            pid = f.read().strip()
        if pid and os.path.isdir(os.path.join(PROFILES_DIR, pid)):
            return pid
    except OSError:
        pass
    return "default"


def set_active_profile(pid: str) -> None:
    if not os.path.isdir(os.path.join(PROFILES_DIR, pid)):
        raise ValueError("profil inconnu")
    with open(_ACTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(pid)


def profile_dir(pid: str | None = None) -> str:
    d = os.path.join(PROFILES_DIR, pid or active_profile())
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    return d


def pfile(name: str, pid: str | None = None) -> str:
    return os.path.join(profile_dir(pid), name)


def pstate(name: str, pid: str | None = None) -> str:
    return os.path.join(profile_dir(pid), "state", name)


DEFAULT_PROFILE = {"name": "Moi", "city": "", "country": "PT", "vinted_domain": "pt", "languages": ["fr", "en"]}


def get_profile(pid: str | None = None) -> dict:
    p = dict(DEFAULT_PROFILE)
    p.update(load_json(pfile("profile.json", pid), {}))
    p["id"] = pid or active_profile()
    return p


def save_profile(data: dict, pid: str | None = None) -> dict:
    p = get_profile(pid)
    for k in ("name", "city", "country", "vinted_domain", "languages", "lat", "lng"):
        if k in data:
            p[k] = data[k]
    save_json(pfile("profile.json", pid), {k: v for k, v in p.items() if k != "id"})
    return p


def list_profiles() -> list[dict]:
    os.makedirs(PROFILES_DIR, exist_ok=True)
    return [get_profile(d) for d in sorted(os.listdir(PROFILES_DIR)) if os.path.isdir(os.path.join(PROFILES_DIR, d))]


def create_profile(name: str, city: str = "") -> dict:
    base = _slug(name)
    pid, n = base, 2
    while os.path.exists(os.path.join(PROFILES_DIR, pid)):
        pid, n = "%s-%d" % (base, n), n + 1
    profile_dir(pid)
    save_json(pfile("wishlist.json", pid), {"cards": []})
    save_json(pfile("shops.json", pid), {"keywords": ["pokemon", "pokémon"], "shops": []})
    return save_profile({"name": name.strip() or "Profil", "city": city}, pid)


def ensure_profiles() -> list[str]:
    """Move the single-user data/ files into data/profiles/default/ (once)."""
    log = ensure_data()
    if os.path.isdir(os.path.join(PROFILES_DIR, "default")):
        return log
    d = profile_dir("default")
    for name in ("wishlist.json", "shops.json"):
        src = data_path(name)
        if os.path.exists(src):
            os.replace(src, os.path.join(d, name))
        elif not os.path.exists(os.path.join(d, name)):
            save_json(os.path.join(d, name), {"cards": []} if name == "wishlist.json" else {"keywords": ["pokemon", "pokémon"], "shops": []})
    for name in _PROFILE_STATE:
        src = os.path.join(STATE_DIR, name)
        if os.path.exists(src):
            os.replace(src, os.path.join(d, "state", name))
    settings = load_json(data_path("settings.json"), {})
    save_profile({"vinted_domain": settings.get("vinted_domain", "pt")}, "default")
    log.append("profil par défaut créé")
    return log
