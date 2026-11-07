import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import requests
from product_hunter import catalog_sync, tracked_catalog as catalogue

ORIGIN = 'https://original.com'
TARGET = ORIGIN + '/products/item'
ITEM = ['item', 'Bashful Beige Bunny 31 cm', '0a0a0a0a0a0a0a0a', '2025-05-01T10:00:00Z']


class CatalogueTests(unittest.TestCase):
    def test_snapshot_filters_removed_and_untracked_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stores = directory / 'stores.json'
            stores.write_text(json.dumps({ORIGIN: '$', 'https://www.pilocea.com/': '$', 'https://another.com': '$'}))
            row = dict(zip(('handle', 'name', 'hash', 'published_at'), ITEM))
            (directory / 'image_hashes.json').write_text(json.dumps({'products': {
                ORIGIN: {'1': row}, 'https://www.pilocea.com/': {'2': row}, 'https://untracked.com': {'3': row}
            }}))
            self.assertTrue(catalogue.remove_excluded(stores))
            self.assertEqual(set(json.loads(stores.read_bytes())), {ORIGIN, 'https://another.com'})
            self.assertEqual(set(json.loads(catalogue.snapshot(directory))['stores']), {ORIGIN, 'https://another.com'})

    def test_count_distinct_tracked_shops_and_show_store_publication_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / catalogue.FILE).write_text(json.dumps({'time': time.time(), 'stores': {
                ORIGIN: [ITEM], 'https://www.original.com': [ITEM],
                'https://other.com': [ITEM, ITEM], 'https://www.other.com': [ITEM],
                'https://pilocea.com': [ITEM], 'https://possible.com': [[ITEM[0], ITEM[1], None, None]],
                'https://third.com': [[ITEM[0], 'Bashful Beige Bunny 50 cm', ITEM[2], None]],
            }}))
            report = catalogue.section(TARGET, directory)
            self.assertIn('1 vahvaa osumaa', report)
            self.assertIn('2 mahdollista', report)
            self.assertIn('kaupan julkaisupäivä 2025-05-01', report)
            self.assertNotIn('pilocea', report)
            self.assertNotIn('original.com', report)
            self.assertIn('ei vielä ole', catalogue.section(ORIGIN + '/products/missing', directory))

    def test_expired_snapshot_not_used(self):
        self.assertIsNone(catalogue.matches({'time': 1, 'stores': {ORIGIN: [ITEM]}}, TARGET, now=9999))

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token'})
    def test_signed_private_transfer_and_replay_guard(self):
        if not socket.has_ipv6:
            self.skipTest('IPv6 unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            server = catalog_sync.start(directory, ('::1', 0))
            try:
                endpoint = f'http://[::1]:{server.server_port}{catalog_sync.ROUTE}'
                now = int(time.time())
                body = json.dumps({'time': now, 'stores': {ORIGIN: [ITEM]}}).encode()
                stale = json.dumps({'time': now-1, 'stores': {}}).encode()
                with requests.Session() as client:
                    client.trust_env = False
                    for payload, signature, expected in (
                        (body, '0'*64, 403), (body, catalog_sync.sign(body), 204),
                        (stale, catalog_sync.sign(stale), 409),
                    ):
                        resp = client.post(endpoint, data=payload, headers={'X-Catalog-Signature': signature}, timeout=3)
                        self.assertEqual(resp.status_code, expected)
                self.assertEqual((directory / catalogue.FILE).read_bytes(), body)
            finally:
                server.shutdown()
                server.server_close()
