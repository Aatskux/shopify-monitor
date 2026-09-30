import sys, tempfile, unittest
from pathlib import Path
from datetime import timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as s
class RadarTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); s.DB=Path(self.tmp.name)/'db';s.init();self.p=s.product({'name':'Turquoise set'})
 def tearDown(self): self.tmp.cleanup()
 def observation(self,likes,when):
  return dict(url='https://www.tiktok.com/@test/video/123',published_at=(s.now()-timedelta(days=6)).isoformat(),observed_at=when,likes=likes,views=780000,shares=4200,comments_count=1800,comments=['Link please','Beautiful','Scam'],source='test')
 def test_growth_and_dedup(self):
  s.video(self.p['id'],self.observation(62000,(s.now()-timedelta(hours=24)).isoformat()))
  v=s.video(self.p['id'],self.observation(91000,s.stamp())); a=s.analysis(v)
  self.assertAlmostEqual(a['growth_per_hour'],1208.3,places=1);self.assertEqual(len(s.state()['videos']),1);self.assertEqual(a['comment_analysis']['intent'],1);self.assertEqual(a['comment_analysis']['negative'],1)
 def test_missing_not_zero(self):
  v=s.video(self.p['id'],self.observation(None,s.stamp()));self.assertIsNone(s.analysis(v)['growth_per_hour']);self.assertEqual(s.analysis(v)['components']['velocity'],0)
 def test_out_of_order(self):
  s.video(self.p['id'],self.observation(10,s.stamp()))
  with self.assertRaises(ValueError):s.video(self.p['id'],self.observation(20,(s.now()-timedelta(hours=2)).isoformat()))
 def test_invalid_url(self):
  with self.assertRaises(ValueError):s.product({'name':'x','image':'javascript:alert(1)'})
 def test_future(self):
  v=self.observation(1,s.stamp());v['published_at']=(s.now()+timedelta(days=1)).isoformat()
  with self.assertRaises(ValueError):s.video(self.p['id'],v)
 def test_niche(self):
  self.assertEqual(s.classify("Turquoise top matching pants"),"Fashion")
  self.assertEqual(s.classify("unknown object"),"Unclassified")
 def test_manual_search(self):
  self.assertEqual(s.search(self.p)['status'],'manual_search_required')
if __name__=='__main__':unittest.main()
