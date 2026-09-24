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
WEBHOOK_STATUS = [200]


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

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        POSTED.append(json.loads(self.rfile.read(n) or b"{}"))
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
        POSTED.clear()
        WEBHOOK_STATUS[0] = 200
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self._saved = m.STORES, m.STORES_FILE, m.STATE, m.TIMEOUT
        m.STORES = {self.base: "$"}
        m.STORES_FILE = pathlib.Path(self.tmp.name) / "stores.json"
        m.STORES_FILE.write_text(json.dumps(m.STORES))
        m.STATE = pathlib.Path(self.tmp.name) / "seen.json"
        m.TIMEOUT = 2
        self.addCleanup(self._restore)

    def _restore(self):
        m.STORES, m.STORES_FILE, m.STATE, m.TIMEOUT = self._saved

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
