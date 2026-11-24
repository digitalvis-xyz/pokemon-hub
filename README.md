# Pokémon Hub

Hub for Pokémon TCG collectors: Vinted deals matched to your wishlist, online shop pre-orders, restocks and price drops — in one local app.

## Install (once)

```bash
git clone https://github.com/digitalvis-xyz/pokemon-hub.git
cd pokemon-hub
python3 app.py
```

## Update

```bash
git pull
```
Then restart `python3 app.py` (Ctrl+C, then run it again). Your wishlist, shops and settings are kept.

## Start the app

```bash
cd pokemon-hub
python3 app.py
```
Your browser opens at http://127.0.0.1:8765. Leave the Terminal window open while you use the app, and press Ctrl+C there to stop it.
You can also double-click **`start.command`**. The first time, macOS may block it: right-click it, choose *Open*, then confirm.

The app runs on **real data**. Your profiles (`data/profiles/<name>/`: wishlist, shops, results, notifications) and settings live in the `data/` folder: it is never sent to GitHub, so `git pull` never touches it. The `wishlist.json` and `shops.json` at the root are only examples. On first start, anything real you had in them is copied into `data/`; the untouched example cards and shops are left out.
To see the app with test data, tick *Mode démo* in Réglages. *Effacer les données de démo* removes every trace of it, including demo notifications.

| Tab | What it does |
|---|---|
| Bons plans | Vinted listings matched to your wishlist, with photo, price vs. your max, language, and why a listing was rejected |
| Boutiques | Pre-orders, restocks and price drops, the full Pokémon inventory of each shop. Add a shop by pasting its address, or with one click from the **Portugal directory** (`shop_directory.json`, pre-filled from a web search). *Tout détecter* checks which shops are supported. |
| Wishlist | Add cards from the open-source TCGdex catalogue (search by name, filter by set, "secret rares only"), or **import a screenshot** (Pokécardex list, binder, other app): Claude reads the cards and matches them to the catalogue for you to confirm. Then edit languages and max price. |
| Carte | Map of your shops and the Portugal directory (OpenStreetMap, free). Shops are placed from their address, or their city when there's none. |
| Sorties | Upcoming releases (FR / EN / JP) with status and source, from `releases.json`, which is updated with the app. You can export them as `.ics` or subscribe from Calendar: `http://127.0.0.1:8765/calendar.ics`. |
| Réglages | **Profile** (name, city, Vinted country, card languages; one wishlist and shop list per profile; switch profile at the top), **Diagnostic** (tests the catalogue, Vinted and every shop, with a report to copy), demo mode, Claude API key, Telegram alerts |

The **bell** (top right) is the notification centre: new Vinted matches, pre-orders, restocks and price drops, each with a link. Turn on *Notifications du Mac* in Réglages to also get system notifications while the page is open.

**Screenshot import** is free by default: it uses the OCR built into macOS (Apple Vision). A small Swift helper is compiled the first time you use it, which takes about a minute and needs the Command Line Tools: `xcode-select --install`. It reads collector numbers like `199/165` and the names next to them, then matches them in the catalogue. With an Anthropic API key in Réglages, Claude reads the screenshot instead: it's better on photos and cluttered layouts, but it's paid. The catalogue never needs a key.

*Auto* (top right) re-runs everything every 15, 30 or 60 minutes while the page is open.

---

The command-line scripts below still work on their own.

```
wishlist.json      your wanted cards (canonical id + names + number + max price)
shops.json         the online shops you follow
vinted_search.py   prototype 1: Vinted × wishlist, with rules + optional Claude check
shops_watch.py     prototype 2: new products, pre-orders, restocks, price drops
classify.py        maps a listing back to a card id (rules, then Claude)
results/           CSV + HTML reports (open the .html in your browser)
state/             what was already seen, so later runs only report what's new
fixtures/          fake data for offline tests
```

## 1. Try it offline first (30 seconds)

```bash
cd pokemon-hub
python3 vinted_search.py --fixtures
python3 shops_watch.py --fixtures --run 1   # baseline
python3 shops_watch.py --fixtures --run 2   # shows NEW_PREORDER, RESTOCK, PRICE_DROP…
open results/*.html
```

## 2. Vinted for real

1. Put your own cards in `wishlist.json`. The `id` can be anything stable. TCGdex ids (`sv03.5-199`) are a good choice because they'll be reused for prices later.
2. Run:
   ```bash
   python3 vinted_search.py              # vinted.fr
   python3 vinted_search.py --domain pt  # vinted.pt
   ```
3. Open the HTML report. Green rows are matches, yellow rows need a look. Add `--show-rejected` to see what was filtered out and why (the `rule_reject` column).

**What happens:** each card generates 2–3 search phrasings (FR name + number, EN name + number, set code). Every result then passes through rules: the number must be present, the price must be under `max_price`, and proxy/custom/fake words, lots, wrong languages and graded cards (unless allowed) are rejected.

**Optional Claude check** (looks at the title and the photo; it costs well under a cent per listing and only runs on new candidates):
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 vinted_search.py
```

## 3. Shops for real

1. Check each shop you care about:
   ```bash
   python3 shops_watch.py --detect https://some-shop.fr
   ```
   `shopify` or `woocommerce` means the shop works with no scraping. `unsupported` means it needs an RSS or HTML module later.
2. Add the supported shops to `shops.json`, then run `python3 shops_watch.py`.
   The first run saves a baseline. After that, each run reports only the changes.

## 4. Alerts on your phone (optional)

Create a bot with @BotFather on Telegram, send it a message, then get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```
Both scripts send an alert only for new matches, new pre-orders, restocks and price drops.

## 5. Run it automatically

`crontab -e`, then for example:
```
*/20 * * * * cd ~/tcg-hub-proto && /usr/bin/python3 vinted_search.py >> run.log 2>&1
*/30 * * * * cd ~/tcg-hub-proto && /usr/bin/python3 shops_watch.py   >> run.log 2>&1
```
(cron doesn't read your shell profile, so put the `export` lines at the top of the crontab.)

## Troubleshooting

- `CERTIFICATE_VERIFY_FAILED`: the app now fixes this automatically by using macOS's own certificates. If it still happens, run *Applications → Python 3.x → Install Certificates.command* once.
- Vinted `HTTP 403`, `401` or an empty `items` list: Vinted's anti-bot protection kicked in. Wait, lower the frequency (`--delay 6`), or switch networks. The endpoint is undocumented and can change. All the Vinted code lives in the `Vinted` class, so it can be swapped for a paid scraper without touching the rest.
- Too many false positives: raise the thresholds in `vinted_search.py` (`rule_score >= 55` = match) or add words to `BLOCK_WORDS` in `classify.py`.

## Limits

Vinted has no public API, so this uses the same catalog endpoint as the website. Keep it to personal use and low frequency (it goes against Vinted's terms). The shop endpoints are public features of Shopify and WooCommerce.

## Vinted (since September 2026)

Vinted retired its old search address (`/api/v2/catalog/items`). The app now:
1. opens an anonymous session on `www.vinted.<country>` (the `access_token_web` cookie, `X-Anon-Id` and the CSRF token),
2. searches `api.vinted.<country>/svc-catalogue/items`, dropping any filter the server rejects with a 400 error,
3. if the API still refuses, falls back to reading the website's results page.
*Réglages → Diagnostic* shows every step with its HTTP code.

## Vinted results, kept readable

- **Meilleures offres**: the N cheapest correct listings per card (single cards only, with the exact collector number). N is set in Réglages, 3 by default.
- **Lots**: group sales that contain the card, shown separately.
- **À vérifier** and **Autres offres**: the rest, one click away.
- *Choisir les cartes…* searches only a selection; the results for the other cards are kept. **Arrêter** in the status bar stops a running search.
- Queries: one per language you want the card in (French name, English name) + the collector number. Vinted matches the seller's title as written, so a French title only matches the French name; the number is the same in every language.

## Shops

*Ajouter une boutique* takes a name, a website or an address. The site's own data, OpenStreetMap (free) or Google Places (optional key in Réglages) fill in the rest. Shops whose stock can't be tracked go to the map-only list.

## Card shows

`events.json` lists Portugal's card shows (buy / sell / trade, no competition). They appear in *Sorties → Card shows*, on the map (purple) and in `calendar.ics`.
