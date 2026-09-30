import unittest, tempfile, sys, os, json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as s
import stock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integrations'))
import signal_bridge as bridge
class ValidationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();s.DB=Path(self.tmp.name)/'db';s.init()
 def tearDown(self):self.tmp.cleanup()
 def p(self):return s.product({'name':'Turquoise top','url':'https://shop.example/products/set'})
 def v(self,p,match='confirmed',comments=None):
  comments=comments if comments is not None else ['haluun tän','mistä mä voin ostaa tän','I want this','Love it']*10
  payload=dict(url='https://www.tiktok.com/@test/video/123456',published_at=(s.now()-timedelta(days=3)).isoformat(),likes=62000,observed_at=(s.now()-timedelta(hours=24)).isoformat(),views=780000,comments=comments,match=match)
  s.video(p['id'],payload);payload.update(likes=91000,observed_at=s.stamp());return s.video(p['id'],payload)
 def test_feed_transaction_and_dedup(self):
  p=s.product({'name':'Top','url':'https://shop.example/products/top'},event_id='a')
  self.assertEqual(s.product({'name':'Top'},event_id='a')['status'],'duplicate')
  second=s.product({'name':'Top','url':'https://shop.example/products/top?ref=test'},event_id='b')
  self.assertEqual(second['id'],p['id']);self.assertEqual(len(s.state()['products']),1)
  with s.conn() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
 def test_automatic_validation(self):
  p=self.p()
  with patch('stock.check_stock',return_value={'status':'sold_out','checked_at':s.stamp()}),patch.object(s,'search',return_value={'status':'manual_search_required'}):
   self.assertTrue(s.run_one_job())
  p=s.state()['products'][0];self.assertEqual(p['validation_status'],'needs_provider');self.assertEqual(p['availability']['status'],'sold_out');self.assertEqual(p['score'],0)
 def test_retry_after_provider_failure(self):
  p=self.p()
  with patch('stock.check_stock',return_value={'status':'unknown'}),patch.object(s,'search',side_effect=RuntimeError('private detail')):s.run_one_job()
  self.assertEqual(s.state()['products'][0]['validation_status'],'failed')
  with s.conn() as c:
   row=c.execute('SELECT * FROM jobs').fetchone();self.assertEqual(row['status'],'failed');self.assertGreater(s.date(row['next_at']),s.now())
 def test_sold_out_corroboration(self):
  p=self.p();self.v(p);s.patch_product(p['id'],{'availability':{'status':'sold_out','checked_at':s.stamp()}})
  p=s.state()['products'][0];self.assertEqual(p['opportunity']['components']['availability'],5);self.assertIsNone(p['opportunity']['probability'])
 def test_unverified_and_demo_excluded(self):
  p=self.p();self.v(p,match='unverified');self.assertEqual(s.state()['products'][0]['score'],0)
  with s.conn() as c:
   row=c.execute('SELECT id,data FROM videos').fetchone();v=json.loads(row['data']);v['match']='confirmed';v['demo']=True;c.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(v),row['id']))
  self.assertEqual(s.state()['products'][0]['score'],0)
 def test_negation_and_small_sample(self):
  p=self.p();v=self.v(p,comments=["I don't want this",'haluun tän']);a=s.analysis(v)
  self.assertEqual(a['comment_analysis']['negative'],1);self.assertEqual(a['comment_analysis']['intent'],1)
  p=s.state()['products'][0];self.assertLess(p['opportunity']['components']['intent'],1)
 def test_stock_variants(self):
  self.assertEqual(stock.summarize({'variants':[{'available':False},{'available':False}]},'https://example.com')['status'],'sold_out')
  self.assertEqual(stock.summarize({'variants':[{'available':False},{'available':True}]},'https://example.com')['status'],'partial')
  self.assertEqual(stock.summarize({'variants':[{'available':'false'}]},'https://example.com')['status'],'unknown')
 def test_stock_private_denied(self):
  with self.assertRaises(ValueError):stock.public_target('http://127.0.0.1/products/a.js')
  with patch('stock.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
   with self.assertRaises(ValueError):stock.public_target('https://shop.example/products/a.js')
 def test_outbox_persist_and_replay(self):
  bridge.OUTBOX=Path(self.tmp.name)/'outbox.json';p={'id':1,'title':'Top','handle':'top','images':[{'src':'https://example.com/top.jpg'}]}
  bridge.enqueue_launch('https://shop.example',p);bridge.enqueue_launch('https://shop.example',p)
  self.assertEqual(len(bridge._read()),1)
 def test_search_url_rejected(self):
  p=self.p()
  with self.assertRaises(ValueError):s.video(p['id'],dict(url='https://www.tiktok.com/search?q=top',published_at=s.stamp()))
if __name__=='__main__':unittest.main()
