#!/usr/bin/env python3
"""
Testit metadata_cleaner.py:lle.

Nostaa pystyyn vale-Discordin (viestit, liitteiden lataus, vastaukset
tiedostoineen, reaktiot, palvelimen tiedot), joten mitaan ei lahde ulos.
Oikeaa ffmpegia vaativat testit generoivat pienen videon metadatan kanssa
ja tarkistavat tuloksen ffprobella; ne ohitetaan jos ffmpeg puuttuu.

Ajo: python3 -m unittest -v test_metadata_cleaner.py
"""

import contextlib
import email
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import discord_intake as d
import metadata_cleaner as mc
import shopify_monitor as m

CHANNEL = "777"
GUILD = "888"
TOKEN = "metadatatoken"
BOT_ID = "999"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# --- vale-Discord -------------------------------------------------------

MESSAGES = []        # kanavan viestit
FILES = {}           # /files/<nimi> -> tavut (liitteiden CDN)
REPLIES = []         # POST /messages: {"payload": .., "files": {nimi: tavut}}
REACTIONS = []       # (viesti-id, emoji)
AUTH = []            # Authorization-otsakkeet
GUILD_TIER = [0]
UPLOAD_413 = [False]


def msg(mid, content="", attachments=(), author=None):
    return {"id": str(mid), "content": content,
            "author": {"id": author or "1", "username": "x", "bot": author == BOT_ID},
            "attachments": [{"id": f"a{mid}{i}", "filename": name, "url": url}
                            for i, (name, url) in enumerate(attachments)]}


def parse_multipart(content_type, body):
    parsed = email.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    payload, files = None, {}
    for part in parsed.get_payload():
        name = part.get_param("name", header="content-disposition")
        if name == "payload_json":
            payload = json.loads(part.get_payload(decode=True))
        else:
            files[part.get_filename()] = part.get_payload(decode=True)
    return payload, files


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path.startswith("/files/"):
            data = FILES.get(unquote(url.path[len("/files/"):]))
            if data is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        AUTH.append(self.headers.get("Authorization"))
        if url.path == "/api/users/@me":
            self._json(200, {"id": BOT_ID, "username": "metadata-bot", "bot": True})
        elif url.path == f"/api/channels/{CHANNEL}/messages":
            after = int(q.get("after", ["0"])[0])
            limit = int(q.get("limit", ["50"])[0])
            ordered = sorted(MESSAGES, key=lambda x: int(x["id"]))
            if "after" in q:
                self._json(200, [x for x in ordered if int(x["id"]) > after][:limit][::-1])
            else:
                self._json(200, ordered[::-1][:limit])      # uusimmat, uusin ensin
        elif url.path == f"/api/channels/{CHANNEL}":
            self._json(200, {"id": CHANNEL, "guild_id": GUILD})
        elif url.path == f"/api/guilds/{GUILD}":
            self._json(200, {"id": GUILD, "premium_tier": GUILD_TIER[0]})
        else:
            self._json(404, {"message": "Unknown"})

    def do_POST(self):
        AUTH.append(self.headers.get("Authorization"))
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/"):
            if UPLOAD_413[0]:
                self._json(413, {"message": "Request entity too large"})
                return
            payload, files = parse_multipart(ctype, body)
        else:
            payload, files = json.loads(body), {}
        REPLIES.append({"payload": payload, "files": files})
        self._json(200, {"id": "5000"})

    def do_PUT(self):
        AUTH.append(self.headers.get("Authorization"))
        parts = urlsplit(self.path).path.strip("/").split("/")
        # api/channels/<c>/messages/<m>/reactions/<emoji>/@me
        REACTIONS.append((parts[4], unquote(parts[6])))
        self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.srv.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        for lst in (MESSAGES, REPLIES, REACTIONS, AUTH):
            lst.clear()
        FILES.clear()
        GUILD_TIER[0] = 0
        UPLOAD_413[0] = False
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        saved = (d.API_BASE, mc.STATE_FILE, mc.clean_video, mc.TIER_LIMITS,
                 tempfile.tempdir, m.TIMEOUT)

        def restore():
            (d.API_BASE, mc.STATE_FILE, mc.clean_video, mc.TIER_LIMITS,
             tempfile.tempdir, m.TIMEOUT) = saved
        self.addCleanup(restore)
        d.API_BASE = f"{self.base}/api"
        mc.STATE_FILE = self.tmp / "metadata_state.json"
        m.TIMEOUT = 5
        # valiaikaistiedostot omaan kansioon, jotta niiden poisto voidaan todeta
        self.work = self.tmp / "work"
        self.work.mkdir()
        tempfile.tempdir = str(self.work)

        for env in ("METADATA_BOT_TOKEN", "METADATA_CHANNEL_ID", "DISCORD_BOT_TOKEN"):
            old = os.environ.pop(env, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, env, old)
            self.addCleanup(os.environ.pop, env, None)

    def baseline(self, last="1"):
        mc.STATE_FILE.write_text(json.dumps({"last_message_id": last}))

    def file(self, name, data):
        FILES[name] = data
        return name, f"{self.base}/files/{name}"

    def stub_ffmpeg(self, fail_on=()):
        """ffmpegin korvike: kopioi tiedoston (tai epaonnistuu)."""
        def fake(src, dst):
            if src.read_bytes() in fail_on:
                raise RuntimeError("ffmpeg 1: Invalid data found")
            shutil.copy(src, dst)
        mc.clean_video = fake

    def state(self):
        return json.loads(mc.STATE_FILE.read_text())

    def run_cleaner(self):
        return mc.run(TOKEN, CHANNEL)


# --- kaynnistys ja pohjadata --------------------------------------------

class TestStartup(Base):
    def test_puuttuva_token_lopettaa_hiljaa(self):
        os.environ["METADATA_CHANNEL_ID"] = CHANNEL
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mc.main()                                  # ei SystemExitia eika virhetta
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(AUTH, [], "ei yhtaan Discord-pyyntoa")
        self.assertFalse(mc.STATE_FILE.exists())

    def test_puuttuva_kanava_lopettaa_hiljaa(self):
        os.environ["METADATA_BOT_TOKEN"] = TOKEN
        mc.main()
        self.assertEqual(AUTH, [])

    def test_intaken_tokenia_ei_kayteta(self):
        os.environ["DISCORD_BOT_TOKEN"] = "intaken-token"
        os.environ["METADATA_CHANNEL_ID"] = CHANNEL
        mc.main()
        self.assertEqual(AUTH, [], "DISCORD_BOT_TOKEN ei saa kaynnistaa metadatan poistoa")

    def test_pyynnot_kayttavat_metadata_tokenia(self):
        MESSAGES.append(msg(5))
        self.run_cleaner()
        self.assertEqual(set(AUTH), {f"Bot {TOKEN}"})

    def test_ensimmainen_ajo_on_pohjadata(self):
        self.stub_ffmpeg()
        MESSAGES.extend([msg(10, attachments=[self.file("v.mp4", b"x")]), msg(12)])
        self.run_cleaner()
        self.assertEqual(self.state(), {"last_message_id": "12"})
        self.assertEqual((REPLIES, REACTIONS), ([], []), "vanhoja viesteja ei kasitella")
        MESSAGES.append(msg(13, attachments=[self.file("u.mp4", b"y")]))
        self.run_cleaner()
        self.assertEqual(REACTIONS, [("13", mc.CLEANED)])

    def test_tyhja_kanava_pohjadataksi(self):
        self.run_cleaner()
        self.assertEqual(self.state(), {"last_message_id": "0"})


# --- viestien kasittely -------------------------------------------------

class TestProcessing(Base):
    def setUp(self):
        super().setUp()
        self.baseline()
        self.stub_ffmpeg(fail_on=(b"rikki",))

    def test_vastaa_puhtaalla_tiedostolla_satunnaisella_nimella(self):
        MESSAGES.append(msg(10, attachments=[self.file("Loma Helsinki IMG_0042.MOV", b"video")]))
        self.run_cleaner()
        [reply] = REPLIES
        [(name, data)] = reply["files"].items()
        self.assertEqual(data, b"video")
        self.assertRegex(name, r"^[0-9a-f]{16}\.mov$", "satunnainen nimi, sama muoto")
        self.assertNotIn("IMG_0042", name)
        self.assertEqual(reply["payload"]["message_reference"]["message_id"], "10")
        self.assertEqual(REACTIONS, [("10", mc.CLEANED)])
        self.assertEqual(self.state(), {"last_message_id": "10"})

    def test_kaikki_videomuodot_ja_vain_ne(self):
        MESSAGES.append(msg(10, attachments=[
            self.file("a.mp4", b"1"), self.file("b.MOV", b"2"), self.file("c.m4v", b"3"),
            self.file("d.webm", b"4"), self.file("e.jpg", b"5"), self.file("f.txt", b"6")]))
        self.run_cleaner()
        exts = sorted(pathlib.Path(n).suffix for r in REPLIES for n in r["files"])
        self.assertEqual(exts, [".m4v", ".mov", ".mp4", ".webm"])

    def test_viestit_ilman_videota_ohitetaan(self):
        MESSAGES.extend([msg(10, "moi"), msg(11, attachments=[self.file("k.png", b"x")])])
        self.run_cleaner()
        self.assertEqual((REPLIES, REACTIONS), ([], []))
        self.assertEqual(self.state(), {"last_message_id": "11"})

    def test_oman_botin_viestit_ohitetaan(self):
        MESSAGES.append(msg(10, attachments=[self.file("a.mp4", b"x")], author=BOT_ID))
        self.run_cleaner()
        self.assertEqual((REPLIES, REACTIONS), ([], []))

    def test_ffmpeg_virhe_varoitus_ja_jatketaan(self):
        MESSAGES.extend([msg(10, attachments=[self.file("rikki.mp4", b"rikki")]),
                         msg(11, attachments=[self.file("ok.mp4", b"ok")])])
        self.run_cleaner()
        self.assertEqual(REACTIONS, [("10", mc.FAILED), ("11", mc.CLEANED)])
        self.assertEqual(len(REPLIES), 1)

    def test_liian_iso_kertoo_koon_ja_rajan(self):
        mc.TIER_LIMITS = {0: 1024 * 1024}                    # 1 Mt
        MESSAGES.append(msg(10, attachments=[self.file("iso.mp4", b"x" * 1_500_000)]))
        self.run_cleaner()
        [reply] = REPLIES
        self.assertEqual(reply["files"], {})
        self.assertIn("1.4 Mt", reply["payload"]["content"])
        self.assertIn("1.0 Mt", reply["payload"]["content"])
        self.assertEqual(REACTIONS, [("10", mc.FAILED)])

    def test_raja_palvelimen_boostitasosta(self):
        GUILD_TIER[0] = 2
        self.assertEqual(mc.upload_limit(TOKEN, CHANNEL), 50 * 1024 * 1024)
        GUILD_TIER[0] = 0
        self.assertEqual(mc.upload_limit(TOKEN, CHANNEL), 10 * 1024 * 1024)

    def test_raja_oletus_10_mt_jos_tietoja_ei_saada(self):
        self.assertEqual(mc.upload_limit(TOKEN, "tuntematon"), 10 * 1024 * 1024)

    def test_413_lahetyksessa_kerrotaan_koko(self):
        UPLOAD_413[0] = True
        MESSAGES.append(msg(10, attachments=[self.file("a.mp4", b"x" * 100)]))
        self.run_cleaner()
        [reply] = REPLIES
        self.assertIn("lähetysraja", reply["payload"]["content"])
        self.assertEqual(REACTIONS, [("10", mc.FAILED)])

    def test_latausvirhe_ei_kaada(self):
        MESSAGES.append(msg(10, attachments=[("a.mp4", f"{self.base}/files/puuttuu.mp4")]))
        self.run_cleaner()
        self.assertEqual(REACTIONS, [("10", mc.FAILED)])

    def test_valiaikaiset_poistetaan_aina(self):
        MESSAGES.extend([msg(10, attachments=[self.file("ok.mp4", b"ok")]),
                         msg(11, attachments=[self.file("rikki.mp4", b"rikki")])])
        mc.TIER_LIMITS = {0: 1}
        MESSAGES.append(msg(12, attachments=[self.file("iso.mp4", b"iso")]))
        self.run_cleaner()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_aikabudjetti_jattaa_loput_seuraavalle(self):
        saved = mc.TIME_BUDGET
        mc.TIME_BUDGET = -1
        self.addCleanup(setattr, mc, "TIME_BUDGET", saved)
        MESSAGES.append(msg(10, attachments=[self.file("a.mp4", b"x")]))
        self.run_cleaner()
        self.assertEqual(self.state(), {"last_message_id": "1"})


# --- oikea ffmpeg -------------------------------------------------------

def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", "-show_chapters", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def stream_hashes(path):
    """Jokaisen virran pakettien md5 (sisalto, ei kontti)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0",
                          "-c", "copy", "-f", "streamhash", "-hash", "md5", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [line.split(",", 2)[2] for line in out.splitlines() if line]


def make_video(path, vcodec, acodec):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", vcodec, "-c:a", acodec, "-shortest",
        "-metadata", "title=Salainen loma",
        "-metadata", "creation_time=2024-06-01T12:00:00Z",
        "-metadata", "location=+60.1699+024.9384/",
        "-metadata", "comment=iPhone 15 Pro",
        "-metadata:s:v", "handler_name=Core Media Video",
        "-metadata:s:a", "handler_name=Core Media Audio",
        "-metadata:s:v", "creation_time=2024-06-01T12:00:00Z",
        str(path)], check=True, capture_output=True)


def encoder_available(name):
    out = subprocess.run(["ffmpeg", "-v", "error", "-encoders"],
                         capture_output=True, text=True).stdout
    return f" {name} " in out


FORBIDDEN = ("creation_time", "encoder", "location", "handler")
# ffmpegin muxerin pakolliset geneeriset oletukset, joita -map_metadata -1
# ei voi poistaa (MP4/MOV hdlr-laatikon nimi, WebM:n muxerin nimi).
# Ne eivat tule alkuperaisesta tiedostosta; kaikki muu on kielletty.
MUXER_DEFAULTS = {"video:handler_name": "VideoHandler",
                  "audio:handler_name": "SoundHandler",
                  "encoder": "Lavf"}
ORIGINAL_VALUES = ("Salainen", "2024-06-01", "+60.1699", "iPhone", "Core Media")


# CI asettaa REQUIRE_FFMPEG=1: silloin puuttuva ffmpeg on virhe eika ohitus.
@unittest.skipUnless(HAVE_FFMPEG or os.environ.get("REQUIRE_FFMPEG"), "ffmpeg/ffprobe puuttuu")
class TestRealFfmpeg(Base):
    CASES = [(".mp4", "mpeg4", "aac"), (".mov", "mpeg4", "aac"), (".m4v", "mpeg4", "aac"),
             (".webm", "libvpx", "libopus")]

    def tags(self, info):
        found = {k.lower(): v for k, v in (info["format"].get("tags") or {}).items()}
        for s in info["streams"]:
            found.update({f"{s['codec_type']}:{k.lower()}": v
                          for k, v in (s.get("tags") or {}).items()})
        return found

    def check(self, ext, vcodec, acodec):
        if not (encoder_available(vcodec) and encoder_available(acodec)):
            self.skipTest(f"{vcodec}/{acodec} ei kaytossa")
        src, dst = self.tmp / f"in{ext}", self.tmp / f"out{ext}"
        make_video(src, vcodec, acodec)
        before = self.tags(ffprobe(src))
        self.assertTrue(any("creation_time" in k for k in before), "testivideossa pitaa olla metadataa")

        mc.clean_video(src, dst)
        info = ffprobe(dst)
        # virrat identtiset: samat koodekit ja tavu tavulta samat paketit
        self.assertEqual(stream_hashes(src), stream_hashes(dst))

        after = self.tags(info)
        leaked = {k: v for k, v in after.items()
                  if any(f in k for f in FORBIDDEN) and MUXER_DEFAULTS.get(k) != v}
        self.assertEqual(leaked, {}, f"{ext}: metadataa jai: {leaked} (kaikki: {after})")
        for value in ORIGINAL_VALUES:
            self.assertNotIn(value, json.dumps(info), f"{ext}: alkuperainen arvo {value!r} jai")
            self.assertNotIn(value.encode(), dst.read_bytes(), f"{ext}: {value!r} tiedoston tavuissa")
        self.assertNotIn("title", after)
        self.assertEqual(info.get("chapters"), [])
        orig = ffprobe(src)["streams"]
        for a, b in zip(orig, info["streams"]):
            for key in ("codec_name", "codec_type", "width", "height", "sample_rate", "channels"):
                self.assertEqual(a.get(key), b.get(key), f"{ext} {key}")
        self.assertEqual(len(orig), len(info["streams"]))

    def test_mp4(self):
        self.check(*self.CASES[0])

    def test_mov(self):
        self.check(*self.CASES[1])

    def test_m4v(self):
        self.check(*self.CASES[2])

    def test_webm(self):
        self.check(*self.CASES[3])

    def test_video_ilman_aanta(self):
        src, dst = self.tmp / "mute.mp4", self.tmp / "clean.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc=duration=1:size=64x64:rate=10", "-c:v", "mpeg4",
                        "-metadata", "creation_time=2024-06-01T12:00:00Z", str(src)],
                       check=True, capture_output=True)
        mc.clean_video(src, dst)                         # -map 0:a? ei kaada
        self.assertEqual([s["codec_type"] for s in ffprobe(dst)["streams"]], ["video"])

    def test_rikkinainen_tiedosto_nostaa_virheen(self):
        src = self.tmp / "rikki.mp4"
        src.write_bytes(b"ei videota")
        with self.assertRaises(RuntimeError):
            mc.clean_video(src, self.tmp / "out.mp4")

    def test_koko_ketju_oikealla_ffmpegilla(self):
        self.baseline()
        src = self.tmp / "loma.mp4"
        make_video(src, "mpeg4", "aac")
        MESSAGES.append(msg(10, attachments=[self.file("loma.mp4", src.read_bytes())]))
        self.run_cleaner()
        [reply] = REPLIES
        [(name, data)] = reply["files"].items()
        out = self.tmp / name
        out.write_bytes(data)
        self.assertEqual(stream_hashes(src), stream_hashes(out))
        leaked = [k for k, v in self.tags(ffprobe(out)).items()
                  if any(f in k for f in FORBIDDEN) and MUXER_DEFAULTS.get(k) != v]
        self.assertEqual(leaked, [])
        for value in ORIGINAL_VALUES:
            self.assertNotIn(value.encode(), data)
        self.assertEqual(REACTIONS, [("10", mc.CLEANED)])
        self.assertEqual(list(self.work.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
