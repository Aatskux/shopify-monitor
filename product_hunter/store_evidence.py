"""Turn cited store-search hits into a bounded report, without inferring sales."""
from datetime import date, datetime, timezone
import re
from urllib.parse import urlsplit, urlunsplit

import tldextract

EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                              extra_suffixes=['myshopify.com'])
MARKER = re.compile(r'^VALIDOINTI\s*:\s*(EI_OSUMIA)?\s*$', re.M)
ROW = re.compile(r'^[^|\n]{1,60}\s*\|\s*(ID|KUVA|[A-Za-z0-9][A-Za-z0-9._-]{2,39})\s*\|\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|-)\s*\|\s*(.{1,180})$')
EXCLUDED = {'amazon', 'ebay', 'etsy', 'aliexpress', 'alibaba', 'temu', 'walmartmarketplace',
            'tiktok', 'instagram', 'facebook', 'pinterest', 'reddit', 'google', 'youtube'}


def site(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            return None
        d = EXTRACT(p.hostname.lower().rstrip('.'))
        if not d.domain or not d.suffix or d.domain in EXCLUDED:
            return None
        return d.domain + '.' + d.suffix
    except (ValueError, TypeError):
        return None


def cited_listing(url):
    if not isinstance(url, str) or re.search(r'\s', url) or not site(url):
        return None
    p = urlsplit(url)
    if not p.path.strip('/') or re.search(r'/(search|collections|category|categories)/?$', p.path, re.I):
        return None
    # Preserve query parameters: some storefronts identify the product there.
    clean = urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))
    return clean if len(clean) <= 250 else None


def split(result, original, caption="", has_photo=False):
    """Split off evidence rows; count only cited, distinct other storefront domains."""
    market_output, hits = [], {}
    original_site = site(original)
    completed = result.get('status') == 'completed'
    searched = any(i.get('type') == 'web_search_call' and i.get('status') == 'completed'
                   and (i.get('action') or {}).get('type') == 'search'
                   for i in result.get('output', []))
    saw_marker = False
    for item in result.get('output', []):
        parts = []
        for content in item.get('content', []):
            if content.get('type') != 'output_text':
                continue
            body = content.get('text', '')
            marker = MARKER.search(body)
            if not marker:
                parts.append(content)
                continue
            saw_marker = True
            annotations = content.get('annotations', [])
            prefix = body[:marker.start()]
            parts.append({**content, 'text': prefix, 'annotations': [
                a for a in annotations if type(a.get('end_index')) is int and a['end_index'] <= len(prefix)
            ]})
            if not (completed and searched and original_site) or marker.group(1):
                continue
            offset = marker.end()
            for line in body[offset:].splitlines(keepends=True):
                next_offset = offset + len(line)
                end = offset + len(line.rstrip('\r\n'))
                urls, edits = set(), []
                for a in annotations:
                    start, stop = a.get('start_index'), a.get('end_index')
                    if (a.get('type') == 'url_citation' and type(start) is int and type(stop) is int
                            and offset <= start <= stop <= end):
                        link = cited_listing(a.get('url'))
                        if link:
                            urls.add(link)
                            if a['url'] in body[start:stop]:
                                edits.append((start-offset, stop-offset))
                for start, stop in sorted(set(edits), reverse=True):
                    line = line[:start] + line[stop:]
                # Citation spans sometimes include the evidence phrase itself; remove only link markup.
                clean = re.sub(r'【[^】\n]*】|cite[^\n]*|\[\d+\]', '', line).strip()
                row = ROW.fullmatch(clean)
                offset = next_offset
                if not row or len(urls) != 1 or 'http' in row[3].lower():
                    continue
                method = row[1]
                if method == 'KUVA' and not has_photo:
                    continue
                if method not in ('ID', 'KUVA') and (
                        not any(c.isdigit() for c in method) or not re.search(
                            r'(?<![\w-])' + re.escape(method) + r'(?![\w-])', caption, re.I)):
                    continue
                url = urls.pop()
                domain = site(url)
                if domain == original_site:
                    continue
                announced = row[2]
                if announced != '-':
                    try:
                        if date.fromisoformat(announced) > datetime.now(timezone.utc).date():
                            announced = '-'
                    except ValueError:
                        announced = '-'
                candidate = (url, announced, row[3])
                if domain not in hits or (hits[domain][1] == '-' and announced != '-'):
                    hits[domain] = candidate
        if parts:
            market_output.append({**item, 'content': parts})
    return {**result, 'output': market_output}, hits, completed and searched and saw_marker and bool(original_site)


def section(hits, checked):
    if not checked:
        return 'Kauppavalidointi: hakutulosta ei voitu varmistaa.'
    if not hits:
        return 'Kauppavalidointi: haussa ei löytynyt riittävää näyttöä samasta tuotteesta muissa kaupoissa.'
    shown = list(hits.items())[:3]
    rows = [f'Kauppavalidointi: sama tuote AI-arvion mukaan {len(shown)} muulla kauppasivustolla.']
    for domain, (url, announced, basis) in shown:
        dated = f' · lähteen julkaisupäivä {announced}' if announced != '-' else ''
        rows.append(f'• {domain}: {basis[:78]}{dated}\n{url}')
    rows.append('Listaukset eivät todista myyntiä tai eri omistajia.')
    if all(item[1] == '-' for _, item in shown):
        rows.append('Julkaisupäiviä ei varmistettu.')
    return '\n'.join(rows)
