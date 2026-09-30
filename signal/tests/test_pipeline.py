import unittest, tempfile, threading, json, urllib.request, os
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as s
class PipelineTests(unittest.TestCase):
 def test_feed_to_validation_with_provider_fixture(self):
  with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'RADAR_FEED_TOKEN':'test-token'}):
   s.DB=Path(tmp)/'db';s.init();http=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
   base='http://127.0.0.1:'+str(http.server_port)
   try:
    body={'event_id':'launch-1','product':{'name':'Turquoise top','url':'https://shop.example/products/top'}}
    def post(token):
     req=urllib.request.Request(base+'/api/feed',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
     with urllib.request.urlopen(req) as response:return response.status,json.load(response)
    with self.assertRaises(urllib.error.HTTPError):post('invalid')
    status,p=post('test-token');self.assertEqual(status,202);self.assertEqual(post('test-token')[1]['status'],'duplicate')
    def fixture(product):
     video={'url':'https://www.tiktok.com/@fixture/video/12345','published_at':(s.now()-timedelta(days=3)).isoformat(),'likes':62000,'views':780000,'comments':['haluun tän']*40,'match':'confirmed','source':'test fixture'}
     video['observed_at']=(s.now()-timedelta(hours=24)).isoformat();s.video(product['id'],video)
     video.update(likes=91000,observed_at=s.stamp());s.video(product['id'],video)
     return {'status':'imported','count':1,'errors':[]}
    with patch('stock.check_stock',return_value={'status':'sold_out','checked_at':s.stamp()}),patch.object(s,'search',side_effect=fixture):self.assertTrue(s.run_one_job())
    with urllib.request.urlopen(base+'/api/state') as response:state=json.load(response)
    p=state['products'][0];self.assertEqual(p['validation_status'],'complete');self.assertEqual(p['opportunity']['components']['availability'],5);self.assertGreater(p['score'],50)
   finally:http.shutdown();http.server_close();thread.join()
if __name__=='__main__':unittest.main()
