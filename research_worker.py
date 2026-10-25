"""Always-on Telegram long polling. One immediate research at a time, no task queue."""
import os
import re
import requests
import threading
import time
from pathlib import Path
from product_hunter import telegram
from product_hunter.manual import Admission, verified_callback
from product_hunter.market import investigate


class Worker:
    def __init__(self, state, bot_id):
        self.state, self.bot_id = state, bot_id
        self.busy = threading.Lock()

    def answer(self, query, text):
        telegram._call('answerCallbackQuery', {'callback_query_id': query['id'], 'text': text})

    def handle(self, query):
        if os.getenv('MANUAL_RESEARCH_ENABLED') != 'true':
            self.answer(query, 'Tutkimus on pois käytöstä.')
            return
        try:
            url, caption = verified_callback(query, self.bot_id)
            self.answer(query, 'Tarkistan ryhmän ja tutkimusrajan…')
            telegram.validate_destination()
            member = telegram._call('getChatMember', {'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'user_id': query['from']['id']})
            if member.get('status') not in ('creator', 'administrator', 'member'):
                raise ValueError('Not a member')
        except Exception:
            self.answer(query, 'Tätä painiketta ei voitu vahvistaa yksityiseen ryhmään.')
            return
        if not self.busy.acquire(blocking=False):
            self.answer(query, 'Tutkimus on jo käynnissä. Kokeile uudelleen sen valmistuttua.')
            return
        try:
            status = self.state.claim(url)
            if status != 'started':
                self.answer(query, 'Tämä tuote on jo käsitelty.' if status == 'duplicate' else 'Kuukauden tutkimusraja on täynnä.')
                self.busy.release()
                return
            self.answer(query, 'Tutkimus alkaa nyt. Raportti tulee tähän ryhmään.')
            thread = threading.Thread(target=self.run_report, args=(url, caption, (query.get('message') or {}).get('photo') or []), daemon=True)
            thread.start()
        except Exception:
            self.busy.release()
            raise

    def run_report(self, url, caption, photos=()):
        try:
            telegram.validate_destination()
            telegram.send_message('🔎 Tutkin tuotteen ja vertaan US- ja UK-markkinoita.\n' + url)
            image = photo_bytes(photos) if photos else None
            report = investigate(url, caption, image)
            # Preserve all sources and paragraphs instead of silently clipping the report.
            while report:
                chunk = telegram._clip(report, 3500)
                if len(chunk) < len(report) and '\n' in chunk[1500:]:
                    chunk = chunk[:chunk.rfind('\n') + 1]
                telegram.validate_destination()
                telegram.send_message(chunk)
                report = report[len(chunk):]
            self.state.finish(url, 'done')
        except Exception:
            self.state.finish(url, 'failed')
            print('[manual-research] Failed; details omitted to protect credentials', flush=True)
            try:
                telegram.validate_destination()
                telegram.send_message('Tuotteen tutkimus epäonnistui tai lähteet jäivät puutteellisiksi. Automaattista uusintaa ei tehdä.\n' + url)
            except Exception:
                pass
        finally:
            self.busy.release()


def photo_bytes(photos):
    # Telegram re-encodes photos to JPEG. Never pass a bot-token file URL to OpenAI.
    try:
        photo = next(p for p in reversed(photos) if p.get('file_size', 5_000_001) <= 5_000_000)
        info = telegram._call('getFile', {'file_id': photo['file_id']})
        path = info.get('file_path', '')
        if not re.fullmatch(r'photos/[A-Za-z0-9_/-]+\.jpg', path) or '..' in path:
            return None
        with requests.get('https://api.telegram.org/file/bot' + os.environ['TELEGRAM_BOT_TOKEN'] + '/' + path,
                          timeout=15, allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            image = bytearray()
            for chunk in response.iter_content(65536):
                image.extend(chunk)
                if len(image) > 5_000_000:
                    return None
            return bytes(image) if image.startswith(b'\xff\xd8\xff') else None
    except Exception:
        return None


def main():
    for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'OPENAI_API_KEY', 'OPENAI_RESEARCH_MODEL'):
        if not os.getenv(name):
            raise RuntimeError('Worker configuration is incomplete')
    if os.getenv('TELEGRAM_PRIVATE_GROUP_ONLY') != 'true':
        raise RuntimeError('Private group protection is required')
    directory = Path(os.getenv('MONITOR_DATA_DIR', '/data'))
    if not os.path.ismount(directory):
        raise RuntimeError('Persistent worker volume is required')
    # A second poller must not share the token. Never delete someone else's webhook.
    if telegram._call('getWebhookInfo', {}).get('url'):
        raise RuntimeError('Bot webhook is configured')
    telegram.validate_destination()
    worker = Worker(Admission(directory / 'manual_research.sqlite3'), telegram._call('getMe', {})['id'])
    print('[manual-research] Ready for explicit button presses', flush=True)
    while True:
        try:
            updates = telegram._call('getUpdates', {'offset': worker.state.offset(), 'timeout': 30,
                                     'allowed_updates': ['callback_query']}, timeout=40)
            for update in updates:
                # Persist before handling: an uncertain paid attempt is never replayed after restart.
                worker.state.acknowledge(update['update_id'] + 1)
                if update.get('callback_query'):
                    worker.handle(update['callback_query'])
        except Exception:
            print('[manual-research] Poll failed; sensitive details omitted', flush=True)
            time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('[manual-research] Startup failed; sensitive details omitted', flush=True)
        raise SystemExit(1)
