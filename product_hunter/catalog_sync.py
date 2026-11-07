"""Send the tracked catalogue over Railway's private network, authenticated by HMAC."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import hmac
import json
import os
import socket
import threading
import time

import requests

from .tracked_catalog import FILE, MAX_BYTES, atomic_write, snapshot

PORT = 8124
ROUTE = '/tracked-catalog'
DESTINATION = f'http://product-research.railway.internal:{PORT}{ROUTE}'


def sign(body):
    key = os.environ['TELEGRAM_BOT_TOKEN'].encode()
    return hmac.new(key, b'tracked-catalog-v1\0' + body, hashlib.sha256).hexdigest()


def publish(directory):
    if not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('RAILWAY_ENVIRONMENT_ID'):
        return False
    body = snapshot(directory)
    if len(body) > MAX_BYTES:
        raise ValueError('Catalogue exceeds transfer limit')
    with requests.Session() as session:
        session.trust_env = False
        result = session.post(DESTINATION, data=body,
                              headers={'X-Catalog-Signature': sign(body)},
                              timeout=(3, 10), allow_redirects=False)
    if result.status_code != 204:
        raise RuntimeError('Catalogue was not accepted')
    return True


class PrivateServer(HTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def start(directory, address=('::', PORT)):
    destination = directory / FILE

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(8)
            super().setup()

        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != ROUTE:
                self.send_error(404)
                return
            signature = self.headers.get('X-Catalog-Signature', '')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BYTES or not signature.isascii() or len(signature) != 64:
                    self.send_error(400)
                    return
                body = self.rfile.read(size)
                if len(body) != size or not hmac.compare_digest(signature, sign(body)):
                    self.send_error(403)
                    return
                data = json.loads(body)
                if type(data['time']) is not int or abs(time.time() - data['time']) > 120 or not isinstance(data['stores'], dict):
                    self.send_error(400)
                    return
                if destination.exists() and data['time'] < json.loads(destination.read_bytes())['time']:
                    self.send_error(409)
                    return
                atomic_write(destination, body)
            except (OSError, TypeError, ValueError, KeyError):
                self.send_error(400)
                return
            print(f'[catalog] synced {len(data["stores"])} tracked stores', flush=True)
            self.send_response(204)
            self.end_headers()

    server = PrivateServer(address, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
