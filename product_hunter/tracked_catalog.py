"""Compare only products already indexed by the tracked-store monitor."""
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
import json
import os
import re
import tempfile
import time
from urllib.parse import urlsplit

FILE = 'tracked_catalog.json'
MAX_AGE = 1800
MAX_BYTES = 24 * 1024 * 1024
HANDLE = re.compile(r'[\w-]{1,180}\Z')
HASH = re.compile(r'[a-f0-9]{16}\Z')


def host(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
            return ''
        return (p.hostname or '').rstrip('.').removeprefix('www.')
    except (ValueError, TypeError, AttributeError):
        return ''


def excluded(url):
    name = host(url)
    return name == 'pilocea.com' or name.endswith('.pilocea.com')


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(data)
            out.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def remove_excluded(path):
    stores = json.loads(path.read_bytes())
    if not isinstance(stores, dict) or not stores:
        raise ValueError('Invalid tracked-store list')
    allowed = {url: currency for url, currency in stores.items() if not excluded(url)}
    if not allowed:
        raise ValueError('Tracked-store list would be empty')
    if allowed != stores:
        atomic_write(path, (json.dumps(allowed, indent=1) + '\n').encode())
        return True
    return False


def snapshot(directory):
    stores = json.loads((directory / 'stores.json').read_bytes())
    catalogue = json.loads((directory / 'image_hashes.json').read_bytes())['products']
    entries = {}
    for store in stores:
        if not host(store) or excluded(store):
            continue
        p = urlsplit(store)
        if p.path.strip('/') or p.query or p.fragment:
            continue
        entries[store] = [
            [row['handle'], str(row.get('name', ''))[:180], row.get('hash'), row.get('published_at')]
            for row in catalogue.get(store, {}).values()
            if isinstance(row.get('handle'), str) and HANDLE.fullmatch(row['handle'])
        ]
    if not entries:
        raise ValueError('No active stores')
    return json.dumps({'time': int(time.time()), 'stores': entries},
                      ensure_ascii=False, separators=(',', ':')).encode()


def words(title):
    return sorted(re.findall(r'[^\W_]+', title.casefold()))


def matches(data, product, now=None):
    now = time.time() if now is None else now
    if not 0 <= now - data['time'] <= MAX_AGE:
        return None
    original = host(product)
    if not original or excluded(product):
        return None
    path = urlsplit(product).path
    if not path.startswith('/products/'):
        return None
    handle = path.removeprefix('/products/').rstrip('/')
    stores = {url: rows for url, rows in data['stores'].items() if host(url) and not excluded(url)}
    source = next((row for url, rows in stores.items() if host(url) == original
                   for row in rows if row[0] == handle), None)
    if source is None:
        return None
    _, name, digest, _ = source
    tokens = words(name)
    t = ' '.join(tokens)
    numeric = {word for word in tokens if word.isdecimal()}
    best = {}
    for url, rows in stores.items():
        shop = host(url)
        if shop == original:
            continue
        for handle, title, other, published in rows:
            if not HANDLE.fullmatch(handle):
                continue
            distance = (bin(int(digest, 16) ^ int(other, 16)).count('1')
                        if isinstance(digest, str) and isinstance(other, str)
                        and HASH.fullmatch(digest) and HASH.fullmatch(other) else 99)
            candidate = words(title)
            same_name = len(set(tokens)) >= 2 and candidate == tokens
            if distance > 4 and not same_name:
                continue
            # Identical catalogue photos can be reused for another size or colour.
            # Conflicting numbers are never strong; changed titles with identical images are review candidates.
            other_numeric = {word for word in candidate if word.isdecimal()}
            similarity = SequenceMatcher(None, t, ' '.join(candidate), autojunk=False).ratio() if t else 0
            strong = distance <= 4 and len(set(tokens) & set(candidate)) >= 2 and similarity >= .85 and numeric == other_numeric
            if not (strong or distance == 0 or same_name):
                continue
            rank = (strong, -distance, similarity)
            if shop not in best or rank > best[shop][0]:
                best[shop] = (rank, shop, url.rstrip('/') + '/products/' + handle,
                              published, 'kuva + nimi' if strong else 'sama kuva' if distance == 0 else 'sama nimi')
    return len({host(url) for url in stores}), sorted(best.values(), reverse=True)


def section(product, directory=None):
    directory = directory or Path(os.getenv('MONITOR_DATA_DIR', '/data'))
    try:
        result = matches(json.loads((directory / FILE).read_bytes()), product)
    except (OSError, TypeError, ValueError, IndexError, KeyError):
        result = None
    if result is None:
        return 'Seurantalista: tuote tai tuore katalogi ei vielä ole vertailtavissa.'
    count, hits = result
    strong = sum(rank[0] for rank, *_ in hits)
    heading = f'Seurantalista ({count} kauppaa): {strong} vahvaa osumaa muissa kaupoissa'
    if len(hits) > strong:
        heading += f', {len(hits)-strong} mahdollista'
    lines = [heading + '.']
    for rank, shop, url, published, reason in hits[:3]:
        date = ''
        try:
            when = datetime.fromisoformat(published.replace('Z', '+00:00'))
            if when.tzinfo and when <= datetime.now(timezone.utc):
                date = ' · kaupan julkaisupäivä ' + when.date().isoformat()
        except (AttributeError, ValueError):
            pass
        lines.append(f'• {shop} · {"vahva" if rank[0] else "mahdollinen"}, {reason}{date}\n{url}')
    lines.append('Vertailu koskee indeksoituja listauksia; osuma ei todista myyntiä.')
    return '\n'.join(lines)
