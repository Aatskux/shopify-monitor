#!/usr/bin/env python3
"""
Shopify-monitori: seuraa kauppojen /products.json -listaa ja postaa
uudet tuotteet Discordin webhookiin.

Asennus:       pip install -r requirements.txt
Jatkuva ajo:   DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py
Yksi kierros:  DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 shopify_monitor.py --once
Testiajo:      python3 shopify_monitor.py --once --dry-run   (ei postaa Discordiin)
Webhook-testi: python3 shopify_monitor.py --test-webhooks      (testikortti SALES/TRENDS)

Saman tuotteen tunnistus kauppojen valilla: uuden tuotteen ensimmaisesta
kuvasta lasketaan perceptual hash (image_hashes.json) ja nimia verrataan
sumeasti. Ristiinkauppaosumat postataan heti TRENDS_WEBHOOKiin, ja sinne
lahtee myos 2 tunnin kooste.

Best-sellerit: kerran tunnissa kaupan /collections/all?sort_by=best-selling
-sivulta luetaan top 20 (best_sellers.json). Nousut postataan
SALES_WEBHOOKiin; KUUMA-kortit (top 10 + sama tuote muissakin kaupoissa)
myos TRENDS_WEBHOOKiin.
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
# Kauppa -> tuote-id -> milloin id havaittiin ensimmaisen kerran puuttuvaksi.
MISSING_FILE = pathlib.Path("seen_missing.json")
PRUNE_AFTER = 24 * 3600            # sekuntia yhtajaksoista puuttumista ennen karsintaa
HEADERS = {"User-Agent": "Mozilla/5.0 (product monitor)"}
TIMEOUT = 15                       # sekuntia yhtä HTTP-pyyntöä kohti
PAGE_SIZE = 250
MAX_PAGES = 20                     # turvaraja, ettei sivutus jää jumiin

# --- tilatiedostot (kirjoitetaan vain kun sisalto oikeasti muuttuu) ---
IMAGE_HASHES = pathlib.Path("image_hashes.json")    # kauppa -> tuote -> hash, nimi, ...
BEST_SELLERS = pathlib.Path("best_sellers.json")    # kauppa -> top 20 handle -> sija
MATCHES_FILE = pathlib.Path("matches.json")         # ristiinkauppaosumat (7 pv)
SUMMARY_STATE = pathlib.Path("summary_state.json")  # milloin yhteenvedot viimeksi lahetettiin

# --- kuvatiivisteet ---
MAX_IMAGE_DOWNLOADS = 300          # per ajo; loput jatkuvat seuraavalla kierroksella
IMAGE_TIMEOUT = 10                 # sekuntia per kuva
IMAGE_BUDGET = 180                 # sekuntia kuvalatauksille yhteensa per ajo
IMAGE_WIDTH = 256                  # Shopifyn CDN pienentaa kuvan
IMAGE_MAX_TRIES = 3                # epaonnistuneen kuvan yritykset ennen luovutusta

# --- saman tuotteen tunnistus ---
# Laatuarviossa etaisyys 6-8 oli lahes aina eri tuote valkoisella taustalla.
HASH_MAX_DISTANCE = 5              # Hamming-etaisyys <= tama = sama kuva
NAME_WITH_IMAGE = 60               # kuva + nimi yli taman -> "varma"
NAME_ONLY = 85                     # pelkka nimi yli taman -> "mahdollinen"
# (mahdolliset osumat eivat tule heti-ilmoituksina, vain 2 h koosteeseen)
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
MATCH_RETENTION = timedelta(days=7)
TRENDS_EVERY = timedelta(hours=2)
# Cron ei osu minuutilleen: pieni etuajo, ettei tahti valu joka kerta
# yhden 5 min kierroksen myohemmaksi.
SUMMARY_GRACE = timedelta(minutes=3)
FRESH_DAYS = 7
DIGEST_TOP = 10

# --- best-sellerit ---
BEST_EVERY = timedelta(hours=1)    # haku kerran tunnissa, ei joka kierroksella
BEST_TOP = 20                      # tallennetaan top 20
BEST_HOT = 10                      # tuore tuote top 10:een -> kortti
BEST_JUMP = 10                     # nousu vah. 10 sijaa top 20:een -> kortti
BEST_MAX_PAGES = 3                 # kokoelmasivuja per haku (sivukoko vaihtelee)
# Pienessa kaupassa top 10 on lahes koko valikoima, joten alle
# BEST_MIN_LISTED tuotteen kaupassa postataan vain kun tuore tuote nousee
# sijoille 1-BEST_SMALL_TOP. Alle 3 tuotteen kauppaa ei voi tarkistaa.
BEST_MIN_LISTED = 15
BEST_SMALL_TOP = 3
BEST_ALPHA_MIN = 0.9               # aakkostesti: nain osa pareista jarjestyksessa
BEST_RECHECK = timedelta(days=7)   # tukematon kauppa tarkistetaan uudelleen


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


def prune_seen(known, products, missing, now):
    """Karsii kaupan nahdyista tuotteista ne, jotka ovat puuttuneet
    products.jsonista yhtajaksoisesti PRUNE_AFTER sekuntia.

    missing = {tuote-id: ensimmainen puuttumisaika} talle kaupalle; paivitetaan
    paikallaan. Id:n palatessa listalle ajastin nollataan, joten hetkellinen
    vajaa vastaus ei aiheuta uudelleenpostauksia. Kutsutaan vain kun kaupan
    haku onnistui talla kierroksella. Tyhjaa vastausta ei uskota lainkaan.
    Palauttaa karsittujen maaran.
    """
    current = {str(p.get("id")) for p in products}
    if not current:
        return 0
    for pid in [i for i in missing if i in current or i not in known]:
        del missing[pid]                      # palasi listalle -> nollaus
    for pid in known - current:
        missing.setdefault(pid, _iso(now))
    expired = [pid for pid, since in missing.items()
               if (now - (_parse_time(since) or now)).total_seconds() >= PRUNE_AFTER]
    for pid in expired:
        known.discard(pid)
        del missing[pid]
    return len(expired)


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


def _is_strong(match):
    return match["strength"] != "mahdollinen"


def process_new_matches(done_new, hashes, matches_log, now, dry_run=False):
    """Etsii osumat uusille tuotteille ja kirjaa ne. Kuvaosumat (varma/vahva)
    postataan heti; pelkat nimiosumat ("mahdollinen") vain 2 h koosteeseen."""
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

        strong = [m for m in matches if _is_strong(m)]
        if not strong:
            continue
        # Kaksi uutta samaa tuotetta samalla kierroksella: yksi kortti riittaa.
        pairs = {frozenset([(store, pid), (m["store"], m["product_id"])]) for m in strong}
        if pairs <= posted_pairs:
            continue
        posted_pairs |= pairs

        payload = build_match_card(store, pid, strong, products)
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


def match_groups(matches_log, products):
    """Yhdistaa kuvaosumat ryhmiksi (sama tuote useassa kaupassa).
    Mahdolliset (vain nimi) osumat eivat yhdista ryhmia."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    latest = {}
    for rec in matches_log:
        strong = [mt for mt in rec.get("matches", []) if _is_strong(mt)]
        if not strong:
            continue
        a = (rec["store"], rec["product_id"])
        find(a)
        latest[a] = {**rec, "matches": strong}
        for mt in strong:
            parent[find((mt["store"], mt["product_id"]))] = find(a)

    groups = defaultdict(list)
    for node in parent:
        if node[1] in products.get(node[0], {}):      # poistetut tuotteet pois
            groups[find(node)].append(node)
    return [(nodes, [latest[n] for n in nodes if n in latest])
            for nodes in groups.values() if len({s for s, _ in nodes}) > 1]


def digest_rows(matches_log, products, now):
    """Top ryhmat: kauppojen maara painotettuna tuoreudella."""
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
        })
    rows.sort(key=lambda r: (-r["score"], -len(r["stores"]), r["node"]))
    return rows[:DIGEST_TOP]


def possible_matches_payload(matches_log, products, since, now):
    """Oma viesti: aikavalin mahdolliset (vain nimi) osumat, tarkistettavaksi."""
    lines, seen_pairs = [], set()
    for rec in matches_log:
        t = _parse_time(rec.get("time"))
        if t is None or not since < t <= now:
            continue
        a = (rec["store"], rec["product_id"])
        for mt in rec.get("matches", []):
            b = (mt["store"], mt["product_id"])
            pair = frozenset([a, b])
            if _is_strong(mt) or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if a[1] not in products.get(a[0], {}) or b[1] not in products.get(b[0], {}):
                continue                      # toinen tuotteista poistettu
            links = []
            for s, pid in (a, b):
                e = products[s][pid]
                links.append(f"[{_clip(e['name'], 50)}]({_product_url(s, e.get('handle', ''))})"
                             f" ({_host(s)})")
            lines.append(f"{links[0]} ↔ {links[1]} — nimi {mt['name_score']:.0f}")
    if not lines:
        return None
    text, shown = "", 0
    for line in lines:
        if len(text) + len(line) > 3800:      # embedin kuvaus max 4096
            break
        text += line + "\n"
        shown += 1
    if shown < len(lines):
        text += f"… +{len(lines) - shown} lisaa"
    return {"embeds": [{
        "title": f"Mahdolliset osumat, tarkista itse ({len(lines)})",
        "description": text.rstrip(),
        "color": STRENGTH_COLOR["mahdollinen"],
        "footer": {"text": "Vain nimi tasmaa, kuva ei. Ei lasketa trendeihin."},
    }]}


def build_digest(matches_log, products, now, since=None):
    """2 h kooste: lista viesteja. Ensin top 10 kuvaosumaryhmaa, sitten
    omana viestinaan jakson mahdolliset osumat."""
    payloads = []
    top = build_top_groups(matches_log, products, now)
    if top:
        payloads.append(top)
    possible = possible_matches_payload(matches_log, products,
                                        since or now - TRENDS_EVERY, now)
    if possible:
        payloads.append(possible)
    return payloads or None


def build_top_groups(matches_log, products, now):
    rows = digest_rows(matches_log, products, now)
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


# --- best-sellerit ------------------------------------------------------

# Shopifyn analytiikkadata on jokaisella kokoelmasivulla tuotteiden
# nayttojarjestyksessa - myos teemoissa jotka lataavat ruudukon
# JavaScriptilla. /collections/all/products.json ei noudata sort_by:ta.
_META = re.compile(r"var meta = (\{.*?\});\s*\n", re.S)


def collection_handles(store, sort, pages=BEST_MAX_PAGES, want=BEST_TOP):
    """Kaikki-kokoelman handlet sivun jarjestyksessa, enintaan want kpl.
    None jos sivulla ei ole jarjestysdataa."""
    handles = []
    for page in range(1, pages + 1):
        r = requests.get(f"{store}/collections/all",
                         params={"sort_by": sort, "page": page},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        m = _META.search(r.text)
        try:
            items = json.loads(m.group(1)).get("products") or [] if m else None
        except ValueError:
            items = None
        if items is None:
            return None if page == 1 else handles
        new = [p["handle"] for p in items if p.get("handle") and p["handle"] not in handles]
        if not new:
            break
        handles += new
        if len(handles) >= want:
            break
    return handles[:want]


def sorting_supported(store, best, titles):
    """Noudattaako kauppa sort_by:ta? Tarkistetaan aakkosjarjestyksella:
    sen pitaa oikeasti olla aakkosissa ja erota best-selling-listasta.
    Muuten kauppa naytaa oletusjarjestyksen (usein uusin ensin), jolloin
    jokainen uusi tuote nayttaisi nousevan top 10:een."""
    alpha = collection_handles(store, "title-ascending", pages=1) or []
    names = [titles[h].lower() for h in alpha if h in titles]
    if len(names) < 3:
        return False
    ordered = sum(a <= b for a, b in zip(names, names[1:])) / (len(names) - 1)
    n = min(len(alpha), len(best))
    return ordered >= BEST_ALPHA_MIN and alpha[:n] != best[:n]


def rise_reason(rank, before, published, now, small=False):
    """Miksi nousu postataan, tai None. before = edellinen sija tai None
    jos tuote oli top 20:n ulkopuolella (eli sija >= 21). small = kaupassa
    alle BEST_MIN_LISTED tuotetta: vain tuore tuote sijoille 1-3."""
    fresh = published is not None and now - published < timedelta(days=FRESH_DAYS)
    if small:
        # Pienessa kaupassa kaikki tuotteet ovat listalla, joten before=None
        # tarkoittaa etta tuote julkaistiin vasta nyt. Myymattomien tuotteiden
        # jarjestys on satunnainen (uusi voi ilmestya heti sijalle 1), joten
        # ensiesiintyminen ei ole nousu.
        if fresh and rank <= BEST_SMALL_TOP and before is not None and before > BEST_SMALL_TOP:
            return f"Alle 7 pv vanha tuote nousi sijalta {before} sijalle {rank} (pieni kauppa)"
        return None
    if fresh and rank <= BEST_HOT and (before is None or before > BEST_HOT):
        return "Alle 7 pv vanha tuote nousi top 10:een"
    if before is not None and before - rank >= BEST_JUMP:
        return f"Nousi {before - rank} sijaa"
    if before is None and rank <= BEST_TOP + 1 - BEST_JUMP:
        return f"Nousi top {BEST_TOP}:n ulkopuolelta vahintaan {BEST_TOP + 1 - rank} sijaa"
    return None


def check_best_sellers(state, catalog, now):
    """Paivittaa kauppojen top 20:n ja palauttaa postattavat nousut.

    state = {"checked": aika, "stores": {kauppa: {"ranks": {handle: sija}}
                                          tai {"unsupported": aika}}}
    Ensimmainen onnistunut haku per kauppa on pohjadata. Kauppa joka ei
    tue jarjestysta (tai jolla on alle 3 tuotetta) ohitetaan hiljaa ja
    tarkistetaan uudelleen BEST_RECHECK:n paasta.
    """
    stores = state.setdefault("stores", {})
    for s in [s for s in stores if s not in STORES]:
        del stores[s]
    alerts, stats = [], Counter()
    for store, products in catalog.items():
        prev = stores.get(store)
        if prev and "unsupported" in prev:
            since = _parse_time(prev["unsupported"])
            if since and now - since < BEST_RECHECK:
                stats["ohitettu"] += 1
                continue
            prev = None
        by_handle = {p.get("handle"): p for p in products}
        try:
            best = collection_handles(store, "best-selling")
            if prev is None:
                titles = {h: p.get("title") or "" for h, p in by_handle.items()}
                if not best or len(best) < 3 or not sorting_supported(store, best, titles):
                    stores[store] = {"unsupported": _iso(now)}
                    stats["ei tue jarjestysta"] += 1
                    continue
        except Exception:
            stats["haku epaonnistui"] += 1        # hiljaa: vanha lista jaa voimaan
            continue
        if not best:
            stats["haku epaonnistui"] += 1        # hetkellinen: vanha lista jaa voimaan
            continue

        ranks = {h: i + 1 for i, h in enumerate(best)}
        stores[store] = {"ranks": ranks}
        if prev is None:
            stats["pohjadata"] += 1
            continue
        small = len(best) < BEST_MIN_LISTED
        stats["seurattu (pieni)" if small else "seurattu"] += 1
        for handle, rank in ranks.items():
            p = by_handle.get(handle)
            if p is None or is_service(p):        # kassalisat myyvat aina karkea
                continue
            before = prev["ranks"].get(handle)
            reason = rise_reason(rank, before, _parse_time(p.get("published_at")), now, small)
            if reason:
                alerts.append({"store": store, "product": p, "rank": rank,
                               "before": before, "reason": reason})
    state["checked"] = _iso(now)
    print("[best-sellerit] " + ", ".join(f"{k}: {v}" for k, v in sorted(stats.items()))
          + f", nousuja {len(alerts)}")
    return alerts


def build_best_card(alert, hashes, now):
    """Nousukortti. Palauttaa (payload, kuuma)."""
    store, p = alert["store"], alert["product"]
    pid = str(p.get("id"))
    entry = hashes["products"].get(store, {}).get(pid)
    others = None                               # ei tiedossa ennen tiivistetta
    if entry and entry.get("hash"):
        others = [m for m in find_matches(store, pid, hashes["products"]) if _is_strong(m)]
    hot = alert["rank"] <= BEST_HOT and bool(others)

    published = _parse_time(p.get("published_at"))
    age = f" ({(now - published).total_seconds() / 86400:.1f} pv sitten)" if published else ""
    if others is None:
        shared = "ei viela tiivistetty"
    elif others:
        shared = f"{len(others)}: " + ", ".join(_host(m["store"]) for m in others[:5])
        if len(others) > 5:
            shared += f" +{len(others) - 5}"
    else:
        shared = "0"
    price = _fmt_money(_prices(p.get("variants") or [], "price"),
                       STORES.get(store, "$")) or "?"
    embed = {
        "title": _clip(("KUUMA: " if hot else "") + (p.get("title") or "(nimeton tuote)"), 256),
        "url": _product_url(store, p.get("handle", "")),
        "color": 0xE74C3C if hot else 0x3498DB,
        "description": alert["reason"],
        "fields": [
            {"name": "Hinta", "value": price, "inline": True},
            {"name": "Kauppa", "value": _host(store), "inline": True},
            {"name": "Sija nyt", "value": str(alert["rank"]), "inline": True},
            {"name": "Edellinen sija",
             "value": str(alert["before"]) if alert["before"] else f"yli {BEST_TOP}",
             "inline": True},
            {"name": "Julkaistu", "value": _fmt_date(p.get("published_at")) + age,
             "inline": True},
            {"name": "Muissa kaupoissa", "value": shared, "inline": True},
        ],
    }
    image = _first_image(p)
    if image:
        embed["thumbnail"] = {"url": image}
    return {"embeds": [embed]}, hot


def post_best_sellers(alerts, hashes, now, dry_run=False):
    """Kaikki nousut #salesiin, KUUMA-kortit lisaksi #trendsiin."""
    for alert in alerts:
        payload, hot = build_best_card(alert, hashes, now)
        targets = ["SALES_WEBHOOK"] + (["TRENDS_WEBHOOK"] if hot else [])
        for env in targets:
            if dry_run:
                _print_payload(env, payload)
                continue
            webhook = os.environ.get(env)
            if not webhook:
                print(f"[best-sellerit] {env} puuttuu, korttia ei postata")
                continue
            try:
                post_webhook(webhook, payload)
                time.sleep(1)  # Discordin rate limit
            except Exception as e:
                print(f"[webhook-virhe] {env}: {describe_error(e)}")


def send_summaries(matches_log, products, summary, now, dry_run=False):
    """2 tunnin trendikooste. summary kertoo milloin se viimeksi
    LAHETETTIIN; se paivittyy vain lahetyksesta, joten tilatiedosto ei muutu
    hiljaisina tunteina. Ei viestia jos uusia osumia ei tullut."""
    if not _is_due(summary, "trends", TRENDS_EVERY, now):
        return
    last = _parse_time(summary.get("trends")) or now - TRENDS_EVERY
    if not any((_parse_time(r.get("time")) or last) > last for r in matches_log):
        return
    payloads = build_digest(matches_log, products, now, since=last)
    if payloads is None:
        return
    if dry_run:
        for payload in payloads:
            _print_payload("TRENDS_WEBHOOK", payload)
        return
    webhook = os.environ.get("TRENDS_WEBHOOK")
    if not webhook:
        print("[trends] TRENDS_WEBHOOK puuttuu, kooste ohitetaan")
        return
    try:
        for i, payload in enumerate(payloads):
            if i:
                time.sleep(1)                 # Discordin rate limit
            post_webhook(webhook, payload)
    except Exception as e:
        print(f"[webhook-virhe] TRENDS_WEBHOOK: {describe_error(e)}")
        return                                # seuraava kierros yrittaa uudelleen
    print("[trends] kooste lahetetty")
    summary["trends"] = _iso(now)


def check_all(seen, webhook=None, dry_run=False, now=None):
    round_start = time.monotonic()
    now = now or _utcnow()
    failed = []
    catalog = {}                 # taman kierroksen onnistuneet haut
    best_state = _load_json(BEST_SELLERS, {})
    missing = _load_json(MISSING_FILE, {})
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

        pruned = prune_seen(known, products, missing.setdefault(store, {}), now)

        if baseline:
            print(f"{store}: OK, {len(products)} tuotetta, "
                  f"pohjadata tallennettu ({took:.1f}s)")
        else:
            print(f"{store}: OK, {len(products)} tuotetta, "
                  f"{len(new)} uutta" + (f", {pruned} yli 24 h puuttunutta karsittu" if pruned else "")
                  + f" ({took:.1f}s)")

    done_new = update_image_hashes(hashes, catalog)
    found = process_new_matches(done_new, hashes, matches_log, now, dry_run=dry_run)
    print(f"[osumat] {len(done_new)} uutta tuotetta tiivistetty, "
          f"{found} ristiinkauppaosumaa")

    if _is_due(best_state, "checked", BEST_EVERY, now):
        alerts = check_best_sellers(best_state, catalog, now)
        post_best_sellers(alerts, hashes, now, dry_run=dry_run)

    matches_log = prune(matches_log, now, MATCH_RETENTION)
    summary = _load_json(SUMMARY_STATE, {})
    send_summaries(matches_log, hashes["products"], summary, now, dry_run=dry_run)

    if dry_run:
        print("[dry-run] tilatiedostoja ei kirjoiteta")
    else:
        save_seen(seen)
        # Kuten seen.json: poistettujen kauppojen tila ei jaa roikkumaan.
        _save_json(IMAGE_HASHES, hashes)
        _save_json(BEST_SELLERS, best_state)
        _save_json(MISSING_FILE, {k: v for k, v in missing.items() if k in STORES and v})
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


def send_test_cards():
    """Lahettaa testikortin SALES_WEBHOOKiin ja TRENDS_WEBHOOKiin ja kertoo
    menikö läpi. Palauttaa True jos kaikki onnistuivat."""
    ok = True
    for env, color in (("SALES_WEBHOOK", 0x3498DB), ("TRENDS_WEBHOOK", 0xE67E22)):
        webhook = os.environ.get(env)
        if not webhook:
            print(f"[testi] {env}: PUUTTUU (ymparistomuuttuja tyhja)")
            ok = False
            continue
        payload = {"embeds": [{
            "title": "Testi, voit poistaa",
            "description": f"shopify-monitorin testikortti kanavalle {env}.",
            "color": color,
            "timestamp": _iso(_utcnow()),
        }]}
        try:
            post_webhook(webhook, payload)
            print(f"[testi] {env}: OK, kortti lahetetty")
        except Exception as e:
            print(f"[testi] {env}: EPAONNISTUI ({describe_error(e)})")
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description="Shopify-tuotemonitori")
    ap.add_argument("--once", action="store_true",
                    help="aja yksi kierros ja lopeta")
    ap.add_argument("--test-webhooks", action="store_true",
                    help="laheta testikortti SALES_WEBHOOKiin ja TRENDS_WEBHOOKiin ja lopeta")
    ap.add_argument("--dry-run", action="store_true",
                    help="tee kaikki muu paitsi ala posta Discordiin; "
                         "tulosta mita olisi postattu")
    args = ap.parse_args()

    if args.test_webhooks:
        sys.exit(0 if send_test_cards() else 1)

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
