import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from product_hunter import providers, jobs

ENV = {'RESEARCH_ENABLED': 'true', 'OPENAI_API_KEY': 'fake', 'OPENAI_RESEARCH_MODEL': 'test-model',
       'APIFY_TOKEN': 'fake', 'APIFY_MAX_RUN_USD': '0.05', 'RESEARCH_DAILY_LIMIT': '1'}
PRODUCT = {'id': 1, 'title': 'Lamp', 'handle': 'lamp', 'images': [{'src': 'https://img.test/p.jpg'}]}
IDENTITY = {'product_name': 'Lamp', 'summary_fi': 'Valaisin', 'search_query': 'lamp'}


class ProviderTests(unittest.TestCase):
    @patch.dict(os.environ, ENV, clear=True)
    @patch.object(providers.requests, 'post')
    def test_ai_request_has_images_bounded_output_no_storage(self, post):
        post.return_value.json.return_value = {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(IDENTITY)}]}]}
        self.assertEqual(providers.identify(PRODUCT), IDENTITY)
        payload = post.call_args.kwargs['json']
        self.assertFalse(payload['store'])
        self.assertEqual(payload['max_output_tokens'], 600)
        self.assertEqual(payload['input'][1]['content'][1]['type'], 'input_image')
        self.assertNotIn('tools', payload)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    @patch.dict(os.environ, ENV, clear=True)
    @patch.object(providers, 'apify', return_value={'data': {'id': 'run123'}})
    def test_apify_cost_and_result_limits(self, api):
        self.assertEqual(providers.start_search('lamp'), 'run123')
        self.assertEqual(api.call_args.kwargs['params']['maxTotalChargeUsd'], .05)
        self.assertEqual(api.call_args.kwargs['params']['maxItems'], 5)
        self.assertEqual(api.call_args.kwargs['json']['searchQueries'], ['lamp'])

    @patch.object(providers, 'ai_json')
    def test_metrics_and_visual_match_and_freshness(self, ai):
        now = datetime.now(timezone.utc)
        row = {'webVideoUrl': 'https://www.tiktok.com/@a/video/1', 'playCount': 200000,
               'diggCount': 10000, 'createTime': now.timestamp()-86400,
               'videoMeta': {'coverUrl': 'https://img.test/cover.jpg'}}
        ai.return_value = {'same_product': True, 'confidence': .95, 'reason': 'same distinctive lamp'}
        result = providers.score_video(PRODUCT, row, now)
        self.assertTrue(result['viral_candidate'])
        ai.return_value = {'same_product': True, 'confidence': .5}
        self.assertFalse(providers.score_video(PRODUCT, row, now)['viral_candidate'])
        self.assertIsNone(providers.score_video(PRODUCT, {**row, 'createTime': now.timestamp()-31*86400}, now))
        self.assertIsNone(providers.score_video(PRODUCT, {**row, 'diggCount': True}, now))
        self.assertIsNone(providers.score_video(PRODUCT, {**row, 'webVideoUrl': 'https://tiktok.com.evil.test/v'}, now))
        no_image = {**row, 'videoMeta': {}}
        self.assertFalse(providers.score_video(PRODUCT, no_image, now)['product_match_verified'])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.state = patch.object(jobs, 'STATE', Path(self.tmp.name)/'jobs.json'); self.state.start(); self.addCleanup(self.state.stop)

    @patch.object(providers, 'identify')
    def test_disabled_means_no_queue_and_no_paid_call(self, identify):
        os.environ['RESEARCH_ENABLED'] = 'false'
        jobs.enqueue('https://shop', PRODUCT); jobs.tick()
        self.assertFalse(jobs.STATE.exists()); identify.assert_not_called()

    @patch.object(providers, 'identify', return_value=IDENTITY)
    @patch.object(providers, 'start_search', return_value='run123')
    @patch.object(providers, 'poll_search', return_value=[])
    @patch.object(jobs.telegram, 'validate_destination')
    @patch.object(jobs.telegram, 'send_message')
    def test_full_lifecycle_dedupe_daily_cap_and_honest_empty_result(self, send, guard, poll, start, identify):
        jobs.enqueue('https://shop', PRODUCT); jobs.enqueue('https://shop', PRODUCT)
        jobs.enqueue('https://shop', {**PRODUCT, 'id': 2})
        jobs.tick(); jobs.tick(); jobs.tick(); jobs.tick()
        identify.assert_called_once(); start.assert_called_once(); send.assert_called_once()
        self.assertIn('Viraaliutta ei vahvistettu', send.call_args.args[0])
        stages = [j['stage'] for j in jobs._load()['jobs'].values()]
        self.assertIn('done', stages); self.assertIn('pending', stages)

    @patch.object(providers, 'identify', return_value=IDENTITY)
    @patch.object(providers, 'start_search', side_effect=TimeoutError('secret'))
    def test_ambiguous_paid_call_is_never_automatically_repeated(self, start, identify):
        jobs.enqueue('https://shop', PRODUCT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jobs.tick(); jobs.tick()
        start.assert_called_once()
        self.assertNotIn('secret', out.getvalue())
        self.assertEqual(next(iter(jobs._load()['jobs'].values()))['stage'], 'ambiguous')

    @patch.object(providers, 'identify')
    def test_invalid_budget_fails_before_any_provider_call(self, identify):
        os.environ['RESEARCH_DAILY_LIMIT'] = '1000'
        jobs.enqueue('https://shop', PRODUCT)
        with contextlib.redirect_stdout(io.StringIO()): jobs.tick()
        identify.assert_not_called()
