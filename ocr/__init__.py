"""Free screenshot reading with macOS's built-in OCR (Vision framework), no API key.

The Swift helper is compiled once into state/vision_ocr (needs the Xcode Command Line Tools,
which macOS installs with `xcode-select --install`). Then we look for collector numbers like
"199/165" and the card name next to them, and match them against the TCGdex catalogue.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from common import STATE_DIR, norm

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "vision_ocr.swift")
BIN = os.path.join(STATE_DIR, "vision_ocr")

NUM_RE = re.compile(r"(?<![\d/])(?:N[°o]\s*)?0*(\d{1,3}[a-zA-Z]?)\s*/\s*0*(\d{1,3})(?![\d/])")
NOISE = re.compile(r"(€|eur\b|prix|price|total|valeur|qt[eé]|x\d+\b|%|\d{1,2}:\d{2})", re.I)


def available() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "La lecture gratuite utilise l'OCR de macOS : elle ne marche que sur Mac."
    if os.path.exists(BIN) or shutil.which("swiftc"):
        return True, ""
    return False, "Installe les outils développeur d'Apple (Terminal : xcode-select --install), puis réessaie."


def _compile() -> None:
    if os.path.exists(BIN) and os.path.getmtime(BIN) >= os.path.getmtime(SRC):
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    r = subprocess.run(["swiftc", "-O", SRC, "-o", BIN], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("Compilation de l'OCR impossible : " + (r.stderr or r.stdout)[-400:])


def read_lines(image_path: str) -> list[dict]:
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    _compile()
    r = subprocess.run([BIN, image_path], capture_output=True, text=True, timeout=120)
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError("OCR : réponse illisible " + (r.stderr or r.stdout)[-200:])
    if "error" in data:
        raise RuntimeError("OCR : " + data["error"])
    return data.get("lines", [])


def _clean_name(t: str) -> str:
    t = NUM_RE.sub(" ", t)
    t = re.sub(r"[|•·#\[\]()]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -:,.")
    return t


def _looks_like_name(t: str) -> bool:
    t = t.strip()
    return (2 < len(t) < 45 and sum(ch.isalpha() for ch in t) >= 3 and not NOISE.search(t)
            and not re.fullmatch(r"[\d\s/.,-]+", t))


def parse(lines: list[dict]) -> list[dict]:
    """Turn OCR lines into detections {name, number, set_total, raw}."""
    for ln in lines:
        ln["cx"] = ln["x"] + ln["w"] / 2
        ln["cy"] = ln["y"] + ln["h"] / 2
    detections, used = [], set()
    for i, ln in enumerate(lines):
        for m in NUM_RE.finditer(ln["text"]):
            number, total = m.group(1), m.group(2)
            if int(total) < 10 or int(re.sub(r"\D", "", number)) > 400:  # "1/2", dates, etc.
                continue
            name = _clean_name(ln["text"][:m.start()]) or _clean_name(ln["text"][m.end():])
            if not _looks_like_name(name):
                # nearest name-like line above (Vision y grows upwards) or beside, same column
                best, best_d = None, 9.0
                for j, o in enumerate(lines):
                    if j == i or not _looks_like_name(_clean_name(o["text"])) or NUM_RE.search(o["text"]):
                        continue
                    dy = o["cy"] - ln["cy"]          # >0 means above
                    dx = abs(o["cx"] - ln["cx"])
                    if -0.02 <= dy <= 0.12 and dx <= max(o["w"], ln["w"], 0.15):
                        d = dy * 2 + dx
                        if d < best_d:
                            best, best_d = j, d
                name = _clean_name(lines[best]["text"]) if best is not None else ""
                if best is not None:
                    used.add(best)
            used.add(i)
            detections.append({"name": name, "number": number, "set_total": total,
                               "set_name": None, "language": None, "raw": ln["text"]})
    if not detections:  # no numbers at all: offer name-only guesses
        for ln in lines:
            t = _clean_name(ln["text"])
            if _looks_like_name(t) and len(detections) < 25:
                detections.append({"name": t, "number": None, "set_total": None,
                                   "set_name": None, "language": None, "raw": ln["text"]})
    # de-duplicate
    seen, out = set(), []
    for d in detections:
        k = (norm(d["name"]), d["number"], d["set_total"])
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out
