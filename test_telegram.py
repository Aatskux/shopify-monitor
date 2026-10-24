"""Offline Telegram transport and new-product integration regressions."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import requests
import shopify_monitor as monitor
from product_hunter import telegram


CONFIG = {"TELEGRAM_BOT_TOKEN": "fake-secret", "TELEGRAM_CHAT_ID": "123"}
PRODUCT = {"id": 2, "title": "<b>Test</b>", "handle": "test"}


class TelegramTests(unittest.TestCase):
    @patch.dict(os.environ, CONFIG, clear=True)
    @patch.object(telegram.requests, "post")
    def test_transport_plain_text_timeout_and_length(self, post):
        post.return_value.json.return_value = {"ok": True, "result": {"message_id": 1}}
        self.assertEqual(telegram.send_message("x" * 5000), {"message_id": 1})
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.telegram.org/botfake-secret/sendMessage")
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(len(kwargs["json"]["text"]), 4096)
        self.assertNotIn("parse_mode", kwargs["json"])
        post.return_value.raise_for_status.assert_called_once()

    @patch.object(telegram, "send_message")
    def test_disabled_partial_configuration_and_dry_run(self, send):
        for config in ({}, {"TELEGRAM_BOT_TOKEN": "fake-secret"},
                       {"TELEGRAM_CHAT_ID": "123"}):
            with patch.dict(os.environ, config, clear=True):
                self.assertFalse(telegram.notify_new_product("https://shop", PRODUCT))
        with patch.dict(os.environ, CONFIG, clear=True):
            self.assertFalse(telegram.notify_new_product("https://shop", PRODUCT, dry_run=True))
        send.assert_not_called()

    @patch.dict(os.environ, CONFIG, clear=True)
    @patch.object(telegram.requests, "post")
    def test_failures_are_isolated_and_redacted(self, post):
        for error in (requests.Timeout("fake-secret"), requests.ConnectionError("fake-secret"),
                      requests.HTTPError("botfake-secret/sendMessage"), ValueError("fake-secret")):
            post.side_effect = error
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertFalse(telegram.notify_new_product("https://shop", PRODUCT))
            self.assertNotIn("fake-secret", output.getvalue())

    @patch.dict(os.environ, CONFIG, clear=True)
    @patch.object(telegram.requests, "post")
    def test_api_rejection(self, post):
        post.return_value.json.return_value = {"ok": False}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(telegram.notify_new_product("https://shop", PRODUCT))

    def test_message_is_honest_about_research(self):
        text = telegram.new_product_message("https://shop/", PRODUCT)
        self.assertIn("https://shop/products/test", text)
        self.assertIn("virality unknown", text)
        self.assertNotIn("pending", text)


class IntegrationTests(unittest.TestCase):
    def run_round(self, seen, *, dry_run=False, discord_error=False, telegram_error=False):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, CONFIG, clear=True))
            stack.enter_context(patch.object(monitor, "STORES", {"https://shop": "$"}))
            for name in ("STATE", "MISSING_FILE", "BEST_SELLERS", "MATCHES_FILE",
                         "IMAGE_HASHES", "SUMMARY_STATE"):
                stack.enter_context(patch.object(monitor, name, monitor.pathlib.Path(folder) / name))
            stack.enter_context(patch.object(monitor, "fetch_products", return_value=[PRODUCT]))
            stack.enter_context(patch.object(monitor, "update_image_hashes", return_value=[]))
            stack.enter_context(patch.object(monitor, "check_best_sellers", return_value=[]))
            stack.enter_context(patch.object(monitor.time, "sleep"))
            discord = stack.enter_context(patch.object(monitor, "notify"))
            send = stack.enter_context(patch.object(telegram, "send_message"))
            if telegram_error:
                send.side_effect = requests.Timeout("fake-secret")
            if discord_error:
                discord.side_effect = RuntimeError("Discord unavailable")
            with contextlib.redirect_stdout(io.StringIO()):
                monitor.check_all(seen, webhook="fake-webhook", dry_run=dry_run)
            return discord, send

    def test_new_product_and_seen_deduplication(self):
        seen = {"https://shop": {"1"}}
        discord, send = self.run_round(seen)
        discord.assert_called_once()
        send.assert_called_once()
        discord, send = self.run_round(seen)
        discord.assert_not_called()
        send.assert_not_called()

    def test_baseline_and_dry_run(self):
        for seen, dry_run in (({}, False), ({"https://shop": set()}, True)):
            _, send = self.run_round(seen, dry_run=dry_run)
            send.assert_not_called()

    def test_discord_failure_does_not_block_telegram(self):
        _, send = self.run_round({"https://shop": set()}, discord_error=True)
        send.assert_called_once()

    def test_telegram_failure_keeps_discord_and_seen(self):
        seen = {"https://shop": set()}
        discord, send = self.run_round(seen, telegram_error=True)
        discord.assert_called_once()
        send.assert_called_once()
        self.assertIn("2", seen["https://shop"])


if __name__ == "__main__":
    unittest.main()
