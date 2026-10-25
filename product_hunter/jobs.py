"""Persistent opt-in research queue. No paid calls until explicitly enabled."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from . import providers, telegram

STATE = Path('research_jobs.json')


def configured():
    return os.getenv('RESEARCH_ENABLED') == 'true' and all(os.getenv(k) for k in (
        'OPENAI_API_KEY', 'OPENAI_RESEARCH_MODEL', 'APIFY_TOKEN',
        'APIFY_MAX_RUN_USD', 'RESEARCH_DAILY_LIMIT', 'RESEARCH_MONTHLY_LIMIT'))


def _load():
    return json.loads(STATE.read_text()) if STATE.exists() else {'jobs': {}, 'usage': {}}


def _save(data):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False))
    temp.replace(STATE)


def enqueue(store, product):
    if not configured():
        return
    data = _load()
    key = hashlib.sha256((store + '/' + str(product.get('id'))).encode()).hexdigest()
    if key in data['jobs']:
        return
    if len(data['jobs']) >= 500:
        print('[research] Queue full; research skipped')
        return
    # Deliberately store only the product fields required for identification.
    data['jobs'][key] = {'store': store, 'product': {k: product[k] for k in (
        'id', 'title', 'body_html', 'images', 'handle') if k in product},
        'stage': 'pending', 'created_at': datetime.now(timezone.utc).isoformat()}
    _save(data)


def report(job):
    identity = job['identity']
    videos = job['videos']
    matches = [v for v in videos if v.get('product_match_verified')]
    candidates = [v for v in matches if v.get('viral_candidate')]
    status = ('Viraaliehdokas: kynnys 100 000 katselua + 3 % tykkäyksiä, enintään 30 pv vanha video'
              if candidates else 'Viraaliutta ei vahvistettu tässä enintään 5 videon otoksessa')
    text = ('🔎 TUOTETUTKIMUS\n' + identity['product_name'] + '\n' + identity['summary_fi'] +
            '\n' + job['store'].rstrip('/') + '/products/' + job['product'].get('handle', '') +
            '\n\nTikTok: ' + status + '\nTuoteosuma arvioidaan kansikuvasta; koko videota ei ole tarkastettu.')
    for video in sorted(videos, key=lambda v: v['views'], reverse=True)[:3]:
        label = 'AI-kansikuvaosuma' if video['product_match_verified'] else 'Tuoteosuma epävarma'
        text += (f"\n\n{label}: {video['views']:,} katselua, {video['likes']:,} tykkäystä, "
                 f"{video['like_rate']:.1%}, ikä {video['age_days']} pv\n{video['url']}"
                 f"\nHaettu: {video['fetched_at'][:19]} UTC")
    return text


def tick():
    if not configured():
        return
    try:
        data = _load()
        limit = int(os.environ['RESEARCH_DAILY_LIMIT'])
        if not 1 <= limit <= 10:
            raise ValueError('Daily research limit must be 1..10')
        month_limit = int(os.environ['RESEARCH_MONTHLY_LIMIT'])
        if not 1 <= month_limit <= 60:
            raise ValueError('Monthly research limit must be 1..60')
        cap = float(os.environ['APIFY_MAX_RUN_USD'])
        if not 0.5 <= cap <= 1:
            raise ValueError('Invalid provider spend cap')
        day = datetime.now(timezone.utc).date().isoformat()
        month_used = sum(count for date, count in data['usage'].items() if date.startswith(day[:7] + '-'))
        for key, job in data['jobs'].items():
            if job['stage'] in ('done', 'failed', 'ambiguous'):
                continue
            if job['stage'] == 'pending':
                if data['usage'].get(day, 0) >= limit or month_used >= month_limit:
                    continue
                data['usage'][day] = data['usage'].get(day, 0) + 1
                # Persist before external calls; never repeat an ambiguous paid request.
                job['stage'] = 'ambiguous'
                _save(data)
                job['identity'] = providers.identify(job['product'])
                job['run_id'] = providers.start_search(job['identity']['search_query'])
                job['stage'] = 'searching'
            elif job['stage'] == 'searching':
                rows = providers.poll_search(job['run_id'])
                if rows is None:
                    continue
                job['stage'] = 'ambiguous'
                _save(data)
                job['videos'] = [v for row in rows if (v := providers.score_video(job['product'], row))]
                job['stage'] = 'ready'
            elif job['stage'] == 'ready':
                telegram.validate_destination()
                # At most one notification attempt, including ambiguous timeout/crash.
                job['stage'] = 'ambiguous'
                _save(data)
                telegram.send_message(report(job))
                job['stage'] = 'done'
            _save(data)
            break  # One bounded job step per five-minute monitor round.
    except Exception:
        print('[research] Step deferred or failed; sensitive details omitted')
