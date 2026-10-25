"""Signed Telegram buttons and durable admission. There is no pending work queue."""
import hashlib
import hmac
import os
import re
import sqlite3
import threading
from functools import wraps

def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped

from datetime import datetime, timezone
from urllib.parse import urlsplit


def product_url(text):
    match = re.search(r'^Tuote: (https://\S+)$', text, re.M)
    if not match:
        raise ValueError('Missing product link')
    url = match.group(1)
    p = urlsplit(url)
    if not p.hostname or p.username or p.password or p.port not in (None, 443) or not p.path.startswith('/products/'):
        raise ValueError('Invalid product link')
    return url


def signature(text):
    key = os.environ['TELEGRAM_BOT_TOKEN'].encode()
    data = ('manual-v1\n' + os.environ['TELEGRAM_CHAT_ID'] + '\n' + text).encode()
    return 'research:' + hmac.new(key, data, hashlib.sha256).hexdigest()[:32]


def keyboard(text):
    if os.getenv('MANUAL_RESEARCH_ENABLED') != 'true':
        return None
    try:
        product_url(text)
        if len(text.encode('utf-16-le')) > 2048:
            return None  # sendPhoto would clip the signed caption.
        return {'inline_keyboard': [[{'text': '🔎 Tutki tuote: US vai UK?', 'callback_data': signature(text)}]]}
    except (ValueError, KeyError):
        return None


def verified_callback(query, bot_id):
    message = query.get('message') or {}
    sender = query.get('from') or {}
    if str((message.get('chat') or {}).get('id')) != os.environ['TELEGRAM_CHAT_ID']:
        raise ValueError('Wrong destination')
    if (message.get('from') or {}).get('id') != bot_id or sender.get('is_bot') or type(sender.get('id')) is not int:
        raise ValueError('Wrong sender')
    text = message.get('caption') or message.get('text') or ''
    if not hmac.compare_digest(str(query.get('data', '')), signature(text)):
        raise ValueError('Invalid signature')
    return product_url(text), text


class Admission:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts (url TEXT PRIMARY KEY, month TEXT, status TEXT)')
        self.db.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")
        self.db.execute('CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY, offset INTEGER)')
        self.db.commit()

    @synchronized
    def offset(self):
        row = self.db.execute('SELECT offset FROM cursor WHERE id=1').fetchone()
        return row[0] if row else 0

    @synchronized
    def acknowledge(self, value):
        self.db.execute('INSERT OR REPLACE INTO cursor VALUES (1, ?)', (value,))
        self.db.commit()

    @synchronized
    def claim(self, url, limit=60):
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM attempts WHERE url=?', (url,)).fetchone():
                return 'duplicate'
            if self.db.execute('SELECT COUNT(*) FROM attempts WHERE month=?', (month,)).fetchone()[0] >= min(limit, 60):
                return 'budget'
            self.db.execute('INSERT INTO attempts VALUES (?, ?, ?)', (url, month, 'running'))
        return 'started'

    @synchronized
    def finish(self, url, status):
        with self.db:
            self.db.execute('UPDATE attempts SET status=? WHERE url=?', (status, url))
