"""Shared helpers: HTTP with cookies, JSON state files, text normalisation.
Standard library only, works on Python 3.8+ (macOS 12 default python3)."""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
RESULTS_DIR = os.path.join(ROOT, "results")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class Http:
    """Tiny HTTP client that keeps cookies between calls (needed for Vinted)."""

    def __init__(self, user_agent: str = BROWSER_UA, timeout: int = 20):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
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
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
            return e.code, body.decode("utf-8", "replace")

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        status, body = self.get(url, params, headers)
        if status != 200:
            raise RuntimeError("HTTP %s on %s: %s" % (status, url, body[:200]))
        return json.loads(body)


def post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
