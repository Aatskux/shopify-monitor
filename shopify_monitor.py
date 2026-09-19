#!/usr/bin/env python3
"""
Shopify-monitori: seuraa kauppojen /products.json -listaa ja postaa
uudet tuotteet Discordin webhookiin.

Asennus:       pip install requests
Jatkuva ajo:   DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py
Yksi kierros:  DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py --once
"""

import json
import os
import pathlib
import sys
import time

import requests

# Kaupat joita seurataan (ilman kauttaviivaa lopussa)
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
    "https://wearseventyone.net": "$"
}

WEBHOOK = os.environ["DISCORD_WEBHOOK"]
INTERVAL = 60                      # sekuntia kierrosten välillä (vain jatkuvassa ajossa)
STATE = pathlib.Path("seen.json")  # muistaa jo nähdyt tuotteet
HEADERS = {"User-Agent": "Mozilla/5.0 (product monitor)"}


def load_seen():
    if STATE.exists():
        return {k: set(v) for k, v in json.loads(STATE.read_text()).items()}
    return {}


def save_seen(seen):
    STATE.write_text(json.dumps({k: sorted(v) for k, v in seen.items()}, indent=1))


def fetch_products(store):
    products, page = [], 1
    while True:
        r = requests.get(
            f"{store}/products.json",
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json().get("products", [])
        products += batch
        if len(batch) < 250:
            return products
        page += 1
        time.sleep(1)


def notify(store, p):
    variants = p.get("variants") or [{}]
    price = variants[0].get("price", "?")
    image = (p.get("images") or [{}])[0].get("src")

    embed = {
        "title": p["title"],
        "url": f"{store}/products/{p['handle']}",
        "color": 0x2ECC71,
        "fields": [
            {"name": "Hinta", "value": f"{price} €", "inline": True},
            {"name": "Variantteja", "value": str(len(variants)), "inline": True},
        ],
        "footer": {"text": store.replace("https://", "")},
    }
    if image:
        embed["thumbnail"] = {"url": image}

    requests.post(WEBHOOK, json={"embeds": [embed]}, timeout=15).raise_for_status()


def check_all(seen):
    for store in STORES:
        baseline = store not in seen

        try:
            products = fetch_products(store)
        except Exception as e:
            print(f"[virhe] {store}: {e}")
            continue

        known = seen.setdefault(store, set())
        new = [p for p in products if str(p["id"]) not in known]

        for p in new:
            known.add(str(p["id"]))
            # Ensimmaisella kerralla vain tallennetaan, ei spammata
            if not baseline:
                try:
                    notify(store, p)
                    time.sleep(1)  # Discordin rate limit
                except Exception as e:
                    print(f"[webhook-virhe] {e}")

        if baseline:
            print(f"{store}: pohjadata tallennettu ({len(products)} tuotetta)")
        else:
            print(f"{store}: {len(new)} uutta tuotetta")

    save_seen(seen)


def main():
    once = "--once" in sys.argv
    seen = load_seen()

    while True:
        check_all(seen)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
