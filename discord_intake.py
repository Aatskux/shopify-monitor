#!/usr/bin/env python3
"""
Discord-intake: lukee kanavalle postatut kauppojen osoitteet ja lisaa
toimivat Shopify-kaupat stores.json:iin.

Jokainen viestin verkkotunnus saa reaktion:
  :white_check_mark:  lisatty (products.json vastasi), pohjadata seen.json:iin
  :x:                 oli jo listalla
  :warning:           ei vastannut (401/403/404/timeout/ei Shopify) - ei lisatty

Asennus:  pip install requests
Ajo:      DISCORD_BOT_TOKEN=... AI_STORES_CHANNEL_ID=... python3 discord_intake.py
Testiajo: ... python3 discord_intake.py --dry-run   (ei reagoi eika kirjoita tiedostoja)
"""

import argparse
import json
import os
import pathlib
import re
import sys
import time
from urllib.parse import quote

import requests

import shopify_monitor as m

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/Aatskux/shopify-monitor, 1.0)"
STORES_FILE = pathlib.Path("stores.json")
SEEN_FILE = pathlib.Path("seen.json")
STATE_FILE = pathlib.Path("intake_state.json")   # viimeksi kasitelty viesti-id
MESSAGE_LIMIT = 100                # Discordin maksimi per pyynto
MAX_MESSAGE_PAGES = 10             # loput jaavat seuraavalle ajolle
MAX_RETRIES = 5                    # 429-uudelleenyritykset per pyynto
DEFAULT_CURRENCY = "$"

ADDED = "✅"            # :white_check_mark:
DUPLICATE = "❌"        # :x:
UNREACHABLE = "⚠️"  # :warning:

# Verkkotunnus, valinnaisesti https://-etuliitteella. Edella ei saa olla
# kirjainta, @:ia, pistetta tai kauttaviivaa, jottei sahkopostiosoitteista
# tai polun osista (kauppa.com/products.json) poimita vaaria domaineja.
DOMAIN_RE = re.compile(
    r"(?<![\w@./-])"
    r"(?:https?://)?"
    r"((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})"
    r"(?![\w-])",
    re.IGNORECASE,
)


# --- verkkotunnukset ----------------------------------------------------

def normalize(host):
    """kauppa.com / WWW.Kauppa.com. -> https://kauppa.com"""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return f"https://{host}"


def extract_stores(text):
    """Poimii viestista kaikki verkkotunnukset normalisoituna, jarjestys
    sailyttaen ja ilman toistoja ([kauppa.com](https://kauppa.com) = yksi)."""
    out = []
    for match in DOMAIN_RE.finditer(text or ""):
        store = normalize(match.group(1))
        if store not in out:
            out.append(store)
    return out


def _bare(store):
    host = store.lower().split("://", 1)[-1].rstrip("/")
    return host[4:] if host.startswith("www.") else host


def is_known(store, stores):
    """www.kauppa.com ja kauppa.com ovat sama kauppa."""
    bare = _bare(store)
    return any(_bare(s) == bare for s in stores)


# --- Discordin REST-API -------------------------------------------------

def _retry_after(r):
    try:
        return float(r.json()["retry_after"])
    except (ValueError, KeyError, TypeError):
        return float(r.headers.get("Retry-After", 1))


def discord_request(token, method, path, **kwargs):
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    for _ in range(MAX_RETRIES):
        r = requests.request(method, API_BASE + path, headers=headers,
                             timeout=m.TIMEOUT, **kwargs)
        if r.status_code == 429:
            wait = _retry_after(r)
            print(f"[rate limit] {method} {path}: odotetaan {wait:.2f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        # Bucket tyhja -> odotetaan ennakkoon, ettei seuraava pyynto saa 429:aa
        if r.headers.get("X-RateLimit-Remaining") == "0":
            time.sleep(float(r.headers.get("X-RateLimit-Reset-After", 0)))
        return r
    raise RuntimeError(f"{method} {path}: rate limit ei hellittanyt "
                       f"{MAX_RETRIES} yrityksella")


def fetch_messages(token, channel, after):
    """Uudet viestit vanhimmasta uusimpaan."""
    messages = {}
    for _ in range(MAX_MESSAGE_PAGES):
        batch = discord_request(
            token, "GET", f"/channels/{channel}/messages",
            params={"after": after, "limit": MESSAGE_LIMIT},
        ).json()
        for msg in batch:
            messages[msg["id"]] = msg
        if len(batch) < MESSAGE_LIMIT:
            break
        after = max(batch, key=lambda msg: int(msg["id"]))["id"]
    return sorted(messages.values(), key=lambda msg: int(msg["id"]))


def react(token, channel, message_id, emoji):
    emoji = quote(emoji, safe="")
    discord_request(token, "PUT",
                    f"/channels/{channel}/messages/{message_id}/reactions/{emoji}/@me")


# --- kaupan tarkistus ---------------------------------------------------

class NotAStore(Exception):
    pass


def products_url(store):
    return f"{store}/products.json"


def fetch_catalog(store):
    """Hakee kaupan koko tuotelistan. Nostaa poikkeuksen, jos products.json ei
    vastaa 200:lla tai vastauksesta puuttuu products-avain."""
    products, page = [], 1
    while page <= m.MAX_PAGES:
        r = requests.get(
            products_url(store),
            params={"limit": m.PAGE_SIZE, "page": page},
            headers=m.HEADERS,
            timeout=m.TIMEOUT,
        )
        r.raise_for_status()
        if r.status_code != 200:
            raise NotAStore(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise NotAStore("products.json ei ole JSONia") from None
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise NotAStore("vastauksesta puuttuu products-avain")
        batch = data["products"]
        products += batch
        if len(batch) < m.PAGE_SIZE:
            break
        page += 1
        time.sleep(1)
    return products


def describe_error(e):
    if isinstance(e, NotAStore):
        return str(e)
    return m.describe_error(e)


def handle_store(store, stores, seen):
    """Palauttaa (reaktio, selite). Muokkaa stores- ja seen-sanakirjoja."""
    if is_known(store, stores):
        return DUPLICATE, "on jo listalla"
    try:
        products = fetch_catalog(store)
    except Exception as e:
        return UNREACHABLE, f"ei lisatty: {describe_error(e)}"
    stores[store] = DEFAULT_CURRENCY
    # Pohjadata: kaupan nykyiset tuotteet merkitaan nahdyiksi, jottei monitori
    # postaa niita kaikkia seuraavalla kierroksella.
    seen[store] = sorted({str(p.get("id")) for p in products})
    return ADDED, f"lisatty, {len(products)} tuotetta pohjadataksi"


# --- tiedostot ----------------------------------------------------------

def load_json(path, default):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        print(f"[varoitus] {path} ei ole validia JSONia ({e}), aloitetaan tyhjasta")
        return default
    return data if isinstance(data, dict) else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


# --- ajo ----------------------------------------------------------------

def run(token, channel, dry_run=False):
    state = load_json(STATE_FILE, {})
    after = state.get("last_message_id", "0")
    stores = m.load_stores(STORES_FILE)
    seen = load_json(SEEN_FILE, {})

    messages = fetch_messages(token, channel, after)
    print(f"{len(messages)} uutta viestia (after={after})")
    counts = {ADDED: 0, DUPLICATE: 0, UNREACHABLE: 0}

    for msg in messages:
        if not (msg.get("author") or {}).get("bot"):
            process_message(token, channel, msg, stores, seen, counts, dry_run)
        if not dry_run:
            state["last_message_id"] = msg["id"]
            save_json(STATE_FILE, state)

    if dry_run:
        print("[dry-run] ei reaktioita, tiedostoja ei kirjoiteta")
    print(f"--- intake valmis: {counts[ADDED]} lisatty, "
          f"{counts[DUPLICATE]} jo listalla, {counts[UNREACHABLE]} ei vastannut ---")
    return counts


def process_message(token, channel, msg, stores, seen, counts, dry_run):
    reactions = []
    for store in extract_stores(msg.get("content")):
        # Virhe yhdessa domainissa ei saa kaataa muiden kasittelya
        try:
            emoji, note = handle_store(store, stores, seen)
        except Exception as e:
            print(f"[virhe] {store}: {describe_error(e)}")
            continue
        print(f"{store}: {note}")
        counts[emoji] += 1
        if emoji not in reactions:
            reactions.append(emoji)

    if dry_run or not reactions:
        return
    # Tiedostot ensin: jos reagointi kaatuu, kauppa on silti tallessa.
    if ADDED in reactions:
        save_json(STORES_FILE, stores)
        save_json(SEEN_FILE, seen)
    for emoji in reactions:
        try:
            react(token, channel, msg["id"], emoji)
        except Exception as e:
            print(f"[reaktio-virhe] viesti {msg['id']}: {describe_error(e)}")


def main():
    ap = argparse.ArgumentParser(description="Kauppojen lisays Discord-kanavalta")
    ap.add_argument("--dry-run", action="store_true",
                    help="tarkista kaupat, mutta ala reagoi tai kirjoita tiedostoja")
    args = ap.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel = os.environ.get("AI_STORES_CHANNEL_ID", "").strip()
    if not token or not channel:
        sys.exit("DISCORD_BOT_TOKEN tai AI_STORES_CHANNEL_ID puuttuu "
                 "ymparistomuuttujista.")
    run(token, channel, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
