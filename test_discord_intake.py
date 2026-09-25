#!/usr/bin/env python3
"""
Testit discord_intake.py:lle.

Nostaa pystyyn paikallisen vale-Discordin (viestit + reaktiot) ja
valekaupat, joten testit eivat ota yhteytta Discordiin eivatka oikeisiin
kauppoihin.

Ajo: python3 -m unittest -v test_discord_intake.py
"""

import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import discord_intake as d
import shopify_monitor as m

CHANNEL = "555"
TOKEN = "testitoken"

# --- vale-Discord ja valekaupat -----------------------------------------

MESSAGES = []        # kanavan viestit, kuten Discord ne palauttaa
REACTIONS = []       # (viesti-id, emoji) jarjestyksessa
RAW_REACTION_PATHS = []
REACTION_429 = [0]   # montako seuraavaa reaktiota saa 429:n
AUTH = []            # Discord-pyyntojen Authorization-otsakkeet
POSTED = []          # monitorin webhook-postaukset

# host -> kaupan tila: lista tuotteita = toimii, muuten virhetyyppi
SHOPS = {}


def msg(mid, content, bot=False):
    return {"id": str(mid), "content": content,
            "author": {"id": "1", "username": "x", "bot": bot}}


def products(*ids):
    return [{"id": i, "title": f"Tuote {i}", "handle": f"t{i}",
             "variants": [{"price": "10.00", "available": True}]} for i in ids]


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        query = parse_qs(url.query)

        if parts[:3] == ["api", "channels", CHANNEL] and parts[3:] == ["messages"]:
            AUTH.append(self.headers.get("Authorization"))
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["50"])[0])
            newer = sorted((x for x in MESSAGES if int(x["id"]) > after),
                           key=lambda x: int(x["id"]))[:limit]
            self._json(200, newer[::-1])      # Discord: uusin ensin
            return

        if parts[0] == "shop" and parts[-1] == "products.json":
            shop = SHOPS.get(parts[1], "404")
            if isinstance(shop, list):
                page = int(query.get("page", ["1"])[0])
                size = m.PAGE_SIZE
                self._json(200, {"products": shop[(page - 1) * size:page * size]})
            elif shop == "slow":
                time.sleep(3)
                self._json(200, {"products": []})
            elif shop == "html":
                b = b"<html>password</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif shop == "nokey":
                self._json(200, {"jotain": []})
            else:
                self._empty(int(shop))
            return

        self._empty(404)

    def do_PUT(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        # api/channels/{ch}/messages/{id}/reactions/{emoji}/@me
        if parts[:3] == ["api", "channels", CHANNEL] and parts[5] == "reactions":
            AUTH.append(self.headers.get("Authorization"))
            if REACTION_429[0]:
                REACTION_429[0] -= 1
                self._json(429, {"retry_after": 0.05, "global": False})
                return
            RAW_REACTION_PATHS.append(parts[6])
            REACTIONS.append((parts[4], unquote(parts[6])))
            self._empty(204)
            return
        self._empty(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        POSTED.append(json.loads(self.rfile.read(n) or b"{}"))
        self._empty(204)

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        for lst in (MESSAGES, REACTIONS, RAW_REACTION_PATHS, AUTH, POSTED):
            lst.clear()
        REACTION_429[0] = 0
        SHOPS.clear()

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = pathlib.Path(self.tmp.name)

        saved_d = (d.API_BASE, d.STORES_FILE, d.SEEN_FILE, d.STATE_FILE, d.products_url)
        saved_m = (m.TIMEOUT, m.STORES, m.STATE, m.fetch_products, m.BEST_SELLERS,
                   m.IMAGE_HASHES, m.MATCHES_FILE, m.SUMMARY_STATE)

        def restore():
            d.API_BASE, d.STORES_FILE, d.SEEN_FILE, d.STATE_FILE, d.products_url = saved_d
            (m.TIMEOUT, m.STORES, m.STATE, m.fetch_products, m.BEST_SELLERS,
             m.IMAGE_HASHES, m.MATCHES_FILE, m.SUMMARY_STATE) = saved_m
        self.addCleanup(restore)

        d.API_BASE = f"{self.base}/api"
        d.STORES_FILE = tmp / "stores.json"
        d.SEEN_FILE = tmp / "seen.json"
        d.STATE_FILE = tmp / "intake_state.json"
        # https://kauppa.com -> valekauppa /shop/kauppa.com/products.json
        d.products_url = lambda store: (
            f"{self.base}/shop/{store.split('://', 1)[1]}/products.json")
        m.TIMEOUT = 2
        # monitorin kierros ajetaan osassa testeja: sen tilat temppiin
        m.BEST_SELLERS = tmp / "best_sellers.json"
        m.IMAGE_HASHES = tmp / "image_hashes.json"
        m.MATCHES_FILE = tmp / "matches.json"
        m.SUMMARY_STATE = tmp / "summary_state.json"

        self.write_stores({"https://vanha.com": "$", "https://www.curvite.com": "$"})
        d.SEEN_FILE.write_text(json.dumps({"https://vanha.com": ["1"]}))

    def write_stores(self, stores):
        d.STORES_FILE.write_text(json.dumps(stores))

    def stores(self):
        return json.loads(d.STORES_FILE.read_text())

    def seen(self):
        return json.loads(d.SEEN_FILE.read_text())

    def run_intake(self, **kw):
        return d.run(TOKEN, CHANNEL, **kw)

    def reactions_for(self, mid):
        return [e for i, e in REACTIONS if i == str(mid)]


# --- domainien poiminta ja normalisointi --------------------------------

class TestExtract(Base):
    def test_paljas_domain(self):
        self.assertEqual(d.extract_stores("kauppa.com"), ["https://kauppa.com"])

    def test_markdown_linkki_on_yksi_kauppa(self):
        self.assertEqual(d.extract_stores("[www.kauppa.com](https://www.kauppa.com)"),
                         ["https://kauppa.com"])

    def test_polku_ja_kauttaviiva_poistetaan(self):
        self.assertEqual(d.extract_stores("https://kauppa.com/jotain"), ["https://kauppa.com"])
        self.assertEqual(d.extract_stores("https://kauppa.com/"), ["https://kauppa.com"])
        self.assertEqual(d.extract_stores("https://kauppa.com/collections/all?page=2"),
                         ["https://kauppa.com"])

    def test_pienet_kirjaimet_ja_http(self):
        self.assertEqual(d.extract_stores("HTTP://WWW.Kauppa.COM"), ["https://kauppa.com"])

    def test_alidomainit_sailyvat(self):
        self.assertEqual(d.extract_stores("buy.fayora.shop modo-clothing.myshopify.com"),
                         ["https://buy.fayora.shop", "https://modo-clothing.myshopify.com"])

    def test_monta_domainia_samassa_viestissa(self):
        text = "uusia: kauppa.com, https://toinen.store/products/x\nja <https://kolmas.co>"
        self.assertEqual(d.extract_stores(text), [
            "https://kauppa.com", "https://toinen.store", "https://kolmas.co"])

    def test_sama_domain_kahdesti_vain_kerran(self):
        self.assertEqual(d.extract_stores("kauppa.com ja www.kauppa.com/x"),
                         ["https://kauppa.com"])

    def test_polun_osia_ja_sahkoposteja_ei_poimita(self):
        self.assertEqual(d.extract_stores("kauppa.com/products.json"), ["https://kauppa.com"])
        self.assertEqual(d.extract_stores("info@kauppa.com"), [])

    def test_ei_domainia(self):
        self.assertEqual(d.extract_stores("moi, tassa ei ole kauppaa 1.5"), [])
        self.assertEqual(d.extract_stores(None), [])

    def test_www_ja_ilman_ovat_sama_kauppa(self):
        self.assertTrue(d.is_known("https://curvite.com", {"https://www.curvite.com": "$"}))
        self.assertFalse(d.is_known("https://curvite.co", {"https://www.curvite.com": "$"}))


# --- kasittely ----------------------------------------------------------

class TestIntake(Base):
    def test_duplikaatti_saa_x(self):
        MESSAGES.append(msg(10, "vanha.com"))
        MESSAGES.append(msg(11, "https://curvite.com/"))   # listalla www-muodossa
        self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.DUPLICATE])
        self.assertEqual(self.reactions_for(11), [d.DUPLICATE])
        self.assertEqual(self.stores(), {"https://vanha.com": "$", "https://www.curvite.com": "$"})

    def test_toimiva_kauppa_lisataan(self):
        SHOPS["uusi.com"] = products(101, 102)
        MESSAGES.append(msg(10, "[www.uusi.com](https://www.uusi.com)"))
        self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.ADDED])
        self.assertEqual(self.stores()["https://uusi.com"], "$")
        self.assertEqual(list(self.stores())[:2], ["https://vanha.com", "https://www.curvite.com"],
                         "vanhat kaupat ja jarjestys sailyvat")

    def test_kuollut_kauppa_saa_varoituksen_eika_paady_listaan(self):
        for host, status in [("a401.com", "401"), ("a403.com", "403"),
                             ("a404.com", "404"), ("hidas.com", "slow"),
                             ("salasana.com", "html"), ("eishopify.com", "nokey")]:
            SHOPS[host] = status
        MESSAGES.append(msg(10, "a401.com a403.com a404.com hidas.com "
                                "salasana.com eishopify.com"))
        counts = self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.UNREACHABLE])
        self.assertEqual(counts[d.UNREACHABLE], 6)
        self.assertEqual(set(self.stores()), {"https://vanha.com", "https://www.curvite.com"})
        self.assertEqual(set(self.seen()), {"https://vanha.com"})

    def test_samassa_viestissa_kaikki_tulokset(self):
        SHOPS["uusi.com"] = products(1)
        SHOPS["kuollut.com"] = "404"
        MESSAGES.append(msg(10, "vanha.com kuollut.com uusi.com"))
        self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.DUPLICATE, d.UNREACHABLE, d.ADDED])
        self.assertIn("https://uusi.com", self.stores())

    def test_virhe_yhdessa_domainissa_ei_kaada_muita(self):
        SHOPS["uusi.com"] = products(1)
        real = d.handle_store

        def flaky(store, stores, seen):
            if store == "https://rikki.com":
                raise RuntimeError("odottamaton")
            return real(store, stores, seen)
        d.handle_store = flaky
        self.addCleanup(setattr, d, "handle_store", real)

        MESSAGES.append(msg(10, "rikki.com uusi.com"))
        MESSAGES.append(msg(11, "vanha.com"))
        self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.ADDED])
        self.assertEqual(self.reactions_for(11), [d.DUPLICATE])

    def test_timeout_ei_pysayta_seuraavaa_domainia(self):
        SHOPS["hidas.com"] = "slow"
        SHOPS["uusi.com"] = products(1)
        MESSAGES.append(msg(10, "hidas.com uusi.com"))
        self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.UNREACHABLE, d.ADDED])

    def test_sama_kauppa_kahdessa_viestissa(self):
        SHOPS["uusi.com"] = products(1)
        MESSAGES.extend([msg(10, "uusi.com"), msg(11, "www.uusi.com")])
        self.run_intake()
        self.assertEqual(self.reactions_for(10), [d.ADDED])
        self.assertEqual(self.reactions_for(11), [d.DUPLICATE])

    def test_viesti_ilman_domainia_ei_saa_reaktiota(self):
        MESSAGES.append(msg(10, "moi kaikki"))
        self.run_intake()
        self.assertEqual(REACTIONS, [])

    def test_botin_viestit_ohitetaan(self):
        SHOPS["uusi.com"] = products(1)
        MESSAGES.append(msg(10, "uusi.com", bot=True))
        self.run_intake()
        self.assertEqual(REACTIONS, [])
        self.assertNotIn("https://uusi.com", self.stores())

    def test_token_lahetetaan_bot_otsakkeena(self):
        MESSAGES.append(msg(10, "vanha.com"))
        self.run_intake()
        self.assertTrue(AUTH)
        self.assertEqual(set(AUTH), {f"Bot {TOKEN}"})


# --- pohjadata ----------------------------------------------------------

class TestBaseline(Base):
    def test_pohjadata_tallentuu_seen_jsoniin(self):
        SHOPS["uusi.com"] = products(101, 102, 103)
        MESSAGES.append(msg(10, "uusi.com"))
        self.run_intake()
        seen = self.seen()
        self.assertEqual(seen["https://uusi.com"], ["101", "102", "103"])
        self.assertEqual(seen["https://vanha.com"], ["1"], "muiden kauppojen tila sailyy")

    def test_pohjadata_kattaa_kaikki_sivut(self):
        SHOPS["iso.com"] = products(*range(1, m.PAGE_SIZE + 6))
        MESSAGES.append(msg(10, "iso.com"))
        self.run_intake()
        self.assertEqual(len(self.seen()["https://iso.com"]), m.PAGE_SIZE + 5)

    def test_monitori_ei_spammaa_uuden_kaupan_tuotteita(self):
        SHOPS["uusi.com"] = products(101, 102, 103)
        MESSAGES.append(msg(10, "uusi.com"))
        self.run_intake()

        # Ajetaan oikea monitorin kierros intaken kirjoittamilla tiedostoilla.
        m.STORES = m.load_stores(d.STORES_FILE)
        m.STATE = d.SEEN_FILE
        m.fetch_products = d.fetch_catalog         # ohjataan valekauppaan
        webhook = f"{self.base}/webhook"

        m.check_all(m.load_seen(), webhook=webhook, dry_run=False)
        self.assertEqual(POSTED, [], "uuden kaupan nykyisia tuotteita ei saa postata")

        # Oikeasti uusi tuote postataan normaalisti
        SHOPS["uusi.com"] = products(101, 102, 103, 104)
        m.check_all(m.load_seen(), webhook=webhook, dry_run=False)
        self.assertEqual([b["embeds"][0]["title"] for b in POSTED], ["Tuote 104"])


# --- tila, sivutus ja rate limit ----------------------------------------

class TestState(Base):
    def test_viimeisin_viesti_tallentuu(self):
        MESSAGES.extend([msg(10, "vanha.com"), msg(12, "moi")])
        self.run_intake()
        self.assertEqual(json.loads(d.STATE_FILE.read_text())["last_message_id"], "12")

    def test_toinen_ajo_ei_kasittele_samoja(self):
        MESSAGES.append(msg(10, "vanha.com"))
        self.run_intake()
        self.run_intake()
        self.assertEqual(len(REACTIONS), 1)

        MESSAGES.append(msg(20, "vanha.com"))
        self.run_intake()
        self.assertEqual(REACTIONS, [("10", d.DUPLICATE), ("20", d.DUPLICATE)])

    def test_viestit_kasitellaan_vanhimmasta_alkaen_yli_sivurajan(self):
        MESSAGES.extend(msg(1000 + i, "vanha.com") for i in range(d.MESSAGE_LIMIT + 20))
        self.run_intake()
        ids = [int(i) for i, _ in REACTIONS]
        self.assertEqual(len(ids), d.MESSAGE_LIMIT + 20)
        self.assertEqual(ids, sorted(ids))

    def test_emoji_on_url_enkoodattu(self):
        SHOPS["uusi.com"] = products(1)
        SHOPS["kuollut.com"] = "404"
        MESSAGES.append(msg(10, "vanha.com uusi.com kuollut.com"))
        self.run_intake()
        self.assertEqual(RAW_REACTION_PATHS, ["%E2%9D%8C", "%E2%9C%85", "%E2%9A%A0%EF%B8%8F"])

    def test_discordin_429_odotetaan_ja_yritetaan_uudelleen(self):
        REACTION_429[0] = 2
        MESSAGES.append(msg(10, "vanha.com"))
        self.run_intake()
        self.assertEqual(REACTIONS, [("10", d.DUPLICATE)])

    def test_reaktion_virhe_ei_kaada_ajoa(self):
        REACTION_429[0] = d.MAX_RETRIES          # ensimmainen reaktio luovuttaa
        SHOPS["uusi.com"] = products(1)
        MESSAGES.extend([msg(10, "vanha.com"), msg(11, "uusi.com")])
        self.run_intake()
        self.assertEqual(REACTIONS, [("11", d.ADDED)])
        self.assertIn("https://uusi.com", self.stores())

    def test_dry_run_ei_reagoi_eika_kirjoita(self):
        SHOPS["uusi.com"] = products(1)
        MESSAGES.append(msg(10, "uusi.com"))
        stores_before = d.STORES_FILE.read_text()
        counts = self.run_intake(dry_run=True)
        self.assertEqual(counts[d.ADDED], 1)
        self.assertEqual(REACTIONS, [])
        self.assertEqual(d.STORES_FILE.read_text(), stores_before)
        self.assertFalse(d.STATE_FILE.exists())


# --- komentoriviliittyma ------------------------------------------------

class TestCli(Base):
    def test_puuttuvat_ymparistomuuttujat_pysayttavat_selkeasti(self):
        import sys
        argv = sys.argv
        env = {k: os.environ.pop(k, None) for k in ("DISCORD_BOT_TOKEN", "AI_STORES_CHANNEL_ID")}
        sys.argv = ["discord_intake.py"]
        try:
            with self.assertRaises(SystemExit):
                d.main()
        finally:
            sys.argv = argv
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v

    def test_main_lukee_ymparistomuuttujat(self):
        import sys
        MESSAGES.append(msg(10, "vanha.com"))
        argv = sys.argv
        env = {k: os.environ.get(k) for k in ("DISCORD_BOT_TOKEN", "AI_STORES_CHANNEL_ID")}
        os.environ["DISCORD_BOT_TOKEN"] = TOKEN
        os.environ["AI_STORES_CHANNEL_ID"] = CHANNEL
        sys.argv = ["discord_intake.py"]
        try:
            d.main()
        finally:
            sys.argv = argv
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(REACTIONS, [("10", d.DUPLICATE)])

    def test_koodissa_ei_ole_kovakoodattua_tokenia(self):
        src = pathlib.Path(d.__file__).read_text()
        self.assertNotRegex(src, r"Bot [A-Za-z0-9_-]{20,}\.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
