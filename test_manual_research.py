import os, tempfile, unittest
from unittest.mock import patch
from product_hunter import telegram, manual, market
from research_worker import Worker
CONFIG={'TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'-123','MANUAL_RESEARCH_ENABLED':'true','TELEGRAM_PRIVATE_GROUP_ONLY':'true','OPENAI_API_KEY':'fake','OPENAI_RESEARCH_MODEL':'gpt-6-luna'}
TEXT='🆕 UUSI TUOTE\nTest\nKauppa: https://shop.example\nTuote: https://shop.example/products/test'
def query():
    return {'id':'q1','from':{'id':10},'data':manual.signature(TEXT),'message':{'chat':{'id':-123},'from':{'id':99},'caption':TEXT}}
@patch.dict(os.environ,CONFIG,clear=True)
class Tests(unittest.TestCase):
    def test_signatures(self):
        self.assertEqual(manual.verified_callback(query(),99)[0],'https://shop.example/products/test')
        for field in ('caption','chat','sender','data'):
            q=query()
            if field=='caption':q['message']['caption']+='x'
            if field=='chat':q['message']['chat']['id']=42
            if field=='sender':q['message']['from']['id']=12
            if field=='data':q['data']='forged'
            with self.assertRaises(ValueError):manual.verified_callback(q,99)
        self.assertLessEqual(len(manual.keyboard(TEXT)['inline_keyboard'][0][0]['callback_data']),64)
        self.assertIsNone(manual.keyboard(TEXT+'x'*1024))
    def test_persistence_and_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            state=manual.Admission(folder+'/db')
            self.assertEqual(state.claim('one',2),'started')
            state.acknowledge(42);state.db.close()
            state=manual.Admission(folder+'/db')
            self.assertEqual(state.offset(),42)
            self.assertEqual(state.claim('one',2),'duplicate')
            self.assertEqual(state.claim('two',2),'started')
            self.assertEqual(state.claim('three',2),'budget')
    @patch('research_worker.threading.Thread')
    @patch.object(telegram,'_call')
    def test_callback_ack_start_busy_duplicate_and_privacy(self,call,thread):
        call.side_effect=lambda method,payload: {'type':'group'} if method=='getChat' else 3 if method=='getChatMemberCount' else {'status':'member'}
        with tempfile.TemporaryDirectory() as folder:
            worker=Worker(manual.Admission(folder+'/db'),99)
            worker.handle(query())
            self.assertEqual(call.call_args.args[0],'answerCallbackQuery')
            thread.return_value.start.assert_called_once()
            worker.handle(query())  # busy, no queued work
            self.assertEqual(thread.call_count,1)
            worker.busy.release()
            worker.handle(query())  # durable duplicate
            self.assertEqual(thread.call_count,1)
            call.side_effect=lambda method,payload: {'type':'group'} if method=='getChat' else 4
            worker.handle(query())
            self.assertEqual(thread.call_count,1)
    @patch('research_worker.investigate',side_effect=RuntimeError('fake-secret'))
    @patch.object(telegram,'send_message')
    @patch.object(telegram,'validate_destination')
    def test_failure_no_retry(self,privacy,send,investigate):
        with tempfile.TemporaryDirectory() as folder:
            worker=Worker(manual.Admission(folder+'/db'),99);worker.state.claim('url');worker.busy.acquire()
            worker.run_report('url',TEXT)
            investigate.assert_called_once()
            self.assertEqual(worker.state.claim('url'),'duplicate')
            self.assertNotIn('fake-secret',str(send.call_args_list))
    @patch.object(market.requests,'post')
    def test_sources_and_caps(self,post):
        post.return_value.json.return_value={'status':'completed','output':[{'content':[{'type':'output_text','text':'UK X','annotations':[{'type':'url_citation','url':'https://example.com','start_index':3,'end_index':4}]}]}]}
        self.assertIn('https://example.com',market.investigate('url',TEXT))
        payload=post.call_args.kwargs['json'];self.assertEqual(payload['max_tool_calls'],6);self.assertEqual(payload['max_output_tokens'],6000);self.assertFalse(payload['store'])
        with self.assertRaises(ValueError):market.render({'status':'incomplete'})
        with self.assertRaises(ValueError):market.render({'status':'completed','output':[]})
    @patch('product_hunter.jobs.enqueue')
    @patch.object(telegram,'send_message')
    @patch.object(telegram,'validate_destination')
    def test_no_autoqueue(self,privacy,send,enqueue):
        telegram.notify_new_product('https://shop.example',{'title':'Test','handle':'test'})
        self.assertIn('reply_markup',send.call_args.kwargs)
        self.assertNotIn('jonossa',send.call_args.args[0]);enqueue.assert_not_called()
if __name__=='__main__':unittest.main()
