import os,unittest
from datetime import datetime,timezone
from unittest.mock import patch
from product_hunter import tiktok,market
NOW=datetime(2026,10,26,tzinfo=timezone.utc)
def row(**extra):
 return dict({'webVideoUrl':'https://www.tiktok.com/@creator/video/123','playCount':200000,'diggCount':10000,'commentCount':100,'shareCount':300,'createTime':NOW.timestamp()-86400,'authorMeta':{'name':'creator'},'videoMeta':{'coverUrl':'https://example.com/cover.jpg'}},**extra)
class Tests(unittest.TestCase):
 def test_metrics_and_freshness(self):
  self.assertEqual(tiktok.normalize(row(),NOW)['like_rate'],5)
  for data in (row(createTime=NOW.timestamp()-86400*31),row(playCount='100000'),row(diggCount=999999),row(webVideoUrl='https://evil.example/video/123'),row(createTime=NOW.timestamp()+1)):
   self.assertIsNone(tiktok.normalize(data,NOW))
  self.assertIsNone(tiktok.normalize(row(shareCount='300'),NOW)['shares'])
 def test_exact_match_required(self):
  v=tiktok.normalize(row(),NOW);e={'status':'ok','videos':[v]}
  self.assertIn('ei varmistettua',tiktok.summary(e));self.assertIn('vastaavuus varmistamatta',tiktok.summary(e))
  v['match']='similar';self.assertIn('Vain samankaltainen',tiktok.summary(e));self.assertIn('ei varmistettua',tiktok.summary(e))
  v['match']='exact';self.assertIn('yksittäinen suosittu',tiktok.summary(e));self.assertIn('US/UK-jakauma eivät ole tiedossa',tiktok.summary(e))
 @patch.dict(os.environ,{'APIFY_TOKEN':'fake','APIFY_MAX_RUN_USD':'0.5'},clear=True)
 @patch.object(tiktok,'visual_json')
 @patch.object(tiktok,'search')
 def test_batch_match_dedup(self,search,vision):
  search.return_value=[row(createTime=datetime.now(timezone.utc).timestamp()-86400)]*2
  vision.side_effect=[{'queries':['coraline cat plush','coraline doll cat toy']},{'matches':[{'index':0,'match':'exact','confidence':0.95}]}]
  e=tiktok.collect('Coraline plush',b'jpeg');self.assertEqual(e['status'],'ok');self.assertEqual(len(e['videos']),1);self.assertEqual(e['videos'][0]['match'],'exact');search.assert_called_once()
 @patch.dict(os.environ,{'APIFY_TOKEN':'fake'},clear=True)
 @patch.object(tiktok,'visual_json')
 @patch.object(tiktok,'search')
 def test_visual_failure_cannot_verify(self,search,vision):
  search.return_value=[row(createTime=datetime.now(timezone.utc).timestamp()-86400)]
  vision.side_effect=[{'queries':['specific toy','specific plush']},ValueError('image unavailable')]
  e=tiktok.collect('toy',b'jpeg');self.assertEqual(e['videos'][0]['match'],'unverified');self.assertIn('ei varmistettua',tiktok.summary(e))
 @patch.dict(os.environ,{'APIFY_TOKEN':'fake','APIFY_MAX_RUN_USD':'0.5'},clear=True)
 @patch.object(tiktok,'apify')
 def test_provider_charge_and_search_bounds(self,api):
  api.side_effect=[{'data':{'id':'run123'}},{'data':{'status':'SUCCEEDED','defaultDatasetId':'data123'}},[]]
  self.assertEqual(tiktok.search(['one','two']),[])
  p=api.call_args_list[0].kwargs;self.assertEqual(p['params']['maxTotalChargeUsd'],0.5);self.assertEqual(p['params']['maxItems'],10);self.assertEqual(p['json']['videoSearchDateFilter'],'PAST_MONTH');self.assertFalse(p['json']['shouldDownloadVideos'])
 def test_no_results_does_not_prove_no_demand(self):
  self.assertIn('ei todista',tiktok.summary({'status':'ok','videos':[]}))
 def test_plain_compact_cited_card(self):
  r={'status':'completed','output':[{'content':[{'type':'output_text','text':'**UK** X','annotations':[{'type':'url_citation','url':'https://example.com','start_index':7,'end_index':8}]}]}]}
  text=market.render(r);self.assertNotIn('**',text);self.assertIn('https://example.com',text)
  r['output'][0]['content'][0]['text']='x'*1600
  with self.assertRaises(ValueError):market.render(r)
if __name__=='__main__':unittest.main()
