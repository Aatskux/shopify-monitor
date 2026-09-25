#!/usr/bin/env python3
"""
Shopify-monitori: seuraa kauppojen /products.json -listaa ja postaa
uudet tuotteet Discordin webhookiin.

Asennus:       pip install requests
Jatkuva ajo:   DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py
Yksi kierros:  DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py --once
Testiajo:      python3 shopify_monitor.py --once --dry-run   (ei postaa Discordiin)

Saman tuotteen tunnistus kauppojen valilla: uuden tuotteen ensimmaisesta
kuvasta lasketaan perceptual hash (image_hashes.json) ja nimia verrataan
sumeasti. Ristiinkauppaosumat postataan heti TRENDS_WEBHOOKiin, ja sinne
lahtee myos 2 tunnin kooste. Loppuunmyynnit (variantti available -> false)
kootaan kerran tunnissa SALES_WEBHOOKiin.
"""

import argparse
import io
import json
import os
import pathlib
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import imagehash
import requests
from PIL import Image
from rapidfuzz import fuzz

# Kaupat joita seurataan luetaan tiedostosta stores.json:
# {"https://kauppa.com": "$"}  (osoite ilman kauttaviivaa lopussa -> valuuttamerkki)
STORES_FILE = pathlib.Path("stores.json")
STORES = {}                        # tayttyy load_stores():lla main():ssa

INTERVAL = 60                      # sekuntia kierrosten välillä (vain jatkuvassa ajossa)
STATE = pathlib.Path("seen.json")  # muistaa jo nähdyt tuotteet
HEADERS = {"User-Agent": "Mozilla/5.0 (product monitor)"}
TIMEOUT = 15                       # sekuntia yhtä HTTP-pyyntöä kohti
PAGE_SIZE = 250
MAX_PAGES = 20                     # turvaraja, ettei sivutus jää jumiin

# --- tilatiedostot (kirjoitetaan vain kun sisalto oikeasti muuttuu) ---
IMAGE_HASHES = pathlib.Path("image_hashes.json")    # kauppa -> tuote -> hash, nimi, ...
STOCK_STATE = pathlib.Path("stock_state.json")      # kauppa -> saatavilla olevat variantit
EVENTS_FILE = pathlib.Path("events.json")           # loppuunmyynnit (72 h)
MATCHES_FILE = pathlib.Path("matches.json")         # ristiinkauppaosumat (7 pv)
SUMMARY_STATE = pathlib.Path("summary_state.json")  # milloin yhteenvedot viimeksi lahetettiin

# --- kuvatiivisteet ---
MAX_IMAGE_DOWNLOADS = 300          # per ajo; loput jatkuvat seuraavalla kierroksella
IMAGE_TIMEOUT = 10                 # sekuntia per kuva
IMAGE_BUDGET = 180                 # sekuntia kuvalatauksille yhteensa per ajo
IMAGE_WIDTH = 256                  # Shopifyn CDN pienentaa kuvan
IMAGE_MAX_TRIES = 3                # epaonnistuneen kuvan yritykset ennen luovutusta

# --- saman tuotteen tunnistus ---
HASH_MAX_DISTANCE = 8              # Hamming-etaisyys <= tama = sama kuva
NAME_WITH_IMAGE = 60               # kuva + nimi yli taman -> "varma"
NAME_ONLY = 85                     # pelkka nimi yli taman -> "mahdollinen"
# token_set_ratio antaa 100 aina kun toinen nimi on toisen osajoukko
# ("2009 Jacket" vs. mika tahansa takki), joten pelkan nimen osumalta
# vaaditaan lisaksi etta nimet ovat kokonaisuutenakin lahella.
NAME_ONLY_SORT_MIN = 80
FILLER_WORDS = {"viral", "trending", "new", "best", "bestseller", "seller",
                "sale", "hot", "top", "limited", "exclusive", "free", "shipping"}
# Kassalisat (toimitusvakuutus ym.) jakavat saman ikonin kymmenissa
# kaupoissa eivatka ole tuotteita.
SERVICE_TITLE = re.compile(r"protection|priority processing|insurance|gift ?card|"
                           r"e-?gift|\btips?\b|donation|membership", re.I)

# --- yhteenvedot ---
EVENT_RETENTION = timedelta(hours=72)
MATCH_RETENTION = timedelta(days=7)
SALES_EVERY = timedelta(hours=1)
TRENDS_EVERY = timedelta(hours=2)
# Cron ei osu minuutilleen: pieni etuajo, ettei tahti valu joka kerta
# yhden 5 min kierroksen myohemmaksi.
SUMMARY_GRACE = timedelta(minutes=3)
FRESH_DAYS = 7
DIGEST_TOP = 10
# Kauppa jonka saatavilla olevista varianteista yli puolet loppuu samalla
# kierroksella on varastosynkan hairio, ei myyntia.
STOCK_NOISE_SHARE = 0.5
STOCK_NOISE_MIN = 20


def get_webhook():
    """Webhook luetaan aina ympäristömuuttujasta - ei koskaan kovakoodattuna."""
    try:
        return os.environ["DISCORD_WEBHOOK"]
    except KeyError:
        sys.exit("DISCORD_WEBHOOK puuttuu ymparistomuuttujista "
                 "(tai aja --dry-run-tilassa).")


def load_stores(path=None):
    """Lukee seurattavat kaupat. Rikkinainen tai puuttuva lista pysayttaa ajon
    selkeasti - tyhjalla listalla ajaminen tallentaisi tyhjan tilan."""
    path = path or STORES_FILE
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        sys.exit(f"{path} puuttuu")
    except json.JSONDecodeError as e:
        sys.exit(f"{path} ei ole validia JSONia ({e})")
    if not isinstance(data, dict) or not data:
        sys.exit(f"{path} pitaa olla ei-tyhja objekti "
                 '{"https://kauppa.com": "$"}')
    return data


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
    text = json.dumps(data, indent=1) + "\n"
    if not STATE.exists() or STATE.read_text() != text:   # ei turhia kirjoituksia
        STATE.write_text(text)


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

    post_webhook(webhook, {"embeds": [embed]})


def post_webhook(webhook, payload):
    r = requests.post(webhook, json=payload, timeout=TIMEOUT)
    if r.status_code == 429:  # Discordin rate limit
        wait = float(r.json().get("retry_after", 2))
        time.sleep(wait + 0.5)
        r = requests.post(webhook, json=payload, timeout=TIMEOUT)
    r.raise_for_status()


def describe_error(e):
    if isinstance(e, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "yhteysvirhe (DNS/TLS/connection refused)"
    return f"{type(e).__name__}: {str(e)[:120]}"


# --- apufunktiot: aika ja tilatiedostot ---------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value):
    """ISO-aikaleima -> aware datetime, tai None jos ei kelpaa."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_json(path, default):
    """Kuten load_seen: rikkinainen tai vaaran muotoinen tiedosto ei kaada
    ajoa, vaan aloitetaan tyhjasta."""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"[varoitus] {path} ei ole validia JSONia ({e}), aloitetaan tyhjasta")
        return default
    if not isinstance(data, type(default)):
        print(f"[varoitus] {path} on vaaraa muotoa, aloitetaan tyhjasta")
        return default
    return data


def _save_json(path, data):
    """Kirjoittaa vain jos sisalto muuttui: muuten git nakisi turhan
    muutoksen (ja mtime paivittyisi) joka kierroksella."""
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True


def _host(store):
    return urlsplit(store).netloc or store


def _product_url(store, handle):
    return f"{store}/products/{handle}"


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _fmt_date(value):
    dt = _parse_time(value)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "?"


def _first_image(p):
    return (p.get("images") or [{}])[0].get("src")


def is_service(p):
    return bool(SERVICE_TITLE.search(p.get("title") or ""))


# --- loppuunmyynnit -----------------------------------------------------

def detect_soldouts(store, products, known, now):
    """Variantti joka oli edellisella kierroksella saatavilla ja on nyt
    loppu = loppuunmyynti.

    known on kaupan edellisen kierroksen saatavilla olevat variantti-id:t,
    tai None jos kauppa on uusi -> pohjadata, ei tapahtumia.
    Palauttaa (saatavilla olevat variantit nyt, tapahtumat).

    Jos tuotteen KAIKKI (vah. 2) seurattua varianttia loppuvat samalla
    kierroksella, se on todennakoisesti kauppiaan muokkaus: yksi "edit"-
    tapahtuma eika loppuunmyynteja.
    """
    available = sorted(str(v.get("id")) for p in products
                       for v in p.get("variants") or [] if v.get("available"))
    if known is None:
        return available, []

    was = set(known)
    events, gone_total = [], 0
    when = _iso(now)
    for p in products:
        tracked = [v for v in p.get("variants") or [] if str(v.get("id")) in was]
        gone = [v for v in tracked if not v.get("available")]
        if not gone:
            continue
        gone_total += len(gone)
        base = {
            "time": when,
            "store": store,
            "product_id": str(p.get("id")),
            "title": p.get("title") or "(nimeton tuote)",
            "handle": p.get("handle", ""),
        }
        if len(tracked) > 1 and len(gone) == len(tracked):
            events.append({**base, "type": "edit",
                           "variant": f"kaikki {len(gone)} varianttia"})
            continue
        for v in gone:
            events.append({**base, "type": "soldout",
                           "variant": v.get("title") or str(v.get("id")),
                           "variant_id": str(v.get("id")),
                           "price": v.get("price")})

    if len(was) >= STOCK_NOISE_MIN and gone_total > STOCK_NOISE_SHARE * len(was):
        print(f"[varasto] {store}: {gone_total}/{len(was)} varianttia loppui kerralla, "
              "tulkitaan varastosynkan hairioksi")
        return available, []
    return available, events


def prune(records, now, keep):
    """Pudottaa yli keep vanhat (ja aikaleimattomat) tietueet."""
    cutoff = now - keep
    return [r for r in records if (_parse_time(r.get("time")) or cutoff) > cutoff]


# --- kuvatiivisteet ja nimet --------------------------------------------

def image_hash(src):
    """Lataa kuvan CDN:sta pienennettyna ja laskee phashin (16 hex-merkkia)."""
    url = "https:" + src if src.startswith("//") else src
    url += ("&" if "?" in url else "?") + f"width={IMAGE_WIDTH}"
    r = requests.get(url, headers=HEADERS, timeout=IMAGE_TIMEOUT)
    r.raise_for_status()
    with Image.open(io.BytesIO(r.content)) as im:
        return str(imagehash.phash(im.convert("RGB")))


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def normalize_name(title):
    """Pienet kirjaimet, ei valimerkkeja, vuosilukuja eika taytesanoja."""
    t = re.sub(r"\b(19|20)\d\d\b", " ", (title or "").lower())
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(w for w in t.split() if w not in FILLER_WORDS)


def _entry(store, p):
    currency = STORES.get(store, "$")
    return {
        "hash": None,
        "name": p.get("title") or "(nimeton tuote)",
        "published_at": p.get("published_at"),
        "handle": p.get("handle", ""),
        "image": _first_image(p),
        "price": _fmt_money(_prices(p.get("variants") or [], "price"), currency),
    }


def update_image_hashes(hashes, catalog):
    """Laskee puuttuvat tiivisteet. Jarjestys: uudet tuotteet (pending,
    "new") ensin, sitten uudelleenyritykset, sitten pohjadata olemassa
    oleville tuotteille. Katto MAX_IMAGE_DOWNLOADS latausta / IMAGE_BUDGET s.

    hashes = {"products": {kauppa: {id: entry}}, "pending": {kauppa: {id: {...}}}}
    catalog = {kauppa: [tuotteet]} vain taman kierroksen onnistuneista kaupoista.
    Palauttaa listan (kauppa, id) uusista tuotteista joille hash valmistui.
    """
    products, pending = hashes["products"], hashes["pending"]

    # siivous: poistetut kaupat ja kaupasta poistuneet tuotteet
    for table in (products, pending):
        for store in [s for s in table if s not in STORES]:
            del table[store]
        for store, items in catalog.items():
            ids = {str(p.get("id")) for p in items}
            for pid in [i for i in table.get(store, {}) if i not in ids]:
                del table[store][pid]

    index = {(s, str(p.get("id"))): p for s, items in catalog.items() for p in items}
    queued = [(s, pid, info) for s, d in pending.items() for pid, info in d.items()
              if (s, pid) in index]
    queued.sort(key=lambda j: not j[2].get("new"))           # uudet ensin
    backfill = [(s, pid, None) for (s, pid), p in index.items()
                if pid not in products.get(s, {}) and pid not in pending.get(s, {})
                and not is_service(p)]

    jobs = queued + backfill
    done_new, downloads, failed, left = [], 0, 0, 0
    started = time.monotonic()
    for i, (store, pid, info) in enumerate(jobs):
        p = index[(store, pid)]
        entry = _entry(store, p)
        if entry["image"]:
            if downloads >= MAX_IMAGE_DOWNLOADS or time.monotonic() - started > IMAGE_BUDGET:
                left = len(jobs) - i      # jatkuu seuraavalla kierroksella
                break
            downloads += 1
            try:
                entry["hash"] = image_hash(entry["image"])
            except Exception as e:
                failed += 1
                tries = (info or {}).get("tries", 0) + 1
                if tries < IMAGE_MAX_TRIES:
                    pending.setdefault(store, {})[pid] = {
                        "new": bool(info and info.get("new")), "tries": tries}
                    continue
                print(f"[kuva] {store} {pid}: luovutetaan ({describe_error(e)})")
        products.setdefault(store, {})[pid] = entry
        if info is not None:
            del pending[store][pid]
            if info.get("new"):
                done_new.append((store, pid))

    for table in (products, pending):
        for store in [s for s, d in table.items() if not d]:
            del table[store]

    print(f"[kuvat] {downloads} latausta ({failed} epaonnistui), "
          f"{time.monotonic() - started:.1f}s" + (f", jonossa viela {left}" if left else ""))
    return done_new


STRENGTH_RANK = {"varma": 3, "vahva": 2, "mahdollinen": 1}


def classify(a, b, names):
    """Osuman vahvuus kahdelle tiivistemerkinnalle, tai None."""
    name_a, name_b = names(a), names(b)
    set_score = fuzz.token_set_ratio(name_a, name_b) if name_a and name_b else 0
    dist = hamming(a["hash"], b["hash"]) if a.get("hash") and b.get("hash") else None
    if dist is not None and dist <= HASH_MAX_DISTANCE:
        strength = "varma" if set_score > NAME_WITH_IMAGE else "vahva"
    elif set_score > NAME_ONLY and fuzz.token_sort_ratio(name_a, name_b) >= NAME_ONLY_SORT_MIN:
        strength = "mahdollinen"
    else:
        return None
    return {"strength": strength, "distance": dist, "name_score": round(set_score, 1)}


def find_matches(store, pid, products):
    """Paras osuma per MUU kauppa. Saman kaupan tuotteita ei verrata."""
    cache = {}

    def names(e):
        key = e["name"]
        if key not in cache:
            cache[key] = normalize_name(key)
        return cache[key]

    me = products[store][pid]
    best = {}
    for other, items in products.items():
        if other == store:
            continue
        for opid, e in items.items():
            m = classify(me, e, names)
            if m is None:
                continue
            m.update(store=other, product_id=opid)
            rank = (STRENGTH_RANK[m["strength"]], -(m["distance"] if m["distance"] is not None else 99))
            if other not in best or rank > best[other][0]:
                best[other] = (rank, m)
    return sorted((m for _, m in best.values()),
                  key=lambda m: (-STRENGTH_RANK[m["strength"]], m["store"]))


STRENGTH_COLOR = {"varma": 0x2ECC71, "vahva": 0xE67E22, "mahdollinen": 0x95A5A6}


def build_match_card(store, pid, matches, products):
    me = products[store][pid]
    total = len(matches) + 1
    best = matches[0]["strength"]
    lines = [f"**Uusi: {_host(store)}** — julkaistu {_fmt_date(me.get('published_at'))}"
             + (f" — {me['price']}" if me.get("price") else "")]
    for mt in matches[:15]:
        other = products[mt["store"]][mt["product_id"]]
        detail = []
        if mt["distance"] is not None:
            detail.append(f"kuva d={mt['distance']}")
        detail.append(f"nimi {mt['name_score']:.0f}")
        lines.append(
            f"[{_host(mt['store'])}]({_product_url(mt['store'], other.get('handle', ''))})"
            f" — julkaistu {_fmt_date(other.get('published_at'))}"
            f" — {mt['strength']} ({', '.join(detail)})")
    if len(matches) > 15:
        lines.append(f"… +{len(matches) - 15} kauppaa")
    embed = {
        "title": _clip(f"Sama tuote {total} kaupassa: {me['name']}", 256),
        "url": _product_url(store, me.get("handle", "")),
        "color": STRENGTH_COLOR[best],
        "description": "\n".join(lines),
        "fields": [
            {"name": "Osuman vahvuus", "value": best, "inline": True},
            {"name": "Kaupoissa yhteensa", "value": str(total), "inline": True},
        ],
    }
    if me.get("image"):
        embed["thumbnail"] = {"url": me["image"]}
    return {"embeds": [embed]}


def process_new_matches(done_new, hashes, matches_log, now, dry_run=False):
    """Etsii osumat uusille tuotteille, kirjaa ne ja postaa kortit heti."""
    products = hashes["products"]
    webhook = os.environ.get("TRENDS_WEBHOOK")
    posted_pairs, found = set(), 0
    for store, pid in done_new:
        matches = find_matches(store, pid, products)
        if not matches:
            continue
        found += 1
        best = matches[0]["strength"]
        matches_log.append({
            "time": _iso(now), "store": store, "product_id": pid,
            "name": products[store][pid]["name"], "strength": best,
            "stores": len(matches) + 1, "matches": matches,
        })
        print(f"[osuma] {_host(store)}: {products[store][pid]['name']!r} -> "
              + ", ".join(f"{_host(m['store'])} ({m['strength']})" for m in matches))

        # Kaksi uutta samaa tuotetta samalla kierroksella: yksi kortti riittaa.
        pairs = {frozenset([(store, pid), (m["store"], m["product_id"])]) for m in matches}
        if pairs <= posted_pairs:
            continue
        posted_pairs |= pairs

        payload = build_match_card(store, pid, matches, products)
        if dry_run:
            _print_payload("TRENDS_WEBHOOK", payload)
        elif not webhook:
            print("[osuma] TRENDS_WEBHOOK puuttuu, korttia ei postata")
        else:
            try:
                post_webhook(webhook, payload)
                time.sleep(1)  # Discordin rate limit
            except Exception as e:
                print(f"[webhook-virhe] TRENDS_WEBHOOK: {describe_error(e)}")
    return found


# --- yhteenvedot --------------------------------------------------------

def _print_payload(label, payload):
    if payload.get("content"):
        print(f"  [dry-run] POST {label}: {payload['content']}")
    for embed in payload.get("embeds", []):
        vals = " | ".join(f"{f['name']}: {f['value']}" for f in embed.get("fields", []))
        text = f"  [dry-run] POST {label}: {embed['title']} | {vals}"
        if embed.get("description"):
            text += " | " + embed["description"]
        print(text.replace("\n", " / "))


def build_sales_summary(events, since, now):
    """Loppuunmyynnit aikavalilla: kaupat maarineen ja top 5 tuotetta.
    None jos loppuunmyynteja ei ollut."""
    window = [e for e in events
              if since < (_parse_time(e.get("time")) or since) <= now]
    sales = [e for e in window if e.get("type") == "soldout"]
    if not sales:
        return None
    edits = sum(1 for e in window if e.get("type") == "edit")

    per_store = Counter(e["store"] for e in sales)
    per_product = Counter((e["store"], e["product_id"]) for e in sales)
    info = {(e["store"], e["product_id"]): e for e in sales}

    lines, used = [], 0
    ranked = per_store.most_common()
    for i, (store, n) in enumerate(ranked):
        line = f"**{_host(store)}** — {n}"
        if used + len(line) > 3800:          # embedin kuvaus max 4096
            lines.append(f"… +{len(ranked) - i} kauppaa")
            break
        lines.append(line)
        used += len(line) + 1

    top = []
    for i, (key, n) in enumerate(per_product.most_common(5), 1):
        e = info[key]
        title = _clip(e.get("title") or "?", 60)
        top.append(f"{i}. [{title}]({_product_url(e['store'], e.get('handle', ''))})"
                   f" — {_host(e['store'])} — {n}")

    footer = f"{len(per_store)} kauppaa"
    if edits:
        footer += f" · {edits} kauppiaan muokkausta suodatettu pois"
    embed = {
        "title": f"Loppuunmyynnit {since:%H:%M}–{now:%H:%M} UTC: {len(sales)} varianttia",
        "description": "\n".join(lines),
        "color": 0x3498DB,
        "fields": [{"name": "Top 5 tuotetta", "value": _clip("\n".join(top), 1024)}],
        "footer": {"text": footer},
        "timestamp": _iso(now),
    }
    return {"embeds": [embed]}


def match_groups(matches_log, products):
    """Yhdistaa osumat ryhmiksi (sama tuote useassa kaupassa)."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    latest = {}
    for rec in matches_log:
        a = (rec["store"], rec["product_id"])
        find(a)
        latest[a] = rec
        for mt in rec.get("matches", []):
            parent[find((mt["store"], mt["product_id"]))] = find(a)

    groups = defaultdict(list)
    for node in parent:
        if node[1] in products.get(node[0], {}):      # poistetut tuotteet pois
            groups[find(node)].append(node)
    return [(nodes, [latest[n] for n in nodes if n in latest])
            for nodes in groups.values() if len({s for s, _ in nodes}) > 1]


def digest_rows(matches_log, products, events, now):
    """Top ryhmat: kauppojen maara painotettuna tuoreudella."""
    soldouts = Counter((e["store"], e["product_id"]) for e in events
                       if e.get("type") == "soldout")
    rows = []
    for nodes, recs in match_groups(matches_log, products):
        stores = sorted({s for s, _ in nodes})
        published = [t for t in (_parse_time(products[s][p].get("published_at"))
                                 for s, p in nodes) if t]
        newest = max(published) if published else None
        age_days = max(0.0, (now - newest).total_seconds() / 86400) if newest else None
        fresh = max(0.0, 1 - age_days / FRESH_DAYS) if age_days is not None else 0.0
        rep = max(recs, key=lambda r: r["time"]) if recs else None
        rep_node = (rep["store"], rep["product_id"]) if rep else nodes[0]
        strengths = Counter(m["strength"] for r in recs for m in r.get("matches", []))
        rows.append({
            "score": round(len(stores) * (1 + fresh), 2), "stores": stores,
            "node": rep_node, "age_days": age_days, "strengths": strengths,
            "soldouts": sum(soldouts[n] for n in nodes),
        })
    rows.sort(key=lambda r: (-r["score"], -len(r["stores"]), r["node"]))
    return rows[:DIGEST_TOP]


def build_digest(matches_log, products, events, now):
    rows = digest_rows(matches_log, products, events, now)
    if not rows:
        return None
    cards = []
    for r in rows:
        store, pid = r["node"]
        e = products[store][pid]
        hosts = ", ".join(_host(s) for s in r["stores"][:8])
        if len(r["stores"]) > 8:
            hosts += f" +{len(r['stores']) - 8}"
        fields = [
            {"name": "Kauppoja", "value": str(len(r["stores"])), "inline": True},
            {"name": "Pisteet", "value": f"{r['score']:.1f}", "inline": True},
            {"name": "Uusin julkaisu",
             "value": f"{r['age_days']:.1f} pv sitten" if r["age_days"] is not None else "?",
             "inline": True},
            {"name": "Osumat", "value": ", ".join(
                f"{n} {k}" for k, n in sorted(r["strengths"].items(),
                                              key=lambda kv: -STRENGTH_RANK[kv[0]])) or "-",
             "inline": True},
        ]
        if e.get("price"):
            fields.insert(0, {"name": "Hinta", "value": e["price"], "inline": True})
        if r["soldouts"]:
            fields.append({"name": "Loppuunmyynnit 72 h", "value": str(r["soldouts"]),
                           "inline": True})
        card = {
            "title": _clip(e["name"], 256),
            "url": _product_url(store, e.get("handle", "")),
            "color": 0x9B59B6,
            "description": hosts,
            "fields": fields,
        }
        if e.get("image"):
            card["thumbnail"] = {"url": e["image"]}
        cards.append(card)
    return {"content": f"**Trendikooste {now:%d.%m. %H:%M} UTC** — top {len(cards)}: "
                       "eniten kauppoja, painotettuna tuoreudella",
            "embeds": cards}                  # Discord: max 10 embedia


def _is_due(summary, key, every, now):
    last = _parse_time(summary.get(key))
    return last is None or now - last >= every - SUMMARY_GRACE


def send_summaries(events, matches_log, products, summary, now, dry_run=False):
    """Tunnin loppuunmyyntikooste ja 2 tunnin trendikooste. summary kertoo
    milloin kumpikin viimeksi LAHETETTIIN; se paivittyy vain lahetyksesta,
    joten tilatiedosto ei muutu hiljaisina tunteina."""

    def sales_payload():
        last = _parse_time(summary.get("sales"))
        return build_sales_summary(events, last or now - SALES_EVERY, now)

    def trends_payload():
        last = _parse_time(summary.get("trends")) or now - TRENDS_EVERY
        if not any((_parse_time(r.get("time")) or last) > last for r in matches_log):
            return None                      # ei uusia osumia -> ei viestia
        return build_digest(matches_log, products, events, now)

    jobs = [("sales", "SALES_WEBHOOK", SALES_EVERY, sales_payload),
            ("trends", "TRENDS_WEBHOOK", TRENDS_EVERY, trends_payload)]
    for key, env, every, build in jobs:
        if not _is_due(summary, key, every, now):
            continue
        payload = build()
        if payload is None:
            continue
        webhook = os.environ.get(env)
        if dry_run:
            _print_payload(env, payload)
            continue
        if not webhook:
            print(f"[{key}] {env} puuttuu, yhteenveto ohitetaan")
            continue
        try:
            post_webhook(webhook, payload)
        except Exception as e:
            print(f"[webhook-virhe] {env}: {describe_error(e)}")
            continue                          # seuraava kierros yrittaa uudelleen
        print(f"[{key}] yhteenveto lahetetty")
        summary[key] = _iso(now)


def check_all(seen, webhook=None, dry_run=False, now=None):
    round_start = time.monotonic()
    now = now or _utcnow()
    failed = []
    catalog = {}                 # taman kierroksen onnistuneet haut
    stock = _load_json(STOCK_STATE, {})
    events = _load_json(EVENTS_FILE, [])
    matches_log = _load_json(MATCHES_FILE, [])
    hashes = _load_json(IMAGE_HASHES, {})
    hashes.setdefault("products", {})
    hashes.setdefault("pending", {})

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
        catalog[store] = products

        stock_baseline = store not in stock
        stock[store], found = detect_soldouts(store, products, stock.get(store), now)
        events += found
        soldouts = sum(1 for e in found if e["type"] == "soldout")
        activity = "" if stock_baseline else f", {soldouts} loppuunmyyty"

        known = seen.setdefault(store, set())
        new = [p for p in products if str(p.get("id")) not in known]

        for p in new:
            known.add(str(p.get("id")))
            # Ensimmaisella kerralla vain tallennetaan, ei spammata
            if baseline:
                continue
            # Uuden tuotteen kuva tiivistetaan ja verrataan muihin kauppoihin.
            # Pohjadatan tuotteet tiivistetaan myohemmin ilman postausta.
            if not is_service(p):
                hashes["pending"].setdefault(store, {})[str(p.get("id"))] = {
                    "new": True, "tries": 0}
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
                  f"{len(new)} uutta{activity} ({took:.1f}s)")

    done_new = update_image_hashes(hashes, catalog)
    found = process_new_matches(done_new, hashes, matches_log, now, dry_run=dry_run)
    print(f"[osumat] {len(done_new)} uutta tuotetta tiivistetty, "
          f"{found} ristiinkauppaosumaa")

    events = prune(events, now, EVENT_RETENTION)
    matches_log = prune(matches_log, now, MATCH_RETENTION)
    summary = _load_json(SUMMARY_STATE, {})
    send_summaries(events, matches_log, hashes["products"], summary, now, dry_run=dry_run)

    if dry_run:
        print("[dry-run] tilatiedostoja ei kirjoiteta")
    else:
        save_seen(seen)
        # Kuten seen.json: poistettujen kauppojen tila ei jaa roikkumaan.
        _save_json(STOCK_STATE, {k: v for k, v in stock.items() if k in STORES})
        _save_json(IMAGE_HASHES, hashes)
        _save_json(EVENTS_FILE, events)
        _save_json(MATCHES_FILE, matches_log)
        _save_json(SUMMARY_STATE, summary)

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

    global STORES
    STORES = load_stores()
    webhook = None if args.dry_run else get_webhook()
    seen = load_seen()

    while True:
        check_all(seen, webhook=webhook, dry_run=args.dry_run)
        if args.once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
