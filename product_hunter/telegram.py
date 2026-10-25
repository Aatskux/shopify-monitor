"""Optional Telegram delivery. Never log request URLs or credentials."""
import os
from urllib.parse import urlsplit
import requests


class TelegramRejected(RuntimeError):
    def __init__(self, code):
        super().__init__('Telegram rejected request')
        self.code = code


def _clip(text, units):
    # Telegram measures entity offsets/lengths in UTF-16 units.
    return text.encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore')


def _call(method, payload, *, token=None, timeout=15):
    token = token or os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise RuntimeError('Telegram is not configured')
    response = requests.post('https://api.telegram.org/bot' + token + '/' + method,
                             json=payload, timeout=timeout, allow_redirects=False)
    if response.status_code >= 400:
        raise TelegramRejected(response.status_code)
    response.raise_for_status()
    data = response.json()
    if data.get('ok') is not True:
        raise TelegramRejected(data.get('error_code'))
    return data['result']


def _chat_id(chat_id=None):
    value = chat_id or os.getenv('TELEGRAM_CHAT_ID')
    if not value:
        raise RuntimeError('Telegram is not configured')
    return value


def validate_destination():
    """Fail closed if a configured private group becomes public or gains members."""
    if os.getenv('TELEGRAM_PRIVATE_GROUP_ONLY') != 'true':
        return
    payload = {'chat_id': _chat_id()}
    chat = _call('getChat', payload)
    if chat.get('type') not in ('group', 'supergroup') or chat.get('username') or chat.get('active_usernames'):
        raise RuntimeError('Destination must be a private group')
    count = _call('getChatMemberCount', payload)
    if type(count) is not int or count != 3:
        raise RuntimeError('Destination must contain exactly two people and the bot')


def _image(product):
    image = ((product.get('images') or [{}])[0] or {}).get('src')
    if image and image.startswith('//'):
        image = 'https:' + image
    if not isinstance(image, str):
        return None
    url = urlsplit(image)
    return image if url.scheme == 'https' and url.hostname and not url.username and not url.password else None


def notify_new_product(store, product, *, dry_run=False):
    if dry_run or not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'):
        return False
    try:
        validate_destination()
        text = new_product_message(store, product)
        from .manual import keyboard
        markup = keyboard(text)
        image = _image(product)
        if image:
            try:
                send_photo(image, text, reply_markup=markup)
            except TelegramRejected as error:
                # Only a definite 400 rejection can fall back without risking duplicates.
                if error.code != 400:
                    raise
                send_message(text, reply_markup=markup)
        else:
            send_message(text, reply_markup=markup)
    except Exception:
        print('[telegram] Notification failed; details omitted to protect credentials')
        return False
    return True


def send_message(text, *, token=None, chat_id=None, timeout=15, reply_markup=None):
    return _call('sendMessage', {'chat_id': _chat_id(chat_id), 'text': _clip(text, 4096),
                 'link_preview_options': {'is_disabled': True}, 'protect_content': True, **({'reply_markup': reply_markup} if reply_markup else {})},
                 token=token, timeout=timeout)


def send_photo(image, caption, *, token=None, chat_id=None, timeout=15, reply_markup=None):
    return _call('sendPhoto', {'chat_id': _chat_id(chat_id), 'photo': image,
                 'caption': _clip(caption, 1024), 'protect_content': True, **({'reply_markup': reply_markup} if reply_markup else {})},
                 token=token, timeout=timeout)


def new_product_message(store, product):
    title = _clip(str(product.get('title') or 'Nimetön tuote'), 180)
    handle = product.get('handle') or ''
    return (f'🆕 UUSI TUOTE\n{title}\n'
            f'Kauppa: {store}\n'
            f'Tuote: {store.rstrip("/")}/products/{handle}')
