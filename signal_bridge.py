"""Optional Signal launch feed with a durable JSON outbox (no credentials stored)."""
import json
import os
import pathlib
import hashlib
import time
import urllib.request
import urllib.parse

OUTBOX=pathlib.Path('signal_outbox.json')

def configured():
    return bool(os.environ.get('SIGNAL_URL') and os.environ.get('SIGNAL_FEED_TOKEN'))

def _read():
    if not OUTBOX.exists():return {}
    data=json.loads(OUTBOX.read_text())
    if not isinstance(data,dict):raise ValueError('Invalid Signal outbox')
    return data

def _write(data):
    temp=OUTBOX.with_suffix('.json.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temp.replace(OUTBOX)

def enqueue_launch(store,p):
    base=urllib.parse.urlsplit(store)
    if base.scheme not in ('http','https') or not base.hostname:raise ValueError('Invalid store URL')
    handle=p.get('handle')
    if not handle or not p.get('title'):raise ValueError('Product title and handle required')
    event='shopify:'+hashlib.sha256((store.rstrip('/')+'/'+str(p.get('id',handle))).encode()).hexdigest()
    image=p.get('image') or next(iter(p.get('images',[])),{})
    if isinstance(image,dict):image=image.get('src','')
    payload={'event_id':event,'product':{'name':p['title'],'url':store.rstrip('/')+'/products/'+urllib.parse.quote(handle,safe=''),'image':image or '', 'competitor':base.hostname,'keywords':p['title'],'launch_at':p.get('published_at'),'source':'shopify-monitor launch'}}
    data=_read()
    if event not in data:
        data[event]=payload;_write(data)
    return event

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def flush(limit=20,budget=30):
    if not configured():return 0
    base=os.environ['SIGNAL_URL'].rstrip('/');target=urllib.parse.urlsplit(base)
    if target.scheme!='https' and not (target.scheme=='http' and target.hostname in ('127.0.0.1','localhost')):
        raise ValueError('Signal requires HTTPS except on localhost')
    if target.username or target.password or target.query or target.fragment:raise ValueError('Invalid Signal base URL')
    data=_read();count=0;deadline=time.monotonic()+budget
    opener=urllib.request.build_opener(NoRedirect())
    for event,payload in list(data.items())[:limit]:
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        try:
            req=urllib.request.Request(base+'/api/feed',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['SIGNAL_FEED_TOKEN']})
            with opener.open(req,timeout=min(3,remaining)) as r:
                if r.status not in (200,201,202):raise ValueError('Unexpected Signal response')
                result=json.loads(r.read(100000))
                if not (result.get('id') or result.get('status')=='duplicate'):raise ValueError('Unexpected Signal acknowledgement')
            # Persist delivery before the next request. Replaying after a crash is safe via event_id.
            del data[event];_write(data);count+=1
        except Exception as e:
            print('[signal] Delivery deferred: '+type(e).__name__)
            # Endpoint is down; preserve all pending messages and retry on the next monitor run.
            break
    return count
