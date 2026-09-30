"""Import a bounded recent snapshot of the monitor catalogue. No launch claims inferred."""
import json, argparse, urllib.parse
from datetime import timedelta
import server as s

def run(path,limit=20):
    snapshot=json.load(open(path));rows=[]
    for store,items in snapshot.get('products',{}).items():
        for pid,p in items.items():
            try:published=s.date(p['published_at'])
            except (KeyError,ValueError):continue
            if s.now()-timedelta(days=14)<=published<=s.now() and p.get('handle') and p.get('name'):
                if __import__('re').search(r'protection|insurance|gift ?card|donation|membership',p['name'],__import__('re').I):continue
                rows.append((published,store,pid,p))
    rows.sort(key=lambda r:r[0],reverse=True);s.init();added=0
    for published,store,pid,p in rows[:limit]:
        result=s.product({'name':p['name'],'url':store.rstrip('/')+'/products/'+urllib.parse.quote(p['handle'],safe=''),'image':p.get('image',''),'competitor':urllib.parse.urlsplit(store).hostname,'launch_at':published.isoformat(),'source':'shopify-monitor stored catalogue snapshot'},event_id='snapshot:'+store+':'+str(pid))
        if result.get('status') not in ('existing','duplicate'):added+=1
    return added
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('path');parser.add_argument('--limit',type=int,default=20);args=parser.parse_args();print('Imported recent products:',run(args.path,args.limit))
