"""Public Shopify availability probe. Never infer why a product is unavailable."""
import json, socket, ipaddress, urllib.request, urllib.parse, http.client, ssl
from datetime import datetime, timezone

def checked(): return datetime.now(timezone.utc).isoformat()

def public_target(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('Public HTTPS product URL required')
    infos=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
    addresses=list(dict.fromkeys(i[4][0] for i in infos))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('Private or reserved address denied')
    return p, addresses[0]

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address):
        super().__init__(host,timeout=12,context=ssl.create_default_context());self.address=address
    def connect(self):
        raw=socket.create_connection((self.address,443),self.timeout)
        self.sock=self._context.wrap_socket(raw,server_hostname=self.host)

def public_json(url):
    # Resolve and validate every redirect, then pin to the validated address (DNS rebinding guard).
    for _ in range(4):
        p,address=public_target(url); c=PinnedHTTPS(p.hostname,address)
        try:
            c.request('GET',urllib.parse.urlunsplit(('','',p.path,p.query,'')),headers={'User-Agent':'SignalProductRadar/0.2','Accept':'application/json'})
            r=c.getresponse()
            if r.status in (301,302,303,307,308):
                location=r.getheader('Location')
                if not location: raise ValueError('Missing redirect URL')
                url=urllib.parse.urljoin(url,location);continue
            if r.status!=200: raise ValueError('HTTP '+str(r.status))
            content=r.read(2_000_001)
            if len(content)>2_000_000: raise ValueError('Response too large')
            return json.loads(content),url
        finally:c.close()
    raise ValueError('Too many redirects')

def summarize(payload,url):
    variants=payload.get('variants',[])
    flags=[v.get('available') for v in variants if isinstance(v,dict)]
    status='unknown'
    if flags and len(flags)==len(variants) and all(type(x) is bool for x in flags):
        status='in_stock' if all(flags) else 'partial' if any(flags) else 'sold_out'
    # Product-level available guards against truncated variants.
    if payload.get('available') is True and status=='sold_out':status='partial'
    if payload.get('available') is False:status='sold_out'
    return {'status':status,'checked_at':checked(),'source':'Shopify public product endpoint','evidence_url':url,'variants_observed':len(flags),'unavailable_variants':sum(x is False for x in flags),'reason':'Saatavuus havaittu verkkokaupassa; loppuunmyynnin syy ei ole tiedossa.'}

def check_stock(url):
    result={'status':'unknown','checked_at':checked(),'source':'Shopify public product endpoint'}
    if not url:return dict(result,reason='Tuote-URL puuttuu.')
    try:
        p=urllib.parse.urlsplit(url)
        if not __import__('re').search(r'/products/[^/]+/?$',p.path):return dict(result,reason='Shopify-tuotepolkua ei tunnistettu.')
        endpoint=urllib.parse.urlunsplit((p.scheme,p.netloc,p.path.rstrip('/').removesuffix('.js')+'.js','',''))
        payload,final=public_json(endpoint)
        if not isinstance(payload,dict):raise ValueError('Invalid product response')
        return summarize(payload,final)
    except Exception:
        return dict(result,reason='Saatavuutta ei voitu vahvistaa: kauppa estää haun tai vastaus ei ole tuotedataa.')
