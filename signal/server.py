import os, json, sqlite3, uuid, math, re, threading, urllib.request, urllib.parse, time, hmac
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
ROOT=Path(__file__).parent
DB=Path(os.environ.get('RADAR_DB', str(ROOT/'radar.sqlite3')))
LOCK=threading.RLock()
def now(): return datetime.now(timezone.utc)
def stamp(): return now().isoformat()
def date(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    with conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, data TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, product_id TEXT, data TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS jobs(product_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, next_at TEXT, error TEXT)')
        c.execute("UPDATE jobs SET status='pending' WHERE status='running'")

def safe_url(s):
    if s and urllib.parse.urlparse(s).scheme not in ('http','https'): raise ValueError('URL must use http or https')
    return s

def classify(text):
    text=text.lower()
    categories={'Fashion':('top','pants','dress','shirt','hoodie','co-ord','housut','toppi'), 'Beauty':('serum','makeup','skincare','lipstick'), 'Fitness':('workout','fitness','gym'), 'Tech':('charger','headphone','keyboard'), 'Home':('lamp','kitchen','cleaner','storage')}
    return next((n for n,terms in categories.items() if any(re.search(r'\b'+re.escape(t)+r'\b',text) for t in terms)), 'Unclassified')

def product(p, event_id=None):
    if not isinstance(p,dict): raise ValueError('product must be an object')
    name=str(p.get('name','')).strip()[:300]
    if not name: raise ValueError('Product name is required; use a descriptive name for image-only cases')
    obj={'id':str(uuid.uuid4()),'name':name,'url':safe_url(p.get('url','')),'image':safe_url(p.get('image','')),'competitor':str(p.get('competitor','Manual'))[:200], 'niche':str(p.get('niche') or classify(name+' '+str(p.get('keywords',''))))[:100], 'keywords':str(p.get('keywords',name))[:500], 'created_at':stamp(),'demo':bool(p.get('demo',False)), 'availability':{'status':'unknown'}, 'validation_status':'pending','launch_at':p.get('launch_at'),'source':str(p.get('source','manual'))[:200]}
    if obj['launch_at'] and date(obj['launch_at'])>now(): raise ValueError('launch_at cannot be in the future')
    with LOCK, conn() as c:
        if event_id and c.execute('SELECT 1 FROM events WHERE id=?',(event_id,)).fetchone(): return {'status':'duplicate'}
        if obj['url']:
            normalized=urllib.parse.urlsplit(obj['url']); canonical=urllib.parse.urlunsplit((normalized.scheme.lower(),normalized.netloc.lower(),normalized.path.rstrip('/'),'',''))
            obj['canonical_url']=canonical
            existing=next((json.loads(r['data']) for r in c.execute('SELECT data FROM products') if json.loads(r['data']).get('canonical_url')==canonical),None)
            if existing:
                if event_id: c.execute('INSERT INTO events VALUES (?)',(event_id,))
                return dict(existing,status='existing')
        c.execute('INSERT INTO products VALUES (?,?)',(obj['id'],json.dumps(obj)))
        if event_id: c.execute('INSERT INTO events VALUES (?)',(event_id,))
        if not obj['demo']: c.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(obj['id'],'pending',0,stamp(),''))
    return obj

def video(pid,v):
    with conn() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone(): raise ValueError('Unknown product')
    if v.get('match','unverified') not in ('unverified','confirmed','rejected'): raise ValueError('match must be unverified, confirmed or rejected')
    url=safe_url(v.get('url',''))
    if not url: raise ValueError('Video URL required')
    parsed_url=urllib.parse.urlsplit(url)
    if not v.get('demo') and (parsed_url.hostname not in ('www.tiktok.com','tiktok.com') or not re.fullmatch(r'/@[^/]+/video/\d+/?',parsed_url.path)): raise ValueError('Use the canonical TikTok /@creator/video/id URL')
    pub=v.get('published_at'); observed=v.get('observed_at',stamp())
    if not pub: raise ValueError('published_at required')
    if date(pub)>now() or date(observed)>now()+timedelta(minutes=5): raise ValueError('Dates cannot be in the future')
    metrics={}
    for k in ('likes','comments_count','shares','views'):
        val=v.get(k); metrics[k]=None if val is None else int(val)
        if metrics[k] is not None and metrics[k]<0: raise ValueError('Metrics cannot be negative')
    if date(observed)<date(pub): raise ValueError('Observation precedes publication')
    comments=v.get('comments',[])
    if not isinstance(comments,list) or any(not isinstance(x,str) for x in comments): raise ValueError('comments must be a list of text')
    vid=str(uuid.uuid5(uuid.NAMESPACE_URL,pid+url))
    with LOCK, conn() as c:
        row=c.execute('SELECT data FROM videos WHERE id=?',(vid,)).fetchone()
        obj=json.loads(row['data']) if row else {'id':vid,'product_id':pid,'url':url,'history':[]}
        if obj['history'] and date(observed)<=date(obj['history'][-1]['observed_at']): raise ValueError('New observation must be later than previous observation')
        obj.update({'published_at':pub,'source':str(v.get('source','manual'))[:200],'demo':bool(v.get('demo',False)), 'comments':comments[:1000], 'match':v.get('match','unverified')})
        obj['history'].append(dict(metrics,observed_at=observed)); obj['history']=obj['history'][-100:]
        c.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?)',(vid,pid,json.dumps(obj)))
    return obj

INTENT=('where can i buy','where to buy','link please','need this','ordered','buy this','mistä saa','mistä ost','haluan','haluun','mistä mä voin ostaa','i want this','ostaisin','tilasin','linkki')
INTEREST=('love','cute','beautiful','ihana','upea','looks good','want')
NEGATIVE=('scam','bad quality','huijaus','poor quality','expensive','kallis')
def analysis(v):
    h=v['history']; latest=h[-1]; age=max(0,(now()-date(v['published_at'])).total_seconds()/86400)
    comments=v['comments']; counts={k:0 for k in ('intent','interest','negative')}; examples={k:[] for k in counts}
    for text in comments:
        t=text.lower()
        label=None
        if any(x in t for x in NEGATIVE) or re.search(r"(?:don't|do not|dont|en|älä)\s+(?:want|buy|halua|osta)",t): label='negative'
        elif any(x in t for x in INTENT): label='intent'
        elif any(x in t for x in INTEREST): label='interest'
        if label:
            counts[label]+=1
            if len(examples[label])<3: examples[label].append(text[:300])
    growth=None
    if len(h)>1 and h[-2]['likes'] is not None and latest['likes'] is not None:
        hours=(date(latest['observed_at'])-date(h[-2]['observed_at'])).total_seconds()/3600
        delta=latest['likes']-h[-2]['likes']
        if hours>0 and delta>=0: growth=delta/hours
    freshness=30*max(0,1-age/30)
    velocity=30*min(1,math.log1p(growth)/math.log1p(3000)) if growth is not None else 0
    intent=25*counts['intent']/len(comments) if comments else 0
    engagement=15*min(1,((latest['likes'] or 0)+(latest['shares'] or 0)*2)/latest['views']/.15) if latest['views'] else 0
    score=round(max(0,freshness+velocity+intent+engagement))
    return dict(age_days=round(age,1),growth_per_hour=round(growth,1) if growth is not None else None,comment_analysis=dict(counts,sample_size=len(comments),examples=examples),score=score,components=dict(freshness=round(freshness,1),velocity=round(velocity,1),intent=round(intent,1),engagement=round(engagement,1)),confidence='limited' if growth is None or not comments or not latest['views'] or v['match']!='confirmed' else 'observed',metrics=latest,stale_hours=round((now()-date(latest['observed_at'])).total_seconds()/3600,1))

def state():
    with conn() as c:
        ps=[json.loads(r['data']) for r in c.execute('SELECT data FROM products')]
        vs=[json.loads(r['data']) for r in c.execute('SELECT data FROM videos')]
    for v in vs: v['analysis']=analysis(v)
    for p in ps:
        related=[v for v in vs if v['product_id']==p['id']]
        p['score']=max([v['analysis']['score'] for v in related],default=0)
        p['video_count']=len(related)
        p['opportunity']=opportunity(p,related)
        p['score']=p['opportunity']['score']
    return dict(products=sorted(ps,key=lambda p:(p.get('demo',False),-p['score'],-date(p.get('launch_at') or p['created_at']).timestamp())),videos=vs,provider_configured=bool(os.environ.get('TIKTOK_PROVIDER_URL')),feed_configured=bool(os.environ.get('RADAR_FEED_TOKEN')))

def search(p):
    endpoint=os.environ.get('TIKTOK_PROVIDER_URL')
    if not endpoint: return dict(status='manual_search_required',search_url='https://www.tiktok.com/search?q='+urllib.parse.quote(p['keywords']))
    safe_url(endpoint)
    req=urllib.request.Request(endpoint,data=json.dumps(dict(query=p['keywords'],image_url=p['image'],product_name=p['name'],product_url=p['url'],recent_days=14,include_comments=True,known_video_urls=[v['url'] for v in state()['videos'] if v['product_id']==p['id'] and not v.get('demo')],limit=20)).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ.get('TIKTOK_PROVIDER_TOKEN','')})
    with urllib.request.urlopen(req,timeout=25) as response: payload=json.loads(response.read(2_000_000))
    found=[]; errors=[]
    for item in payload.get('videos',[])[:20]:
        try: found.append(video(p['id'],item))
        except (ValueError,TypeError,KeyError) as e: errors.append(str(e))
    return dict(status='imported',count=len(found),errors=errors)

WAKE=threading.Event()
def patch_product(pid, changes):
    with LOCK, conn() as c:
        row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
        if not row: raise ValueError('Unknown product')
        p=json.loads(row['data']); p.update(changes)
        c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(p),pid))
    return p

def enqueue(pid):
    with LOCK, conn() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone(): raise ValueError('Unknown product')
        job=c.execute('SELECT status FROM jobs WHERE product_id=?',(pid,)).fetchone()
        if job and job['status']=='running': return
        c.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?)',(pid,'pending',0,stamp(),''))
        row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
        p=json.loads(row['data']);p['validation_status']='pending'
        c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(p),pid))
    WAKE.set()

def opportunity(p,vs):
    if p.get('demo'):
        return dict(score=max([v['analysis']['score'] for v in vs],default=0),label='Esimerkkisignaali · ei markkinanäyttöä',probability=None,confidence='low',stage='Synteettinen case',reasons=['91k tykkäyksen ja 6 päivän esimerkki havainnollistaa laskentaa.'],risks=['Luvut ja kommentit ovat esimerkkidataa.'],components={},sample_size=0,intent_rate=None,stock_status='unknown')
    # Exclude demonstrations and unverified product matches from commercial evidence.
    matched=[v for v in vs if not v.get('demo') and v.get('match')=='confirmed']
    usable=[v for v in matched if v['analysis']['stale_hours']<=24]
    risks=[]; reasons=[]
    stock=p.get('availability',{'status':'unknown'}); stock_status=stock.get('status','unknown')
    if stock_status=='sold_out': risks.append('Kauppa ei tarjoa tuotetta ostettavaksi; se ei todista loppuunmyynnin syytä tai kysyntää.')
    if stock_status=='partial': reasons.append('Osa varianteista on ostettavissa, osa ei.')
    if not usable:
        return dict(score=0,label='Näyttö puuttuu',probability=None,confidence='low',stage='Tuntematon',reasons=['Tuoreita, samaan tuotteeseen varmennettuja videoita ei ole.'],risks=risks+['Videoiden osumat ja mittarit on vahvistettava.'],components={},sample_size=0,intent_rate=None,stock_status=stock_status)
    best=max(usable,key=lambda v:v['analysis']['score']); a=best['analysis']; age=a['age_days']
    stage='Varhainen havainto' if age<=3 else 'Aktiivinen ikkuna' if age<=7 else 'Myöhäinen / tarkista kasvu' if age<=14 else 'Vanha havainto'
    if age>7: risks.append('Havainto on yli viikon vanha; lyhyen trendin myynti-ikkuna voi sulkeutua.')
    fresh=25*max(0,1-age/14)
    velocity=a['components']['velocity']/30*30
    total=sum(v['analysis']['comment_analysis']['sample_size'] for v in usable)
    intent=sum(v['analysis']['comment_analysis']['intent'] for v in usable)
    neg=sum(v['analysis']['comment_analysis']['negative'] for v in usable)
    # Shrink tiny comment samples instead of awarding full demand points from one comment.
    intent_points=25*(intent/total if total else 0)*min(1,total/30)
    engagement=a['components']['engagement']
    # Availability is only a supporting signal when independent purchase intent and growth exist.
    stock_fresh=stock.get('checked_at') and (now()-date(stock['checked_at'])).total_seconds()<86400
    stock_points=5 if stock_status=='sold_out' and stock_fresh and total>=30 and intent/total>=.15 and (a['growth_per_hour'] or 0)>0 else 0
    negative_penalty=15*neg/total if total else 0
    score=round(max(0,min(100,fresh+velocity+intent_points+engagement+stock_points-negative_penalty)))
    if a['growth_per_hour'] is None: risks.append('Kasvua ei ole vielä mitattu kahdesta havaintopisteestä.')
    else: reasons.append('Mitattu kasvu: '+str(a['growth_per_hour'])+' tykkäystä/tunti.')
    if total<30: risks.append('Kommenttiotos on pieni: '+str(total)+' kommenttia, tavoite vähintään 30.')
    if total: reasons.append(str(intent)+' / '+str(total)+' kommentista sisältää ostoaikomuksen avainsanoja.')
    if stock_status=='unknown': risks.append('Saatavuutta ei voitu vahvistaa.')
    if not stock_fresh: risks.append('Saatavuustieto puuttuu tai on yli vuorokauden vanha.')
    if len(usable)==1: risks.append('Näyttö perustuu vain yhteen varmennettuun videoon.')
    reasons.append('Tuorein pisteytykseen valittu julkaisu: '+str(age)+' päivää.')
    high_quality=a['growth_per_hour'] is not None and total>=30 and len(usable)>=2 and bool(a['metrics']['views'])
    label='Vahva signaali · tarkista sourcaus' if score>=70 and high_quality else 'Lupaava · lisänäyttöä tarvitaan' if score>=40 else 'Heikko / myöhäinen signaali'
    return dict(score=score,label=label,probability=None,confidence='medium' if high_quality else 'low',stage=stage,reasons=reasons,risks=risks,components=dict(freshness=round(fresh,1),velocity=round(velocity,1),intent=round(intent_points,1),engagement=round(engagement,1),availability=stock_points,negative_penalty=round(negative_penalty,1)),sample_size=total,intent_rate=round(intent/total*100,1) if total else None,stock_status=stock_status)

def validate_product(pid):
    p=patch_product(pid,{'validation_status':'running'})
    if p.get('demo'):
        patch_product(pid,{'validation_status':'demo'}); return 'complete',[]
    errors=[]
    from stock import check_stock
    availability=check_stock(p.get('url',''))
    patch_product(pid,{'availability':availability})
    try:
        result=search(p)
        errors=result.get('errors',[])
        status='needs_provider' if result['status']=='manual_search_required' else 'partial' if errors else 'complete'
    except Exception:
        result={'status':'provider_error'};status='failed';errors=['Videohaku epäonnistui; tarkista data-adapteri.']
    patch_product(pid,{'validation_status':status,'validated_at':stamp(),'validation_errors':errors,'search_status':result['status']})
    return status,errors

def run_one_job():
    with LOCK, conn() as c:
        row=c.execute("SELECT * FROM jobs WHERE status IN ('pending','waiting','failed') AND next_at<=? ORDER BY next_at LIMIT 1",(stamp(),)).fetchone()
        if not row: return False
        pid=row['product_id']; attempts=row['attempts']+1
        c.execute("UPDATE jobs SET status='running',attempts=? WHERE product_id=?",(attempts,pid))
    try: status,errors=validate_product(pid)
    except Exception:
        status,errors='failed',['Validointityö epäonnistui.']
        patch_product(pid,{'validation_status':status,'validation_errors':errors})
    delay=min(3600,60*2**min(attempts,6)) if status=='failed' else max(60,int(os.environ.get('RADAR_REFRESH_SECONDS','21600')))
    with conn() as c:
        c.execute('UPDATE jobs SET status=?,next_at=?,error=? WHERE product_id=?',('failed' if status=='failed' else 'waiting',(now()+timedelta(seconds=delay)).isoformat(),'; '.join(errors),pid))
    return True

def worker():
    while True:
        try:
            if run_one_job(): continue
        except Exception as e: print('Worker:',type(e).__name__,flush=True)
        WAKE.wait(5); WAKE.clear()

class Handler(BaseHTTPRequestHandler):
    def reply(self,obj,status=200):
        body=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path=='/api/state': return self.reply(state())
        path=ROOT/'static'/('index.html' if self.path=='/' else self.path.lstrip('/'))
        if path.resolve().parent!=(ROOT/'static').resolve() or not path.is_file(): return self.reply({'error':'Not found'},404)
        content=path.read_bytes(); self.send_response(200); self.send_header('Content-Type',{'html':'text/html','js':'text/javascript','css':'text/css'}.get(path.suffix[1:],'text/plain')); self.end_headers(); self.wfile.write(content)
    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc!=self.headers.get('Host'): return self.reply({'error':'Cross-origin request denied'},403)
            length=int(self.headers.get('Content-Length','0'))
            if length>2_000_000: return self.reply({'error':'Payload too large'},413)
            p=json.loads(self.rfile.read(length))
            if self.path=='/api/products':
                obj=product(p); WAKE.set(); return self.reply(obj,201)
            if self.path=='/api/videos': return self.reply(video(p['product_id'],p),201)
            if self.path=='/api/feed':
                token=os.environ.get('RADAR_FEED_TOKEN')
                if not token or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token): return self.reply({'error':'Configure RADAR_FEED_TOKEN and send Bearer token'},401)
                event=str(p.get('event_id',''))
                if not event: raise ValueError('event_id required')
                obj=product(p['product'],event_id=event)
                WAKE.set()
                return self.reply(obj,202 if obj.get('status')!='duplicate' else 200)
            if self.path=='/api/availability':
                status=p.get('status')
                if status not in ('sold_out','in_stock','partial','unknown'): raise ValueError('Invalid availability status')
                checked_at=p.get('checked_at')
                if not checked_at or date(checked_at)>now(): raise ValueError('Valid checked_at required')
                evidence=safe_url(p.get('evidence_url',''))
                if not evidence or not p.get('source'): raise ValueError('Evidence URL and source required')
                obj=patch_product(p['product_id'],{'availability':{'status':status,'checked_at':checked_at,'source':str(p['source'])[:200],'evidence_url':evidence,'reason':str(p.get('reason','Manual verification'))[:1000]}})
                return self.reply(obj)
            if self.path=='/api/validate':
                enqueue(p['product_id']); return self.reply({'status':'pending'},202)
            if self.path=='/api/search':
                ps=state()['products']; item=next((x for x in ps if x['id']==p['product_id']),None)
                if not item: raise ValueError('Unknown product')
                return self.reply(search(item))
            if self.path=='/api/discover':
                candidates=state()['products'][:10]; results=[dict(product_id=x['id'],result=search(x)) for x in candidates]
                return self.reply(dict(status='seed_discovery',results=results))
            if self.path=='/api/demo':
                p=product(dict(name='Turquoise two-tone matching set',niche='Fashion',competitor='Example boutique',keywords='turquoise two tone top matching pants co ord',demo=True))
                pub=(now()-timedelta(days=6)).isoformat()
                for hours,likes in ((24,62000),(0,91000)):
                    video(p['id'],dict(url='https://www.tiktok.com/search?q=turquoise%20two%20tone%20set',published_at=pub,observed_at=(now()-timedelta(hours=hours)).isoformat(),likes=likes,views=780000,shares=4200,comments_count=1800,comments=['Where can I buy this?','Link please','I need this','Love the colours','Too expensive'],source='synthetic example — search link, not a verified video',demo=True))
                return self.reply(p,201)
            self.reply({'error':'Not found'},404)
        except (ValueError,TypeError,KeyError) as e: self.reply({'error':str(e)},400)
        except Exception as e:
            print(type(e).__name__,str(e)); self.reply({'error':'Provider or storage failure; check server log'},502)

if __name__=='__main__':
    init(); port=int(os.environ.get('PORT','8765')); print('Product Radar: http://127.0.0.1:'+str(port),flush=True)
    threading.Thread(target=worker,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
