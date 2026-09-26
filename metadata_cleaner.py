#!/usr/bin/env python3
"""
Metadatan poisto videoista Discord-kanavan kautta.

Lukee kanavan METADATA_CHANNEL_ID uudet viestit, ajaa jokaisen
videoliitteen (mp4, mov, m4v, webm) ffmpegin lapi ilman uudelleenkoodausta
ja vastaa alkuperaiseen viestiin puhtaalla tiedostolla satunnaisella nimella.

  :white_check_mark:  puhdistettu ja lahetetty
  :warning:           ffmpeg epaonnistui, lataus epaonnistui tai tiedosto on
                      liian iso lahetettavaksi (silloin myos vastausviesti)

Kayttaa omaa bottiaan (METADATA_BOT_TOKEN), EI intaken DISCORD_BOT_TOKENia.
Jos token tai kanava puuttuu, lopettaa hiljaa ilman virhetta.
Ensimmainen ajo ei kasittele vanhoja viesteja, vaan tallentaa viimeisimman
viesti-id:n pohjaksi (metadata_state.json).

Ajo: METADATA_BOT_TOKEN=... METADATA_CHANNEL_ID=... python3 metadata_cleaner.py
Vaatii: ffmpeg PATHissa.
"""

import json
import os
import pathlib
import secrets
import shutil
import subprocess
import tempfile
import time

import requests

import discord_intake as d
import shopify_monitor as m

STATE_FILE = pathlib.Path("metadata_state.json")   # viimeksi kasitelty viesti-id
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
MIME_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime",
              ".m4v": "video/x-m4v", ".webm": "video/webm"}

DEFAULT_LIMIT = 10 * 1024 * 1024   # Discordin oletusraja (10 Mt)
# Palvelimen boostitaso -> latausraja (Discordin dokumentaation mukaan)
TIER_LIMITS = {0: 10 * 1024 * 1024, 1: 10 * 1024 * 1024,
               2: 50 * 1024 * 1024, 3: 100 * 1024 * 1024}
MAX_DOWNLOAD = 500 * 1024 * 1024   # isompia ei edes ladata
FFMPEG_TIMEOUT = 120               # sekuntia per video
TIME_BUDGET = 180                  # sekuntia per ajo; loput jaavat seuraavalle
                                   # (koko workflow-jobin timeout on 10 min)

CLEANED = "✅"          # :white_check_mark:
FAILED = "⚠️"  # :warning:


# --- ffmpeg -------------------------------------------------------------

def ffmpeg_command(src, dst):
    """Metadata ja kappaleet pois, virrat kopioidaan sellaisenaan."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-map", "0:v", "-map", "0:a?",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c", "copy",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-movflags", "+faststart",
            # -map_metadata -1 ei poista muxerin omia oletuksia
            # (VideoHandler/SoundHandler, Lavf): tyhjennetaan ne erikseen.
            "-metadata:s:v", "handler_name=", "-metadata:s:a", "handler_name=",
            "-metadata", "encoder=",
            str(dst)]


def clean_video(src, dst):
    """Ajaa ffmpegin. Nostaa poikkeuksen jos se epaonnistuu."""
    r = subprocess.run(ffmpeg_command(src, dst), capture_output=True, text=True,
                       timeout=FFMPEG_TIMEOUT)
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg {r.returncode}: {r.stderr.strip()[-300:]}")


# --- Discord ------------------------------------------------------------

def bot_user_id(token):
    return d.discord_request(token, "GET", "/users/@me").json()["id"]


def latest_message_id(token, channel):
    batch = d.discord_request(token, "GET", f"/channels/{channel}/messages",
                              params={"limit": 1}).json()
    return batch[0]["id"] if batch else "0"


def upload_limit(token, channel):
    """Latausraja kanavan palvelimen boostitasosta, oletus 10 Mt."""
    try:
        guild_id = d.discord_request(token, "GET", f"/channels/{channel}").json().get("guild_id")
        if not guild_id:
            return DEFAULT_LIMIT
        tier = d.discord_request(token, "GET", f"/guilds/{guild_id}").json().get("premium_tier")
        return TIER_LIMITS.get(tier, DEFAULT_LIMIT)
    except Exception as e:
        print(f"[metadata] rajaa ei saatu ({d.describe_error(e)}), oletetaan 10 Mt")
        return DEFAULT_LIMIT


def reply_with_file(token, channel, message_id, path):
    payload = {"message_reference": {"message_id": message_id, "fail_if_not_exists": False},
               "allowed_mentions": {"replied_user": False}}
    data = path.read_bytes()        # tavuina, jotta 429-uusinta lahettaa koko tiedoston
    d.discord_request(
        token, "POST", f"/channels/{channel}/messages",
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": (path.name, data, MIME_TYPES.get(path.suffix, "video/mp4"))},
    )


def reply_text(token, channel, message_id, text):
    d.discord_request(token, "POST", f"/channels/{channel}/messages", json={
        "content": text,
        "message_reference": {"message_id": message_id, "fail_if_not_exists": False},
        "allowed_mentions": {"replied_user": False},
    })


def _mb(n):
    return f"{n / (1024 * 1024):.1f} Mt"


def download(url, dst):
    with requests.get(url, stream=True, timeout=m.TIMEOUT) as r:
        r.raise_for_status()
        size = 0
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise RuntimeError(f"liite yli {_mb(MAX_DOWNLOAD)}")
                f.write(chunk)


# --- viestien kasittely -------------------------------------------------

def video_attachments(msg):
    return [a for a in msg.get("attachments") or []
            if pathlib.Path(a.get("filename") or "").suffix.lower() in VIDEO_EXTENSIONS]


def process_attachment(token, channel, msg, att, limit):
    """Palauttaa reaktion (CLEANED/FAILED). Valiaikaiset poistetaan aina."""
    ext = pathlib.Path(att["filename"]).suffix.lower()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="metadata-"))
    try:
        src = tmp / f"in{ext}"
        dst = tmp / f"{secrets.token_hex(8)}{ext}"      # satunnainen nimi
        try:
            download(att["url"], src)
            clean_video(src, dst)
        except Exception as e:
            print(f"[metadata] viesti {msg['id']}: {d.describe_error(e)}")
            return FAILED

        size = dst.stat().st_size
        too_big = size > limit
        if not too_big:
            try:
                reply_with_file(token, channel, msg["id"], dst)
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 413:
                    raise
                too_big = True                            # raja arvioitu vaarin
        if too_big:
            reply_text(token, channel, msg["id"],
                       f"Puhdistettu video on {_mb(size)}, mutta tämän palvelimen "
                       f"lähetysraja on {_mb(limit)}. Tiedostoa ei voitu lähettää.")
            print(f"[metadata] viesti {msg['id']}: liian iso ({_mb(size)} > {_mb(limit)})")
            return FAILED
        print(f"[metadata] viesti {msg['id']}: puhdistettu ({_mb(size)})")
        return CLEANED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_message(token, channel, msg, limit):
    reactions = []
    for att in video_attachments(msg):
        try:
            emoji = process_attachment(token, channel, msg, att, limit)
        except Exception as e:
            print(f"[metadata] viesti {msg['id']}: {d.describe_error(e)}")
            emoji = FAILED
        if emoji not in reactions:
            reactions.append(emoji)
    for emoji in reactions:
        try:
            d.react(token, channel, msg["id"], emoji)
        except Exception as e:
            print(f"[reaktio-virhe] viesti {msg['id']}: {d.describe_error(e)}")
    return reactions


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1) + "\n")


def run(token, channel):
    state = d.load_json(STATE_FILE, {})
    if "last_message_id" not in state:
        # Pohjadata: kanavan vanhoja viesteja ei kasitella.
        state["last_message_id"] = latest_message_id(token, channel)
        save_state(state)
        print(f"[metadata] pohjadata tallennettu (viimeisin viesti {state['last_message_id']})")
        return 0

    messages = d.fetch_messages(token, channel, state["last_message_id"])
    if not messages:
        print("[metadata] ei uusia viesteja")
        return 0
    me = bot_user_id(token)
    limit = None
    handled, started = 0, time.monotonic()
    for msg in messages:
        if time.monotonic() - started > TIME_BUDGET:
            print("[metadata] aikabudjetti taynna, loput seuraavalla ajolla")
            break
        author = (msg.get("author") or {}).get("id")
        if author != me and video_attachments(msg):
            if limit is None:
                limit = upload_limit(token, channel)
            process_message(token, channel, msg, limit)
            handled += 1
        state["last_message_id"] = msg["id"]
        save_state(state)
    print(f"--- metadata valmis: {handled} viestia videoineen kasitelty ---")
    return handled


def main():
    token = os.environ.get("METADATA_BOT_TOKEN", "").strip()
    channel = os.environ.get("METADATA_CHANNEL_ID", "").strip()
    if not token or not channel:
        return                         # ominaisuus ei kaytossa: hiljaa pois
    if shutil.which("ffmpeg") is None:
        print("[metadata] ffmpeg puuttuu, ohitetaan")
        return
    run(token, channel)


if __name__ == "__main__":
    main()
