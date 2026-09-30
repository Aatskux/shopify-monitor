import unittest, tempfile, json, os, pathlib
from unittest.mock import patch, MagicMock
import signal_bridge as b
class BridgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.original=b.OUTBOX;b.OUTBOX=pathlib.Path(self.tmp.name)/'queue.json'
  self.env=patch.dict(os.environ,{'SIGNAL_URL':'http://127.0.0.1:8765','SIGNAL_FEED_TOKEN':'test-only'});self.env.start()
  self.product={'id':1,'title':'Turquoise top','handle':'two-tone','images':[{'src':'https://example.com/top.jpg'}]}
 def tearDown(self):self.env.stop();b.OUTBOX=self.original;self.tmp.cleanup()
 def test_enqueue_is_durable_and_deduplicated(self):
  first=b.enqueue_launch('https://shop.example',self.product);b.enqueue_launch('https://shop.example',self.product)
  q=json.loads(b.OUTBOX.read_text());self.assertEqual(len(q),1);self.assertEqual(q[first]['product']['url'],'https://shop.example/products/two-tone');self.assertNotIn('test-only',b.OUTBOX.read_text())
 def test_failed_delivery_is_retained(self):
  b.enqueue_launch('https://shop.example',self.product)
  with patch('signal_bridge.urllib.request.build_opener') as create:
   create.return_value.open.side_effect=OSError('offline');self.assertEqual(b.flush(),0)
  self.assertEqual(len(b._read()),1)
 def test_acknowledged_duplicate_is_removed(self):
  b.enqueue_launch('https://shop.example',self.product)
  with patch('signal_bridge.urllib.request.build_opener') as create:
   r=MagicMock();r.status=200;r.read.return_value=b'{"status":"duplicate"}';create.return_value.open.return_value.__enter__.return_value=r
   self.assertEqual(b.flush(),1)
  self.assertEqual(b._read(),{})
 def test_invalid_ack_is_retained(self):
  b.enqueue_launch('https://shop.example',self.product)
  with patch('signal_bridge.urllib.request.build_opener') as create:
   r=MagicMock();r.status=200;r.read.return_value=b'{}';create.return_value.open.return_value.__enter__.return_value=r
   self.assertEqual(b.flush(),0)
  self.assertEqual(len(b._read()),1)
 def test_remote_http_denied(self):
  with patch.dict(os.environ,{'SIGNAL_URL':'http://remote.example'}):
   with self.assertRaises(ValueError):b.flush()
 def test_missing_config_no_queue_delivery(self):
  with patch.dict(os.environ,{'SIGNAL_FEED_TOKEN':''}):self.assertEqual(b.flush(),0)
if __name__=='__main__':unittest.main()
