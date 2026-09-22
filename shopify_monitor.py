#!/usr/bin/env python3
"""
Shopify-monitori: seuraa kauppojen /products.json -listaa ja postaa
uudet tuotteet Discordin webhookiin.

Asennus:       pip install requests
Jatkuva ajo:   DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py
Yksi kierros:  DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py --once
Testiajo:      python3 shopify_monitor.py --once --dry-run   (ei postaa Discordiin)
"""

import argparse
import json
import os
import pathlib
import sys
import time

import requests

# Kaupat joita seurataan: osoite (ilman kauttaviivaa lopussa) -> valuuttamerkki
STORES = {
    "https://avellalane.com": "$",
    "https://padrecca.com": "$",
    "https://sentrafashion.com": "$",
    "https://cielena.com": "$",
    "https://blankspaces.us": "$",
    "https://distrikdofficial.com": "$",
    "https://allicient.co": "$",
    "https://officialsoreva.com": "$",
    "https://shoplunnessa.com": "$",
    "https://www.curvite.com": "$",
    "https://rovak.store": "$",
    "https://cortezstudios.co": "$",
    "https://shopurbanthread.com": "$",
    "https://shopvelour.co": "$",
    "https://maisonveya.com": "$",
    "https://modo-clothing.myshopify.com": "$",
    "https://vosseraofficial.com": "$",
    "https://www.ashnon.com": "$",
    "https://tryoceans.com": "$",
    "https://alirausa.com": "$",
    "https://pilocea.com": "$",
    "https://airpuply.com": "$",
    "https://shopsakura.store": "$",
    "https://wearseventyone.net": "$",
    "https://avelowear.com": "$",
    "https://pristinelosangeles.com": "$",
    "https://ashfordhills.com": "$",
    "https://pinkpookie.com": "$",
    "https://lovedline.com": "$",
    "https://namico.store": "$",
    "https://theivorylane.net": "$",
    "https://mylumeras.com": "$",
    "https://elvani.net": "$",
    "https://sentralofficial.net": "$",
    "https://peachyhaven.com": "$",
    "https://renveroapparel.com": "$",
    "https://silvae.store": "$",
    "https://daisiesstore.shop": "$",
    "https://aurorathelabel.shop": "$",
    "https://shopaluma.com": "$",
    "https://nivorawear.com": "$",
    "https://shopvolara.com": "$",
    "https://exaluna.com": "$",
    "https://shopnovyrena.com": "$",
    "https://stryxaco.com": "$",
    "https://vaylae.com": "$",
    "https://evonnashop.com": "$",
    "https://buy.fayora.shop": "$",
    "https://miravaofficial.com": "$",
    "https://nuvaly.shop": "$",
    "https://veyfina.com": "$",
    "https://urbaneofficial.shop": "$",
    "https://lumenclothingco.shop": "$",
    "https://novirausa.com": "$",
    "https://clemoreclothing.shop": "$",
    "https://balmay.co": "$",
    "https://revoraco.com": "$",
    "https://cevorashop.com": "$",
    "https://myraco.shop": "$",
    "https://rosavelle.online": "$",
    "https://rosinsstore.myshopify.com": "$",
    "https://maverostyle.com": "$",
    "https://orvanemain.com": "$",
    "https://trendlinemain.com": "$",
    "https://autumnandash.co": "$",
    "https://coralyneshop.com": "$",
    "https://lyrx.shop": "$",
    "https://shopcrome.com": "$",
    "https://alori.store": "$",
    "https://lunveraco.com": "$",
    "https://get.currentsco.com": "$",
    "https://trinkettown.store": "$",
    "https://dorchellla.com": "$",
    "https://kairovaglobal.com": "$",
    "https://cavessi.store": "$",
    "https://astrapparels.com": "$",
    "https://coralyneshop.com": "$",
}

INTERVAL = 60                      # sekuntia kierrosten välillä (vain jatkuvassa ajossa)
STATE = pathlib.Path("seen.json")  # muistaa jo nähdyt tuotteet
HEADERS = {"User-Agent": "Mozilla/5.0 (product monitor)"}
TIMEOUT = 15                       # sekuntia yhtä HTTP-pyyntöä kohti
PAGE_SIZE = 250
MAX_PAGES = 20                     # turvaraja, ettei sivutus jää jumiin


def get_webhook():
    """Webhook luetaan aina ympäristömuuttujasta - ei koskaan kovakoodattuna."""
    try:
        return os.environ["DISCORD_WEBHOOK"]
    except KeyError:
        sys.exit("DISCORD_WEBHOOK puuttuu ymparistomuuttujista "
                 "(tai aja --dry-run-tilassa).")


def load_seen():
    if not STATE.exists():
        return {}
    try:
        data = json.loads(STATE.read_text())
    except json.JSONDecodeError as e:
        print(f"[varoitus] {STATE} ei ole validia JSONia ({e}), aloitetaan tyhjasta")
        return {}
    if not isinstance(data, dict):
        print(f"[varoitus] {STATE} ei ole objekti, aloitetaan tyhjasta")
        return {}
    return {k: set(map(str, v)) for k, v in data.items()}


def save_seen(seen):
    # Vain seurannassa olevat kaupat talletetaan, jotta tila ei jaa roikkumaan
    # poistettujen kauppojen jaljilta.
    data = {k: sorted(v) for k, v in seen.items() if k in STORES}
    STATE.write_text(json.dumps(data, indent=1) + "\n")


def fetch_products(store):
    products, page = [], 1
    while page <= MAX_PAGES:
        r = requests.get(
            f"{store}/products.json",
            params={"limit": PAGE_SIZE, "page": page},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json().get("products", [])
        products += batch
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(1)
    return products


def _prices(variants, key):
    out = []
    for v in variants:
        raw = v.get(key)
        if raw in (None, "", "0.00"):
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def _fmt_money(values, currency):
    """Muotoilee hinnan vaihteluvalina kaikista varianteista."""
    if not values:
        return None
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"{currency}{lo:.2f}"
    return f"{currency}{lo:.2f} - {currency}{hi:.2f}"


def build_embed(store, p):
    currency = STORES.get(store, "$")
    variants = p.get("variants") or []

    price = _fmt_money(_prices(variants, "price"), currency) or "?"
    compare_at = _fmt_money(_prices(variants, "compare_at_price"), currency)
    in_stock = sum(1 for v in variants if v.get("available"))

    fields = [
        {"name": "Hinta", "value": price, "inline": True},
        {"name": "Variantteja", "value": str(len(variants)), "inline": True},
        {"name": "Varastossa", "value": f"{in_stock}/{len(variants)}", "inline": True},
    ]
    if compare_at:
        fields.insert(1, {"name": "Vertailuhinta", "value": compare_at, "inline": True})

    embed = {
        "title": p.get("title") or "(nimeton tuote)",
        "url": f"{store}/products/{p.get('handle', '')}",
        "color": 0x2ECC71,
        "fields": fields,
        "footer": {"text": store.replace("https://", "")},
    }
    image = (p.get("images") or [{}])[0].get("src")
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def notify(webhook, store, p, dry_run=False):
    embed = build_embed(store, p)

    if dry_run:
        vals = {f["name"]: f["value"] for f in embed["fields"]}
        print(f"  [dry-run] POST Discord: {embed['title']} | "
              + " | ".join(f"{k}: {v}" for k, v in vals.items())
              + f" | {embed['url']}")
        return

    r = requests.post(webhook, json={"embeds": [embed]}, timeout=TIMEOUT)
    if r.status_code == 429:  # Discordin rate limit
        wait = float(r.json().get("retry_after", 2))
        time.sleep(wait + 0.5)
        r = requests.post(webhook, json={"embeds": [embed]}, timeout=TIMEOUT)
    r.raise_for_status()


def describe_error(e):
    if isinstance(e, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "yhteysvirhe (DNS/TLS/connection refused)"
    return f"{type(e).__name__}: {str(e)[:120]}"


def check_all(seen, webhook=None, dry_run=False):
    round_start = time.monotonic()
    failed = []

    for store in STORES:
        baseline = store not in seen
        t0 = time.monotonic()

        try:
            products = fetch_products(store)
        except Exception as e:
            reason = describe_error(e)
            failed.append((store, reason))
            print(f"[virhe] {store}: {reason} ({time.monotonic() - t0:.1f}s)")
            continue

        took = time.monotonic() - t0
        known = seen.setdefault(store, set())
        new = [p for p in products if str(p.get("id")) not in known]

        for p in new:
            known.add(str(p.get("id")))
            # Ensimmaisella kerralla vain tallennetaan, ei spammata
            if baseline:
                continue
            try:
                notify(webhook, store, p, dry_run=dry_run)
                if not dry_run:
                    time.sleep(1)  # Discordin rate limit
            except Exception as e:
                print(f"[webhook-virhe] {store}: {describe_error(e)}")

        if baseline:
            print(f"{store}: OK, {len(products)} tuotetta, "
                  f"pohjadata tallennettu ({took:.1f}s)")
        else:
            print(f"{store}: OK, {len(products)} tuotetta, "
                  f"{len(new)} uutta ({took:.1f}s)")

    if dry_run:
        print("[dry-run] seen.json:ia ei kirjoiteta")
    else:
        save_seen(seen)

    total = time.monotonic() - round_start
    ok = len(STORES) - len(failed)
    print(f"--- kierros valmis: {ok}/{len(STORES)} kauppaa OK, {total:.1f}s ---")
    if failed:
        print("Epaonnistuneet kaupat:")
        for store, reason in failed:
            print(f"  {store}: {reason}")
    return failed


def main():
    ap = argparse.ArgumentParser(description="Shopify-tuotemonitori")
    ap.add_argument("--once", action="store_true",
                    help="aja yksi kierros ja lopeta")
    ap.add_argument("--dry-run", action="store_true",
                    help="tee kaikki muu paitsi ala posta Discordiin; "
                         "tulosta mita olisi postattu")
    args = ap.parse_args()

    webhook = None if args.dry_run else get_webhook()
    seen = load_seen()

    while True:
        check_all(seen, webhook=webhook, dry_run=args.dry_run)
        if args.once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
