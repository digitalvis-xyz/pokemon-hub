"""CSV + a self-contained HTML page you can open in your browser."""
from __future__ import annotations

import csv
import html
import os

from common import RESULTS_DIR


def write_csv(rows: list[dict], name: str, columns: list[str]) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, name + ".csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (", ".join(v) if isinstance(v, list) else v) for k, v in r.items()})
    return path


def write_html(rows: list[dict], name: str, title: str, columns: list[str], image_key: str | None = None,
               link_key: str | None = None) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, name + ".html")
    e = html.escape
    head = "".join("<th>%s</th>" % e(c) for c in columns)
    body = []
    for r in rows:
        cls = "ok" if r.get("verdict") == "match" else ("maybe" if r.get("verdict") == "review" else "")
        cells = []
        if image_key is not None:
            img = r.get(image_key)
            cells.append("<td>%s</td>" % ('<img src="%s" loading="lazy">' % e(img) if img else ""))
        for c in columns:
            v = r.get(c, "")
            v = ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
            if c == "title" and link_key and r.get(link_key):
                v = '<a href="%s" target="_blank">%s</a>' % (e(r[link_key]), e(v))
            else:
                v = e(v)
            cells.append("<td>%s</td>" % v)
        body.append('<tr class="%s">%s</tr>' % (cls, "".join(cells)))
    img_head = "<th></th>" if image_key is not None else ""
    page = """<!doctype html><meta charset="utf-8"><title>%s</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:24px;color:#222}
h1{font-size:20px;font-weight:600} p{color:#666}
table{border-collapse:collapse;width:100%%;font-size:13px}
th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#fafafa}
tr.ok{background:#eefaf2} tr.maybe{background:#fff8e6}
img{width:72px;border-radius:4px} a{color:#0a58ca}
</style>
<h1>%s</h1><p>%d rows · green = match, yellow = to review</p>
<table><thead><tr>%s%s</tr></thead><tbody>%s</tbody></table>""" % (
        e(title), e(title), len(rows), img_head, head, "\n".join(body))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path
