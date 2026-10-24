"""Telegram notification transport. Never log the bot token."""
import os
import requests


def notify_new_product(store, product, *, dry_run=False):
    """Best-effort delivery; missing configuration and dry runs send nothing.

    Do not print exception text: requests errors can contain the token URL.
    No automatic retry after ambiguous failures, to avoid duplicate messages.
    """
    if dry_run or not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        return False
    try:
        send_message(new_product_message(store, product))
    except Exception:
        print("[telegram] Notification failed; details omitted to protect credentials")
        return False
    return True


def send_message(text, *, token=None, chat_id=None, timeout=15):
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": False},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected the message")
    return data["result"]


def new_product_message(store, product):
    title = product.get("title") or "Unnamed product"
    handle = product.get("handle") or ""
    image = ((product.get("images") or [{}])[0] or {}).get("src") or ""
    return (
        f"🆕 NEW PRODUCT\n{title}\n"
        f"Store: {store}\n"
        f"Product: {store.rstrip('/')}/products/{handle}\n"
        f"Image: {image or 'Not available'}\n"
        "AI research: not connected; TikTok virality unknown"
    )

