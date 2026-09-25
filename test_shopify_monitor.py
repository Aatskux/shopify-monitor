#!/usr/bin/env python3
"""
Testit shopify_monitor.py:lle.

Nostaa pystyyn paikallisen valekaupan ja vale-Discordin, joten testit eivat
ota yhteytta oikeisiin kauppoihin eivatka postaa mitaan ulos.

Ajo: python3 -m unittest -v test_shopify_monitor.py
"""

import json
import os
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import shopify_monitor as m

# --- valekauppa ---------------------------------------------------------

def product(pid, title, variants, handle=None, image=None):
    p = {"id": pid, "title": title, "handle": handle or f"p{pid}", "variants": variants}
    if image:
        p["images"] = [{"src": image}]
    return p


CATALOG = [
    # monta varianttia, eri hinnat -> vaihteluvali
    product(1, "Range Hoodie", [
        {"price": "49.00", "compare_at_price": "79.00", "available": True},
        {"price": "59.50", "compare_at_price": "89.00", "available": False},
        {"price": "45.00", "compare_at_price": None, "available": True},
    ], handle="range-hoodie", image="https://img.example/1.jpg"),
    # yksi variantti, compare_at_price nollattu -> ei vertailuhintakenttaa
    product(2, "Flat Tee", [
        {"price": "20.00", "compare_at_price": "0.00", "available": False},
    ], handle="flat-tee"),
    # ei variantteja lainkaan
    product(3, "No Variants", [], handle="nv"),
]

POSTED = []          # vale-Discordiin tulleet rungot
POSTED_PATHS = []    # ja mihin polkuun ne tulivat (eri kanavat)
WEBHOOK_STATUS = [200]
LIVE = []            # muokattava valekauppa uusille testeille
IMAGES = {}          # /img/<nimi> -> kuvan tavut
IMAGE_REQUESTS = []  # kuvapyyntojen polut kyselyineen
BEST_ORDER = {}      # kauppa -> handlet best-selling-jarjestyksessa
NO_SORT = set()      # kaupat jotka eivat noudata sort_by:ta
NO_META = set()      # kaupat joiden sivulla ei ole analytiikkadataa
COLLECTION_PAGE = [20]
COLLECTION_REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        page = 1
        if "page=" in self.path:
            page = int(self.path.split("page=")[1].split("&")[0])

        if path == "/products.json":
            self._json(200, {"products": CATALOG})
        elif path.startswith("/live") and path.endswith("/products.json"):
            # /live/<kauppa>/products.json -> LIVE-tuotteet joilla "shop" == <kauppa>
            shop = path.split("/")[2] if path.count("/") > 2 else ""
            self._json(200, {"products": [p for p in LIVE if p.get("_shop", "") == shop]})
        elif path.startswith("/live/") and path.endswith("/collections/all"):
            self._collection(path.split("/")[2])
        elif path.startswith("/img/"):
            IMAGE_REQUESTS.append(self.path)
            name = path[len("/img/"):]
            if name == "hidas.png":
                import time as _t
                _t.sleep(3)
            data = IMAGES.get(name)
            if data is None:
                self.send_response(404); self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/paged/products.json":
            # sivu 1 taynna (250), sivu 2 vajaa -> sivutuksen pitaa paattya
            if page == 1:
                self._json(200, {"products": [
                    product(1000 + i, f"Bulk {i}", [{"price": "10.00", "available": True}])
                    for i in range(m.PAGE_SIZE)]})
            else:
                self._json(200, {"products": [
                    product(2000 + i, f"Tail {i}", [{"price": "10.00", "available": True}])
                    for i in range(5)]})
        elif path == "/403/products.json":
            self.send_response(403); self.end_headers()
        elif path == "/404/products.json":
            self.send_response(404); self.end_headers()
        elif path == "/slow/products.json":
            import time as _t
            _t.sleep(3)
            self._json(200, {"products": []})
        else:
            self.send_response(404); self.end_headers()

    def _collection(self, shop):
        """Kokoelmasivu kuten Shopify: analytiikkadata var meta = {...};"""
        from urllib.parse import parse_qs, urlsplit
        COLLECTION_REQUESTS.append(self.path)
        q = parse_qs(urlsplit(self.path).query)
        sort, page = q.get("sort_by", [""])[0], int(q.get("page", ["1"])[0])
        items = [p for p in LIVE if p.get("_shop") == shop][::-1]   # oletus: uusin ensin
        if shop not in NO_SORT:
            if sort == "best-selling":
                order = BEST_ORDER.get(shop, [])
                items.sort(key=lambda p: order.index(p["handle"]) if p["handle"] in order
                           else len(order))
            elif sort == "title-ascending":
                items.sort(key=lambda p: p["title"].lower())
        size = COLLECTION_PAGE[0]
        chunk = items[(page - 1) * size:page * size]
        meta = {"products": [{"id": p["id"], "handle": p["handle"], "vendor": "x"}
                             for p in chunk], "page": {"pageType": "collection"}}
        script = "" if shop in NO_META else f"var meta = {json.dumps(meta)};\n"
        body = f"<html><script>window.x = 1;\n{script}for (var a in meta) {{}}</script></html>"
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        POSTED.append(json.loads(self.rfile.read(n) or b"{}"))
        POSTED_PATHS.append(self.path)
        code = WEBHOOK_STATUS[0]
        if code == 429:
            WEBHOOK_STATUS[0] = 200      # toinen yritys onnistuu
            self._json(429, {"retry_after": 0.1})
        else:
            self.send_response(204); self.end_headers()

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        # timeout-testi katkaisee yhteyden kesken vastauksen; ei kohinaa siita
        pass


SAVED = ["STORES", "STORES_FILE", "STATE", "TIMEOUT", "BEST_SELLERS", "IMAGE_HASHES",
         "MATCHES_FILE", "SUMMARY_STATE", "IMAGE_TIMEOUT", "BEST_MIN_LISTED",
         "MAX_IMAGE_DOWNLOADS", "IMAGE_BUDGET", "fetch_products"]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_port
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        for lst in (POSTED, POSTED_PATHS, LIVE, IMAGE_REQUESTS, COLLECTION_REQUESTS):
            lst.clear()
        for coll in (IMAGES, BEST_ORDER, NO_SORT, NO_META):
            coll.clear()
        COLLECTION_PAGE[0] = 20
        WEBHOOK_STATUS[0] = 200
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = pathlib.Path(self.tmp.name)

        self._saved = {k: getattr(m, k) for k in SAVED}
        m.STORES = {self.base: "$"}
        m.STORES_FILE = tmp / "stores.json"
        m.STORES_FILE.write_text(json.dumps(m.STORES))
        m.STATE = tmp / "seen.json"
        m.BEST_SELLERS = tmp / "best_sellers.json"
        m.IMAGE_HASHES = tmp / "image_hashes.json"
        m.MATCHES_FILE = tmp / "matches.json"
        m.SUMMARY_STATE = tmp / "summary_state.json"
        m.TIMEOUT = 2
        m.IMAGE_TIMEOUT = 2
        self.addCleanup(self._restore)

        # Kehittajan omat webhookit eivat saa vuotaa testeihin.
        for env in ("SALES_WEBHOOK", "TRENDS_WEBHOOK"):
            old = os.environ.pop(env, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, env, old)
            self.addCleanup(os.environ.pop, env, None)

    def _restore(self):
        for k, v in self._saved.items():
            setattr(m, k, v)

    @property
    def webhook(self):
        return f"{self.base}/webhook"


# --- embedin sisalto ----------------------------------------------------

class TestEmbed(Base):
    def embed(self, idx):
        return m.build_embed(self.base, CATALOG[idx])

    def field(self, embed, name):
        for f in embed["fields"]:
            if f["name"] == name:
                return f["value"]
        return None

    def test_hinta_on_vaihteluvali_kaikista_varianteista(self):
        # ei variants[0] (= 49.00) vaan min-max koko listasta
        self.assertEqual(self.field(self.embed(0), "Hinta"), "$45.00 - $59.50")

    def test_yksi_hinta_ei_nayta_valia(self):
        self.assertEqual(self.field(self.embed(1), "Hinta"), "$20.00")

    def test_compare_at_price_on_oma_kentta(self):
        self.assertEqual(self.field(self.embed(0), "Vertailuhinta"), "$79.00 - $89.00")

    def test_nollattu_compare_at_price_jatetaan_pois(self):
        self.assertIsNone(self.field(self.embed(1), "Vertailuhinta"))

    def test_varastossa_olevien_varianttien_maara(self):
        self.assertEqual(self.field(self.embed(0), "Varastossa"), "2/3")
        self.assertEqual(self.field(self.embed(1), "Varastossa"), "0/1")

    def test_varianttien_maara(self):
        self.assertEqual(self.field(self.embed(0), "Variantteja"), "3")

    def test_valuutta_tulee_stores_sanakirjasta(self):
        m.STORES = {self.base: "€"}
        self.assertTrue(self.field(self.embed(0), "Hinta").startswith("€"))

    def test_tuote_ilman_variantteja_ei_kaada(self):
        e = self.embed(2)
        self.assertEqual(self.field(e, "Hinta"), "?")
        self.assertEqual(self.field(e, "Varastossa"), "0/0")

    def test_url_ja_kuva(self):
        e = self.embed(0)
        self.assertEqual(e["url"], f"{self.base}/products/range-hoodie")
        self.assertEqual(e["thumbnail"]["url"], "https://img.example/1.jpg")


# --- dry-run ------------------------------------------------------------

class TestDryRun(Base):
    def test_dry_run_ei_posta_eika_kirjoita_tilaa(self):
        seen = {self.base: set()}          # ei baseline -> postaisi oikeasti
        m.check_all(seen, webhook=self.webhook, dry_run=True)
        self.assertEqual(POSTED, [], "dry-run ei saa postata Discordiin")
        self.assertFalse(m.STATE.exists(), "dry-run ei saa kirjoittaa seen.jsonia")

    def test_dry_run_kay_lapi_kaikki_tuotteet(self):
        seen = {self.base: set()}
        m.check_all(seen, dry_run=True)
        # kaikki kolme tuotetta merkittiin nahdyiksi muistissa
        self.assertEqual(seen[self.base], {"1", "2", "3"})

    def test_dry_run_ei_vaadi_webhookia(self):
        os.environ.pop("DISCORD_WEBHOOK", None)
        m.check_all({self.base: set()}, webhook=None, dry_run=True)
        self.assertEqual(POSTED, [])


# --- oikea postaus ------------------------------------------------------

class TestPosting(Base):
    def test_postaa_uudet_tuotteet_ja_tallentaa_tilan(self):
        seen = {self.base: set()}
        m.check_all(seen, webhook=self.webhook, dry_run=False)
        self.assertEqual(len(POSTED), 3)
        titles = [b["embeds"][0]["title"] for b in POSTED]
        self.assertEqual(titles, ["Range Hoodie", "Flat Tee", "No Variants"])
        self.assertEqual(json.loads(m.STATE.read_text())[self.base], ["1", "2", "3"])

    def test_ei_posta_samoja_uudestaan(self):
        seen = {self.base: {"1", "2", "3"}}
        m.check_all(seen, webhook=self.webhook, dry_run=False)
        self.assertEqual(POSTED, [])

    def test_ensimmainen_kierros_ei_spammaa(self):
        m.check_all({}, webhook=self.webhook, dry_run=False)   # baseline
        self.assertEqual(POSTED, [], "baseline-kierros ei saa postata")
        self.assertEqual(json.loads(m.STATE.read_text())[self.base], ["1", "2", "3"])

    def test_discordin_429_yritetaan_uudelleen(self):
        WEBHOOK_STATUS[0] = 429
        m.notify(self.webhook, self.base, CATALOG[1])
        self.assertEqual(len(POSTED), 2, "429:n jalkeen pitaa yrittaa uudelleen")


# --- virhetilanteet -----------------------------------------------------

class TestErrors(Base):
    def run_stores(self, stores):
        m.STORES = stores
        return dict(m.check_all({k: set() for k in stores}, dry_run=True))

    def test_403_404_ja_timeout_raportoidaan(self):
        failed = self.run_stores({
            f"{self.base}/403": "$",
            f"{self.base}/404": "$",
            f"{self.base}/slow": "$",
        })
        self.assertEqual(failed[f"{self.base}/403"], "HTTP 403")
        self.assertEqual(failed[f"{self.base}/404"], "HTTP 404")
        self.assertEqual(failed[f"{self.base}/slow"], "timeout")

    def test_yksi_kaatunut_kauppa_ei_pysayta_muita(self):
        m.STORES = {f"{self.base}/404": "$", self.base: "$"}
        seen = {f"{self.base}/404": set(), self.base: set()}
        failed = m.check_all(seen, webhook=self.webhook, dry_run=False)
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(POSTED), 3, "toimiva kauppa pitaa silti kasitella")

    def test_rikkinaisesta_seen_jsonista_toivutaan(self):
        m.STATE.write_text("{ tama ei ole jsonia")
        self.assertEqual(m.load_seen(), {})

    def test_webhook_virhe_ei_kaada_ajoa(self):
        seen = {self.base: set()}
        m.check_all(seen, webhook="http://127.0.0.1:1/webhook", dry_run=False)
        self.assertTrue(m.STATE.exists(), "tila pitaa tallentua webhook-virheista huolimatta")


# --- tila ja sivutus ----------------------------------------------------

class TestState(Base):
    def test_sivutus_hakee_kaikki_sivut(self):
        products = m.fetch_products(f"{self.base}/paged")
        self.assertEqual(len(products), m.PAGE_SIZE + 5)

    def test_poistetut_kaupat_siivotaan_tilasta(self):
        m.save_seen({self.base: {"1"}, "https://poistettu.example": {"9"}})
        self.assertEqual(list(json.loads(m.STATE.read_text())), [self.base])

    def test_tila_sailyy_kierrosten_yli(self):
        seen = m.load_seen()
        m.check_all(seen, webhook=self.webhook, dry_run=False)     # baseline
        m.check_all(m.load_seen(), webhook=self.webhook, dry_run=False)
        self.assertEqual(POSTED, [], "toinen kierros ei saa postata samoja")


# --- komentoriviliittyma ------------------------------------------------

class TestCli(Base):
    def test_once_lopettaa_yhden_kierroksen_jalkeen(self):
        import sys
        argv = sys.argv
        sys.argv = ["shopify_monitor.py", "--once", "--dry-run"]
        try:
            m.main()          # jos --once ei toimi, tama jaa ikuiseen silmukkaan
        finally:
            sys.argv = argv

    def test_ilman_dry_runia_puuttuva_webhook_pysayttaa_selkeasti(self):
        import sys
        argv, env = sys.argv, os.environ.pop("DISCORD_WEBHOOK", None)
        sys.argv = ["shopify_monitor.py", "--once"]
        try:
            with self.assertRaises(SystemExit):
                m.main()
        finally:
            sys.argv = argv
            if env is not None:
                os.environ["DISCORD_WEBHOOK"] = env

    def test_koodissa_ei_ole_kovakoodattua_discord_urlia(self):
        src = pathlib.Path(m.__file__).read_text()
        for line in src.splitlines():
            if "discord.com/api/webhooks/" in line and "DISCORD_WEBHOOK=" not in line:
                self.fail(f"kovakoodattu webhook-URL: {line.strip()}")

    def test_stores_on_siisti(self):
        stores = m.load_stores(REPO_STORES)
        for url in stores:
            self.assertFalse(url.endswith("/"), f"kauttaviiva lopussa: {url}")
            self.assertTrue(url.startswith("https://"), f"ei https: {url}")
            self.assertEqual(url, url.lower(), f"isoja kirjaimia: {url}")
            self.assertTrue(stores[url], f"valuuttamerkki puuttuu: {url}")

    def test_seen_json_vastaa_stores_listaa(self):
        state = pathlib.Path(m.__file__).parent / "seen.json"
        if not state.exists():
            self.skipTest("seen.json puuttuu")
        keys = set(json.loads(state.read_text()))
        self.assertEqual(keys - set(m.load_stores(REPO_STORES)), set(),
                         "seen.jsonissa tuntemattomia kauppoja")

    def test_main_lukee_kaupat_stores_jsonista(self):
        import sys
        m.STORES = {}
        argv = sys.argv
        sys.argv = ["shopify_monitor.py", "--once", "--dry-run"]
        try:
            m.main()
        finally:
            sys.argv = argv
        self.assertEqual(m.STORES, {self.base: "$"})


# --- stores.json --------------------------------------------------------

REPO_STORES = pathlib.Path(m.__file__).parent / "stores.json"


class TestStoresFile(Base):
    def test_lukee_kaupat_ja_valuutat(self):
        m.STORES_FILE.write_text(json.dumps({"https://a.example": "$", "https://b.example": "€"}))
        self.assertEqual(m.load_stores(), {"https://a.example": "$", "https://b.example": "€"})

    def test_puuttuva_tiedosto_pysayttaa_selkeasti(self):
        m.STORES_FILE.unlink()
        with self.assertRaises(SystemExit):
            m.load_stores()

    def test_rikkinainen_json_pysayttaa_selkeasti(self):
        m.STORES_FILE.write_text("{ ei jsonia")
        with self.assertRaises(SystemExit):
            m.load_stores()

    def test_tyhja_lista_pysayttaa_selkeasti(self):
        # tyhjalla listalla save_seen tyhjentaisi koko seen.jsonin
        m.STORES_FILE.write_text("{}")
        with self.assertRaises(SystemExit):
            m.load_stores()

    def test_koodissa_ei_ole_kovakoodattua_kauppalistaa(self):
        src = pathlib.Path(m.__file__).read_text()
        self.assertNotIn("avellalane.com", src)


# --- yhteiset apuvalineet uusille testeille ------------------------------

import io
import random
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def png(seed, size=256):
    """Satunnainen mutta toistettava kuva: sama seed = sama kuva."""
    rnd = random.Random(seed)
    im = Image.new("RGB", (256, 256), "white")
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rnd.randrange(256), rnd.randrange(256)
        d.rectangle([x, y, x + rnd.randrange(20, 120), y + rnd.randrange(20, 120)],
                    fill=tuple(rnd.randrange(256) for _ in range(3)))
    if size != 256:
        im = im.resize((size, size))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class LiveBase(Base):
    """Kolme valekauppaa (a, b, c) jotka lukevat LIVE-listaa."""

    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = (f"{self.base}/live/{s}" for s in "abc")
        m.STORES = {self.a: "$", self.b: "$", self.c: "€"}

    def add(self, shop, pid, title, image=None, published=None, variants=None):
        p = {"_shop": shop, "id": pid, "title": title, "handle": f"h{pid}",
             "published_at": published or m._iso(T0 - timedelta(days=1)),
             "variants": variants or [{"id": pid * 10, "title": "M", "price": "30.00",
                                       "available": True}]}
        if image:
            IMAGES.setdefault(f"{image}.png", png(image))
            p["images"] = [{"src": f"{self.base}/img/{image}.png?v=1"}]
        LIVE.append(p)
        return p

    def round(self, now=T0, dry_run=False):
        return m.check_all(m.load_seen(), webhook=self.webhook, dry_run=dry_run, now=now)

    def state(self, path):
        return json.loads(path.read_text()) if path.exists() else None

    def posted(self, path):
        return [b for b, p in zip(POSTED, POSTED_PATHS) if p == path]

    def cards(self):
        """Heti postatut osumakortit (ei 2 h koosteen viesteja)."""
        return [b for b in self.posted("/trends")
                if b["embeds"][0]["title"].startswith("Sama tuote")]


# --- kuvatiivisteet ja nimet --------------------------------------------

class TestImageHash(LiveBase):
    def test_kuva_haetaan_pienennettyna(self):
        IMAGES["x.png"] = png(1)
        m.image_hash(f"{self.base}/img/x.png?v=123")
        self.assertEqual(IMAGE_REQUESTS, ["/img/x.png?v=123&width=256"])
        m.image_hash(f"{self.base}/img/x.png")
        self.assertEqual(IMAGE_REQUESTS[-1], "/img/x.png?width=256")

    def test_sama_kuva_eri_koossa_on_lahella_eri_kuva_kaukana(self):
        IMAGES.update({"a.png": png(1), "b.png": png(1, size=600), "c.png": png(2)})
        ha, hb, hc = (m.image_hash(f"{self.base}/img/{n}.png") for n in "abc")
        self.assertLessEqual(m.hamming(ha, hb), m.HASH_MAX_DISTANCE)
        self.assertGreater(m.hamming(ha, hc), m.HASH_MAX_DISTANCE)

    def test_nimen_normalisointi(self):
        self.assertEqual(m.normalize_name("VIRAL Trending 2026 NEW Hoodie™ — Best SALE!"),
                         "hoodie")
        self.assertEqual(m.normalize_name("Snoopy Shoulder-Bag"), "snoopy shoulder bag")

    def classify(self, name_a, name_b, hash_a=None, hash_b=None):
        a, b = {"name": name_a, "hash": hash_a}, {"name": name_b, "hash": hash_b}
        r = m.classify(a, b, lambda e: m.normalize_name(e["name"]))
        return r and r["strength"]

    def test_osuman_vahvuus(self):
        h = "c3d4e5f6a7b8c9d0"
        limit = m.HASH_MAX_DISTANCE
        near = format(int(h, 16) ^ (2 ** limit - 1), "016x")         # juuri rajalla
        far = format(int(h, 16) ^ (2 ** (limit + 1) - 1), "016x")    # bitin yli
        self.assertEqual(m.hamming(h, near), limit)
        self.assertEqual(self.classify("Snoopy Shoulder Bag", "Snoopy Bag Brown", h, near), "varma")
        self.assertEqual(self.classify("Cargo Pants", "Denim Jacket", h, h), "vahva")
        self.assertEqual(self.classify("Last Supper Hoodie", "Viral Last Supper Hoodie", h, far),
                         "mahdollinen")
        self.assertIsNone(self.classify("Cargo Pants", "Denim Jacket", h, far))

    def test_pelkka_osajoukkonimi_ei_riita(self):
        # token_set_ratio = 100, mutta nimet eivat ole oikeasti samat
        self.assertIsNone(self.classify("2009 Jacket", "Dean Winchester Leather Jacket"))


# --- ristiinkauppaosumat ------------------------------------------------

class TestMatching(LiveBase):
    def setUp(self):
        super().setUp()
        os.environ["TRENDS_WEBHOOK"] = f"{self.base}/trends"
        self.add("a", 1, "Snoopy Shoulder Bag", image=101,
                 published=m._iso(T0 - timedelta(days=3)))
        self.add("a", 2, "Plain Tee", image=102)
        self.round()                                  # pohjadata

    def test_pohjadata_ei_postaa_vanhoista_tuotteista(self):
        LIVE.clear()
        for shop in "ab":                             # sama tuote kahdessa kaupassa
            self.add(shop, 7 if shop == "a" else 8, "Twin Hoodie", image=107)
        m.STORES = {self.a: "$", self.b: "$"}
        for f in (m.STATE, m.IMAGE_HASHES, m.MATCHES_FILE):
            f.unlink(missing_ok=True)                 # alusta kuin uusi asennus
        self.round()
        self.assertEqual(POSTED, [])
        self.assertEqual(self.state(m.MATCHES_FILE), [])
        hashes = self.state(m.IMAGE_HASHES)["products"]
        self.assertIsNotNone(hashes[self.a]["7"]["hash"], "pohjadata tiivistetaan silti")

    def test_tiivisteen_tiedot_tallentuvat(self):
        e = self.state(m.IMAGE_HASHES)["products"][self.a]["1"]
        self.assertEqual(len(e["hash"]), 16)
        self.assertEqual(e["name"], "Snoopy Shoulder Bag")
        self.assertEqual(e["published_at"], m._iso(T0 - timedelta(days=3)))

    def test_uusi_sama_tuote_toisessa_kaupassa_postataan_heti(self):
        self.add("b", 5, "Snoopy Shoulder Bag Brown", image=101,
                 published=m._iso(T0 - timedelta(hours=2)))
        self.round(T0 + timedelta(minutes=5))
        [card] = self.cards()
        embed = card["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(fields["Kaupoissa yhteensa"], "2")
        self.assertEqual(fields["Osuman vahvuus"], "varma")
        self.assertIn("Sama tuote 2 kaupassa", embed["title"])
        self.assertEqual(embed["url"], f"{self.b}/products/h5")
        self.assertIn(m._host(self.a), embed["description"])
        self.assertIn(m._host(self.b), embed["description"])
        self.assertIn("julkaistu 22.09.2026", embed["description"])     # vanhan julkaisu
        self.assertIn("julkaistu 25.09.2026 10:00", embed["description"])
        self.assertTrue(embed["thumbnail"]["url"].startswith(f"{self.base}/img/101.png"))

    def test_kolmas_kauppa_tuottaa_uuden_kortin_paivitetylla_maaralla(self):
        self.add("b", 5, "Snoopy Shoulder Bag", image=101)
        self.round(T0 + timedelta(minutes=5))
        self.add("c", 9, "Snoopy Bag", image=101)
        self.round(T0 + timedelta(minutes=10))
        counts = [{f["name"]: f["value"] for f in b["embeds"][0]["fields"]}["Kaupoissa yhteensa"]
                  for b in self.cards()]
        self.assertEqual(counts, ["2", "3"])

    def test_vain_kuva_on_vahva_osuma(self):
        self.add("b", 5, "Totally Different Name", image=101)
        self.round(T0 + timedelta(minutes=5))
        [card] = self.cards()
        self.assertIn("vahva", card["embeds"][0]["description"])

    def test_mahdollinen_osuma_ei_tule_heti_vaan_koosteeseen(self):
        self.add("b", 5, "Snoopy Shoulder Bag", image=555)    # nimi sama, kuva ei
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(self.cards(), [], "pelkka nimiosuma ei saa tulla heti")
        [rec] = self.state(m.MATCHES_FILE)
        self.assertEqual(rec["strength"], "mahdollinen")
        # 2 h kooste: vain "tarkista itse" -osio, ei top-ryhmia
        [digest] = self.posted("/trends")
        embed = digest["embeds"][0]
        self.assertEqual(embed["title"], "Mahdolliset osumat, tarkista itse (1)")
        self.assertIn(f"{self.a}/products/h1", embed["description"])
        self.assertIn(f"{self.b}/products/h5", embed["description"])

    def test_kortissa_vain_kuvaosumat(self):
        self.add("b", 5, "Snoopy Shoulder Bag", image=555)    # mahdollinen
        self.round(T0 + timedelta(minutes=5))
        self.add("c", 9, "Snoopy Shoulder Bag", image=101)    # kuva a:n kanssa
        self.round(T0 + timedelta(minutes=10))
        [card] = self.cards()
        embed = card["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(fields["Kaupoissa yhteensa"], "2")
        self.assertIn(f"{self.a}/products/h1", embed["description"])
        self.assertNotIn(f"{self.b}/products", embed["description"])

    def test_saman_kaupan_tuotteita_ei_verrata(self):
        self.add("a", 3, "Snoopy Shoulder Bag", image=101)
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(self.cards(), [])

    def test_erilainen_tuote_ei_osu(self):
        self.add("b", 5, "Cargo Pants", image=999)
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(self.cards(), [])

    def test_kaksi_uutta_samaa_tuotetta_samalla_kierroksella_yksi_kortti(self):
        self.add("b", 5, "Brand New Thing", image=300)
        self.add("c", 6, "Brand New Thing", image=300)
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(len(self.state(m.MATCHES_FILE)), 2, "molemmat kirjataan")

    def test_kassalisia_ei_verrata(self):
        self.add("b", 5, "Shipping Protection", image=101)
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(self.cards(), [])
        self.assertNotIn(self.b, self.state(m.IMAGE_HASHES)["products"])

    def test_dry_run_tulostaa_eika_postaa_eika_kirjoita(self):
        before = m.IMAGE_HASHES.read_text()
        self.add("b", 5, "Snoopy Shoulder Bag", image=101)
        self.round(T0 + timedelta(minutes=5), dry_run=True)
        self.assertEqual(POSTED, [])
        self.assertEqual(m.IMAGE_HASHES.read_text(), before)

    def test_poistettu_tuote_siivotaan(self):
        del LIVE[1]
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(list(self.state(m.IMAGE_HASHES)["products"][self.a]), ["1"])


# --- kuvahaun rajat ja virheet ------------------------------------------

class TestImageLimits(LiveBase):
    def test_latauksia_enintaan_katon_verran_per_ajo(self):
        m.MAX_IMAGE_DOWNLOADS = 2
        for pid in range(1, 6):
            self.add("a", pid, f"Tuote {pid}", image=pid)
        self.round()
        self.assertEqual(len(IMAGE_REQUESTS), 2)
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(len(IMAGE_REQUESTS), 4, "loput jatkuvat seuraavalla kierroksella")
        self.assertEqual(len(self.state(m.IMAGE_HASHES)["products"][self.a]), 4)

    def test_uudet_tuotteet_ennen_pohjadataa(self):
        m.MAX_IMAGE_DOWNLOADS = 1
        self.add("a", 1, "Vanha", image=1)
        self.add("a", 2, "Vanha 2", image=2)
        m.MAX_IMAGE_DOWNLOADS = 0
        self.round()                                  # pohjadata ilman latauksia
        m.MAX_IMAGE_DOWNLOADS = 1
        self.add("b", 5, "Uusi", image=5)
        m.save_seen({**{k: set(v) for k, v in m.load_seen().items()}, self.b: set()})
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(IMAGE_REQUESTS, ["/img/5.png?v=1&width=256"])

    def test_epaonnistunut_ja_hidas_kuva_eivat_kaada_ajoa(self):
        m.IMAGE_TIMEOUT = 1
        self.add("a", 1, "Rikki", image=None)
        LIVE[-1]["images"] = [{"src": f"{self.base}/img/puuttuu.png"}]
        self.add("a", 2, "Hidas", image=None)
        IMAGES["hidas.png"] = png(1)
        LIVE[-1]["images"] = [{"src": f"{self.base}/img/hidas.png"}]
        self.add("a", 3, "Ehja", image=3)
        failed = self.round()
        self.assertEqual(failed, [])
        state = self.state(m.IMAGE_HASHES)
        self.assertEqual(list(state["products"][self.a]), ["3"])
        self.assertEqual(state["pending"][self.a]["1"]["tries"], 1)

    def test_epaonnistunut_kuva_luovutetaan_yritysten_jalkeen(self):
        self.add("a", 1, "Rikki")
        LIVE[-1]["images"] = [{"src": f"{self.base}/img/puuttuu.png"}]
        for i in range(m.IMAGE_MAX_TRIES):
            self.round(T0 + timedelta(minutes=5 * i))
        state = self.state(m.IMAGE_HASHES)
        self.assertIsNone(state["products"][self.a]["1"]["hash"])
        self.assertNotIn(self.a, state["pending"])
        self.round(T0 + timedelta(minutes=30))
        self.assertEqual(len(IMAGE_REQUESTS), m.IMAGE_MAX_TRIES, "ei yriteta ikuisesti")

    def test_tuote_ilman_kuvaa_ei_lataa_mitaan(self):
        self.add("a", 1, "Ei kuvaa")
        self.round()
        self.assertEqual(IMAGE_REQUESTS, [])
        self.assertIsNone(self.state(m.IMAGE_HASHES)["products"][self.a]["1"]["hash"])


# --- tilatiedostot eivat muutu turhaan ----------------------------------

class TestStableState(LiveBase):
    FILES = ("STATE", "IMAGE_HASHES", "MATCHES_FILE", "SUMMARY_STATE", "BEST_SELLERS")

    def snapshot(self):
        out = {}
        for name in self.FILES:
            f = getattr(m, name)
            if name == "BEST_SELLERS":
                # tallennetaan speksin mukaan kerran tunnissa: vain "checked"
                # saa muuttua, listat eivat jos jarjestys ei muutu
                data = json.loads(f.read_text())
                data.pop("checked")
                out[name] = data
                continue
            out[name] = (f.read_text(), f.stat().st_mtime_ns) if f.exists() else None
        return out

    def test_muuttumaton_data_ei_muuta_yhtaan_tiedostoa(self):
        os.environ["SALES_WEBHOOK"] = f"{self.base}/sales"
        os.environ["TRENDS_WEBHOOK"] = f"{self.base}/trends"
        self.add("a", 1, "Snoopy Bag", image=1)
        self.add("b", 2, "Other", image=2)
        self.round()
        self.add("c", 3, "Snoopy Bag", image=1)        # osuma + kooste
        self.round(T0 + timedelta(minutes=5))
        self.assertTrue(POSTED, "tassa vaiheessa jotain pitaa lahtea")

        before = self.snapshot()
        for i in range(2, 40):                        # yli 3 h hiljaisia kierroksia
            self.round(T0 + timedelta(minutes=5 * i))
        after = self.snapshot()
        for name in self.FILES:
            self.assertEqual(before[name], after[name], f"{name} muuttui turhaan")


# --- 2 tunnin trendikooste ----------------------------------------------

class TestDigest(LiveBase):
    def entry(self, name, days_old):
        return {"hash": None, "name": name, "handle": name.lower(), "image": None,
                "price": "$10.00", "published_at": m._iso(T0 - timedelta(days=days_old))}

    def record(self, store, pid, others, when=T0):
        return {"time": m._iso(when), "store": store, "product_id": pid,
                "strength": "varma", "stores": len(others) + 1,
                "matches": [{"store": s, "product_id": p, "strength": "varma",
                             "distance": 0, "name_score": 100} for s, p in others]}

    def setUp(self):
        super().setUp()
        d = "https://d.example"
        self.products = {
            self.a: {"1": self.entry("Kolmessa", 20), "2": self.entry("Tuore", 0)},
            self.b: {"1": self.entry("Kolmessa", 20), "2": self.entry("Tuore", 1)},
            self.c: {"1": self.entry("Kolmessa", 20), "3": self.entry("Vanha", 30)},
            d: {"3": self.entry("Vanha", 30)},
        }
        self.log = [
            self.record(self.b, "1", [(self.a, "1")]),
            self.record(self.c, "1", [(self.a, "1"), (self.b, "1")]),
            self.record(self.b, "2", [(self.a, "2")]),
            self.record(d, "3", [(self.c, "3")]),
        ]

    def test_eniten_kauppoja_painotettuna_tuoreudella(self):
        rows = m.digest_rows(self.log, self.products, T0)
        names = [self.products[r["node"][0]][r["node"][1]]["name"] for r in rows]
        # Tuore (uusin julkaisu tanaan): 2 * (1 + 1) = 4.0; Kolmessa: 3 * 1 = 3.0;
        # Vanha: 2 * 1 = 2.0
        self.assertEqual(names, ["Tuore", "Kolmessa", "Vanha"])
        self.assertEqual([len(r["stores"]) for r in rows], [2, 3, 2])
        self.assertEqual([r["score"] for r in rows], [4.0, 3.0, 2.0])

    def test_top_10(self):
        log = [self.record(self.a, str(i), [(self.b, str(i))]) for i in range(15)]
        products = {s: {str(i): self.entry(f"T{i}", 1) for i in range(15)}
                    for s in (self.a, self.b)}
        self.assertEqual(len(m.build_top_groups(log, products, T0)["embeds"]), 10)

    def test_kooste_2h_valein_ja_vain_uusista_osumista(self):
        os.environ["TRENDS_WEBHOOK"] = f"{self.base}/trends"
        summary = {}
        m.send_summaries(self.log, self.products, summary, T0)
        self.assertEqual(len(POSTED), 1)
        self.assertTrue(POSTED[0]["content"].startswith("**Trendikooste"))
        # 2 h kuluttua, mutta ei uusia osumia -> ei viestia eika tilamuutosta
        m.send_summaries(self.log, self.products, summary, T0 + timedelta(hours=2))
        self.assertEqual(len(POSTED), 1)
        self.assertEqual(summary["trends"], m._iso(T0))
        # uusi osuma tunnin paasta -> odotetaan silti 2 h tahtiin
        log = self.log + [self.record(self.c, "3", [(self.a, "1")], T0 + timedelta(hours=1))]
        m.send_summaries(log, self.products, summary, T0 + timedelta(hours=1, minutes=5))
        self.assertEqual(len(POSTED), 1)
        m.send_summaries(log, self.products, summary, T0 + timedelta(hours=2, minutes=5))
        self.assertEqual(len(POSTED), 2)

    def possible(self, store, pid, other, opid, when=T0):
        rec = self.record(store, pid, [(other, opid)], when)
        rec["strength"] = rec["matches"][0]["strength"] = "mahdollinen"
        rec["matches"][0]["distance"] = 30
        return rec

    def test_mahdolliset_omana_osionaan(self):
        self.products[self.c]["9"] = self.entry("Nimitwin", 1)
        self.products[self.a]["9"] = self.entry("Nimitwin", 1)
        log = self.log + [self.possible(self.c, "9", self.a, "9")]
        top, possible = m.build_digest(log, self.products, T0)
        self.assertTrue(top["content"].startswith("**Trendikooste"))
        self.assertEqual(possible["embeds"][0]["title"],
                         "Mahdolliset osumat, tarkista itse (1)")
        self.assertIn("nimi 100", possible["embeds"][0]["description"])
        names = [self.products[s][p]["name"] for s, p in
                 (r["node"] for r in m.digest_rows(log, self.products, T0))]
        self.assertNotIn("Nimitwin", names, "mahdollinen ei muodosta trendiryhmaa")

    def test_mahdolliset_vain_jakson_ajalta(self):
        self.products[self.c]["9"] = self.entry("Nimitwin", 1)
        self.products[self.a]["9"] = self.entry("Nimitwin", 1)
        old = self.possible(self.c, "9", self.a, "9", T0 - timedelta(hours=3))
        self.assertEqual(len(m.build_digest(self.log + [old], self.products, T0)), 1)

    def test_mahdollinen_ei_yhdista_kuvaryhmia(self):
        # a1-b1-c1 on kuvaryhma; d3 vain nimella a1:n kanssa -> ryhma pysyy 3 kaupassa
        log = self.log + [self.possible("https://d.example", "3", self.a, "1")]
        rows = m.digest_rows(log, self.products, T0)
        self.assertEqual(sorted(len(r["stores"]) for r in rows), [2, 2, 3])

    def test_poistetut_tuotteet_eivat_ole_koosteessa(self):
        del self.products[self.b]["2"]
        rows = m.digest_rows(self.log, self.products, T0)
        self.assertNotIn("Tuore", [self.products[s][p]["name"] for s, p in
                                   (r["node"] for r in rows)])

# --- best-sellerit ------------------------------------------------------

class BestBase(LiveBase):
    """Kaupassa a on 25 tuotetta; best-selling-jarjestys ohjataan BEST_ORDERilla."""

    def setUp(self):
        super().setUp()
        os.environ["SALES_WEBHOOK"] = f"{self.base}/sales"
        os.environ["TRENDS_WEBHOOK"] = f"{self.base}/trends"
        m.STORES = {self.a: "$", self.b: "$"}
        old = m._iso(T0 - timedelta(days=30))
        for pid in range(1, 26):
            # nimet eri jarjestyksessa kuin myynti, muuten aakkostesti hylkaisi
            self.add("a", pid, f"Tuote {pid * 7 % 26:02d}", published=old)
        self.rank("a", range(1, 26))

    def rank(self, shop, pids):
        BEST_ORDER[shop] = [f"h{pid}" for pid in pids]

    def best(self):
        return self.state(m.BEST_SELLERS)

    def sales(self):
        return self.posted("/sales")

    def fields(self, body):
        return {f["name"]: f["value"] for f in body["embeds"][0]["fields"]}


class TestBestSellerFetch(BestBase):
    def test_jarjestys_luetaan_analytiikkadatasta(self):
        self.rank("a", [5, 3, 1])
        handles = m.collection_handles(self.a, "best-selling")
        self.assertEqual(handles[:3], ["h5", "h3", "h1"])
        self.assertEqual(len(handles), 20, "vain top 20")

    def test_sivutus_kun_sivukoko_pieni(self):
        COLLECTION_PAGE[0] = 8
        self.assertEqual(len(m.collection_handles(self.a, "best-selling")), 20)
        self.assertEqual(len(COLLECTION_REQUESTS), 3)

    def test_ilman_analytiikkadataa_none(self):
        NO_META.add("a")
        self.assertIsNone(m.collection_handles(self.a, "best-selling"))

    def test_jarjestyksen_tuki_tarkistetaan_aakkosilla(self):
        titles = {p["handle"]: p["title"] for p in LIVE}
        best = m.collection_handles(self.a, "best-selling")
        self.assertTrue(m.sorting_supported(self.a, best, titles))
        NO_SORT.add("a")
        best = m.collection_handles(self.a, "best-selling")
        self.assertFalse(m.sorting_supported(self.a, best, titles))


class TestBestSellers(BestBase):
    def setUp(self):
        super().setUp()
        self.round()                                   # pohjadata

    def test_pohjadata_ei_postaa_ja_tallentaa_top20(self):
        self.assertEqual(self.sales(), [])
        ranks = self.best()["stores"][self.a]["ranks"]
        self.assertEqual(len(ranks), 20)
        self.assertEqual((ranks["h1"], ranks["h20"]), (1, 20))
        self.assertNotIn("h21", ranks)

    def test_haku_kerran_tunnissa(self):
        n = len(COLLECTION_REQUESTS)
        self.round(T0 + timedelta(minutes=5))
        self.round(T0 + timedelta(minutes=30))
        self.assertEqual(len(COLLECTION_REQUESTS), n, "ei hakua saman tunnin sisalla")
        self.round(T0 + timedelta(minutes=58))         # cronin viive-etuajo sallittu
        self.assertGreater(len(COLLECTION_REQUESTS), n)

    def test_tuore_tuote_nousee_top_10(self):
        self.add("a", 30, "Uutuus", published=m._iso(T0 - timedelta(days=2)), image=30)
        self.rank("a", [1, 2, 30] + list(range(3, 26)))
        self.round(T0 + timedelta(hours=1))
        [body] = self.sales()
        f = self.fields(body)
        embed = body["embeds"][0]
        self.assertEqual(embed["title"], "Uutuus")
        self.assertEqual((f["Sija nyt"], f["Edellinen sija"]), ("3", "yli 20"))
        self.assertEqual(f["Hinta"], "$30.00")
        self.assertEqual(f["Kauppa"], m._host(self.a))
        self.assertIn("23.09.2026", f["Julkaistu"])
        self.assertIn("top 10", embed["description"])
        self.assertEqual(embed["url"], f"{self.a}/products/h30")
        self.assertTrue(embed["thumbnail"]["url"].startswith(f"{self.base}/img/30.png"))
        self.assertEqual(self.posted("/trends"), [], "ei muissa kaupoissa -> ei KUUMA")

    def test_vanha_tuote_pieni_nousu_top_10_ei_postaa(self):
        self.rank("a", [12] + [p for p in range(1, 26) if p != 12])   # 12 -> 1: 11 sijaa
        self.round(T0 + timedelta(hours=1))
        self.assertEqual(len(self.sales()), 1)
        self.rank("a", [12, 1, 14] + [p for p in range(2, 26) if p not in (12, 14)])  # 14: 13 -> 3 = 10
        self.round(T0 + timedelta(hours=2))
        self.assertEqual(len(self.sales()), 2)
        self.rank("a", [12, 1, 14, 3, 2] + [p for p in range(4, 26) if p not in (12, 14)])  # 3: 5 -> 4
        self.round(T0 + timedelta(hours=3))
        self.assertEqual(len(self.sales()), 2, "alle 10 sijan nousu vanhalle tuotteelle ei postaa")

    def test_nousu_vahintaan_10_sijaa(self):
        self.rank("a", [1, 2, 3, 4, 17] + [p for p in range(5, 26) if p != 17])
        self.round(T0 + timedelta(hours=1))
        [body] = self.sales()
        f = self.fields(body)
        self.assertEqual((f["Sija nyt"], f["Edellinen sija"]), ("5", "17"))
        self.assertEqual(body["embeds"][0]["description"], "Nousi 12 sijaa")

    def test_top_20_ulkopuolelta(self):
        self.rank("a", list(range(1, 11)) + [24] + list(range(11, 24)) + [25])   # 24 -> 11
        self.round(T0 + timedelta(hours=1))
        [body] = self.sales()
        self.assertEqual(self.fields(body)["Edellinen sija"], "yli 20")
        self.rank("a", list(range(1, 11)) + [24] + list(range(11, 16)) + [25] + list(range(16, 24)))
        self.round(T0 + timedelta(hours=2))                  # 25 ulkopuolelta sijalle 17
        self.assertEqual(len(self.sales()), 1, "ei varmaa 10 sijan nousua")

    def test_kuuma_myos_trendsiin(self):
        self.add("a", 30, "Snoopy Bag", published=m._iso(T0 - timedelta(days=1)), image=77)
        self.add("b", 40, "Snoopy Bag", image=77)            # sama kuva toisessa kaupassa
        self.round(T0 + timedelta(minutes=5))                # tiivisteet (b = pohjadataa)
        self.rank("a", [30] + list(range(1, 26)))
        self.round(T0 + timedelta(hours=1))
        [sales] = self.sales()
        hot = [b for b in self.posted("/trends") if b["embeds"][0]["title"].startswith("KUUMA")]
        self.assertEqual(len(hot), 1)
        self.assertEqual(sales, hot[0])
        self.assertEqual(sales["embeds"][0]["title"], "KUUMA: Snoopy Bag")
        self.assertTrue(self.fields(sales)["Muissa kaupoissa"].startswith("1: "))

    def test_ilman_tiivistetta_muissa_kaupoissa_ei_tiedossa(self):
        m.MAX_IMAGE_DOWNLOADS = 0
        self.add("a", 30, "Uutuus", published=m._iso(T0 - timedelta(days=1)), image=31)
        self.rank("a", [30] + list(range(1, 26)))
        self.round(T0 + timedelta(hours=1))
        [body] = self.sales()
        self.assertEqual(self.fields(body)["Muissa kaupoissa"], "ei viela tiivistetty")
        self.assertFalse(body["embeds"][0]["title"].startswith("KUUMA"))

    def test_kassalisat_eivat_ole_nousuja(self):
        self.add("a", 30, "Shipping Protection", published=m._iso(T0 - timedelta(days=1)))
        self.rank("a", [30] + list(range(1, 26)))
        self.round(T0 + timedelta(hours=1))
        self.assertEqual(self.sales(), [])

    def test_dry_run_ei_postaa_eika_tallenna(self):
        before = m.BEST_SELLERS.read_text()
        self.rank("a", [17] + [p for p in range(1, 26) if p != 17])
        self.round(T0 + timedelta(hours=1), dry_run=True)
        self.assertEqual(POSTED, [])
        self.assertEqual(m.BEST_SELLERS.read_text(), before)


class TestBestSellerSkips(BestBase):
    def test_jarjestysta_tukematon_kauppa_ohitetaan_hiljaa(self):
        NO_SORT.add("a")                       # oletusjarjestys: uusin ensin
        self.round()
        self.assertEqual(self.best()["stores"][self.a], {"unsupported": m._iso(T0)})
        self.add("a", 30, "Uusin", published=m._iso(T0))
        self.round(T0 + timedelta(hours=1))
        self.assertEqual(self.sales(), [], "uusin ensin -jarjestys ei saa nayttaa nousulta")

    def test_tukematon_tarkistetaan_uudelleen_viikon_paasta(self):
        NO_META.add("a")
        self.round()
        n = len(COLLECTION_REQUESTS)
        self.round(T0 + timedelta(days=3))
        self.assertEqual(len(COLLECTION_REQUESTS), n, "ei turhia hakuja")
        NO_META.clear()
        self.round(T0 + timedelta(days=8))
        self.assertIn("ranks", self.best()["stores"][self.a], "tuki loytyi -> pohjadata")
        self.assertEqual(self.sales(), [])

    def test_alle_3_tuotteen_kauppa_ohitetaan(self):
        del LIVE[2:]
        self.round()
        self.assertIn("unsupported", self.best()["stores"][self.a])


class TestSmallStore(BestBase):
    """Alle 15 tuotetta: vain tuore tuote sijoille 1-3."""

    def setUp(self):
        super().setUp()
        del LIVE[8:]                                   # 8 tuotetta
        LIVE[7]["published_at"] = m._iso(T0 - timedelta(days=2))   # tuore, sija 8
        self.rank("a", range(1, 9))
        self.round()                                   # pohjadata

    def test_pieni_kauppa_seurataan(self):
        self.assertEqual(len(self.best()["stores"][self.a]["ranks"]), 8)

    def test_tuore_tuote_nousee_sijoille_1_3(self):
        self.rank("a", [1, 8] + list(range(2, 8)))     # tuote 8: sija 8 -> 2
        self.round(T0 + timedelta(hours=1))
        [body] = self.sales()
        f = self.fields(body)
        self.assertEqual((f["Sija nyt"], f["Edellinen sija"]), ("2", "8"))
        self.assertIn("pieni kauppa", body["embeds"][0]["description"])

    def test_tuore_tuote_sijalle_4_ei_postaa(self):
        self.rank("a", [1, 2, 3, 8] + list(range(4, 8)))
        self.round(T0 + timedelta(hours=1))
        self.assertEqual(self.sales(), [])

    def test_juuri_julkaistu_suoraan_karkeen_ei_ole_nousu(self):
        # oikea havainto: myymattomien jarjestys on satunnainen, uusi tuote
        # voi ilmestya heti sijalle 1
        self.add("a", 30, "Uutuus", published=m._iso(T0 + timedelta(minutes=30)))
        self.rank("a", [30] + list(range(1, 9)))
        self.round(T0 + timedelta(hours=1))
        self.assertEqual(self.sales(), [])

    def test_vanha_tuote_ei_postaa_vaikka_nousee_karkeen(self):
        self.rank("a", [7] + [p for p in range(1, 9) if p != 7])
        self.round(T0 + timedelta(hours=1))
        self.assertEqual(self.sales(), [])

    def test_hakuvirhe_ei_kaada_ja_vanha_lista_jaa(self):
        self.round()
        ranks = self.best()["stores"][self.a]
        original = m.collection_handles
        m.collection_handles = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError())
        self.addCleanup(setattr, m, "collection_handles", original)
        failed = self.round(T0 + timedelta(hours=1))
        self.assertEqual(failed, [])
        self.assertEqual(self.best()["stores"][self.a], ranks)

    def test_rise_reason(self):
        fresh, old = T0 - timedelta(days=3), T0 - timedelta(days=30)
        self.assertIsNotNone(m.rise_reason(10, None, fresh, T0))
        self.assertIsNotNone(m.rise_reason(9, 14, fresh, T0))
        self.assertIsNone(m.rise_reason(8, 9, fresh, T0), "oli jo top 10:ssa")
        self.assertIsNone(m.rise_reason(12, None, fresh, T0), "tuore mutta ei top 10")
        self.assertEqual(m.rise_reason(5, 15, old, T0), "Nousi 10 sijaa")
        self.assertIsNone(m.rise_reason(3, None, fresh, T0, small=True), "ensiesiintyminen")
        self.assertIsNotNone(m.rise_reason(1, 5, fresh, T0, small=True))
        self.assertIsNone(m.rise_reason(2, 3, fresh, T0, small=True), "oli jo top 3:ssa")
        self.assertIsNone(m.rise_reason(4, 6, fresh, T0, small=True))
        self.assertIsNone(m.rise_reason(1, 15, old, T0, small=True), "vanha: ei hyppysaantoa")
        self.assertIsNone(m.rise_reason(6, 15, old, T0))
        self.assertIsNotNone(m.rise_reason(11, None, old, T0))
        self.assertIsNone(m.rise_reason(12, None, old, T0))

# --- seen.json:n karsinta ja webhook-testi --------------------------------

class TestSeenPruning(LiveBase):
    def setUp(self):
        super().setUp()
        m.STORES = {self.a: "$", self.b: "$"}
        for pid in range(1, 11):
            self.add("a", pid, f"Tuote {pid}")
        self.add("b", 50, "B-tuote")
        self.round()                                   # pohjadata

    def seen(self):
        return json.loads(m.STATE.read_text())

    def test_poistunut_tuote_karsitaan(self):
        del LIVE[0]                                    # tuote 1 poistui kaupasta
        self.round(T0 + timedelta(minutes=5))
        self.assertNotIn("1", self.seen()[self.a])
        self.assertEqual(len(self.seen()[self.a]), 9)

    def test_epaonnistunut_haku_ei_karsi(self):
        original = m.fetch_products

        def flaky(store):
            if store == self.a:
                raise requests.ConnectionError("katkos")
            return original(store)
        m.fetch_products = flaky
        del LIVE[:5]
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(len(self.seen()[self.a]), 10, "haku epaonnistui -> ei karsintaa")

    def test_tyhja_vastaus_ei_karsi(self):
        del LIVE[:10]                                  # kauppa a palauttaa tyhjan listan
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(len(self.seen()[self.a]), 10)

    def test_iso_osa_kadonnut_karsitaan(self):
        # oikeassa datassa 12 kauppaa on poistanut yli puolet tuotteistaan
        del LIVE[:8]
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(self.seen()[self.a], ["10", "9"])

    def test_karsittu_tuote_joka_palaa_on_uusi(self):
        removed = LIVE.pop(0)
        self.round(T0 + timedelta(minutes=5))
        LIVE.insert(0, removed)
        self.round(T0 + timedelta(minutes=10))
        titles = [b["embeds"][0]["title"] for b in POSTED]
        self.assertEqual(titles, ["Tuote 1"])

    def test_muiden_kauppojen_tila_sailyy(self):
        del LIVE[0]
        self.round(T0 + timedelta(minutes=5))
        self.assertEqual(self.seen()[self.b], ["50"])


class TestWebhookTest(Base):
    def test_testikortti_molempiin(self):
        os.environ["SALES_WEBHOOK"] = f"{self.base}/sales"
        os.environ["TRENDS_WEBHOOK"] = f"{self.base}/trends"
        self.assertTrue(m.send_test_cards())
        self.assertEqual(POSTED_PATHS, ["/sales", "/trends"])
        self.assertEqual({b["embeds"][0]["title"] for b in POSTED}, {"Testi, voit poistaa"})

    def test_puuttuva_tai_rikki_webhook_raportoidaan(self):
        os.environ["SALES_WEBHOOK"] = "http://127.0.0.1:1/sales"
        self.assertFalse(m.send_test_cards())
        self.assertEqual(POSTED, [])

    def test_cli_lopettaa_testin_jalkeen(self):
        import sys
        os.environ["SALES_WEBHOOK"] = f"{self.base}/sales"
        os.environ["TRENDS_WEBHOOK"] = f"{self.base}/trends"
        argv = sys.argv
        sys.argv = ["shopify_monitor.py", "--test-webhooks"]
        try:
            with self.assertRaises(SystemExit) as cm:
                m.main()
        finally:
            sys.argv = argv
        self.assertEqual(cm.exception.code, 0)
        self.assertFalse(m.STATE.exists(), "testi ei aja kierrosta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
