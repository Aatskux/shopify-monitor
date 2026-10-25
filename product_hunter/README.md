# AI Product Hunter — optional Telegram notifications

New products detected after a store's baseline are also sent to Telegram when
both Telegram variables are configured. Discord behavior remains unchanged.
Baseline collection, already-seen products and dry runs send no Telegram messages.

- `telegram.py`: Telegram Bot API notification transport.
- `research.py`: product identification, search query generation, and evidence-based video scoring.
- TikTok search and ad metrics are **not connected** yet. Do not present estimates as live data.

## Configuration

Create a Telegram bot with @BotFather. Never commit credentials.

- Railway: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` on the service that
  actually runs `python -u shopify_monitor.py` (continuous mode), or
  `python -u shopify_monitor.py --once` for a scheduled job.
- GitHub Actions secrets are separate from Railway variables. The current
  `monitor.yml` passes only Discord variables; Telegram is deliberately not
  enabled there by this change. Enabling it later requires both GitHub secrets
  and explicit environment mappings on the monitor step.
- The repository contains no Railway deployment manifest. Verify the Railway
  service's source branch, start command, schedule and persistent state storage
  in its dashboard before deployment. This PR does not change those settings.
- Keep one active owner of Telegram delivery. Two independent monitors can send
  duplicates because their seen state is separate. Railway needs persistent
  storage for the existing state files across restarts.

Delivery is best-effort with a 15-second timeout per new product. Failures log
only a fixed message, never exception details or credentials. Telegram failure
does not stop Discord or state persistence. There is no Telegram retry queue:
failed notifications are not replayed after the product is marked seen, and an
interrupted round before state is saved can replay notifications on restart.
For a busy catalog the timeout budget can accumulate; stage with a small store
list before enabling a full rollout. Disable Telegram by removing either variable.

## Next integration steps

1. Review the integration PR, run CI, and stage the deployment before merging.
2. Connect an authorized TikTok video-data provider and verify freshness, coverage and cost.
3. Fetch video thumbnails/frames for visual product matching; distinguish same product from same category.
4. Save timestamped ad snapshots to persistent storage and trigger scaling alerts only with supporting evidence.
5. Run tests and stage rollout before disabling Discord.

The repository currently has a GitHub Actions schedule in addition to the user's Railway deployment. Confirm which one is active to avoid duplicate notifications.


## Photo delivery and private group

Product images now use Telegram sendPhoto with the product link in the caption.
Messages set protect_content=true to restrict forwarding/saving. This is not
end-to-end encryption or an absolute guarantee against copying.
Only TELEGRAM_CHAT_ID receives messages; the monitor does not broadcast to bot users.
Set TELEGRAM_PRIVATE_GROUP_ONLY=true for a private group of exactly three members
(two people and this bot). Public usernames or another member count block all
new delivery. Keep membership and invite links under your control; the bot cannot
prevent a group admin from adding somebody or taking a photo of the screen.
If this guard fails, notifications are skipped without stopping Discord.

## Research integration — prepared, disabled until budget approval

The queue only activates when RESEARCH_ENABLED=true and all of these exist:
OPENAI_API_KEY, OPENAI_RESEARCH_MODEL (an image-capable Responses model supporting
JSON mode), APIFY_TOKEN, APIFY_MAX_RUN_USD (positive and at most 1 USD), and
RESEARCH_DAILY_LIMIT (1..10 new jobs/day). Do not commit key values. Set secrets
in Railway, separately from GitHub Actions. No paid smoke test has been performed.
Verify model access, response shape, actor pricing and sample accuracy before enabling.

The first message includes a product photo immediately. A separate research
follow-up arrives after the persistent worker identifies the product from image
and description, starts a bounded Clockworks TikTok search, polls it on a later
round, then compares product/cover images. One step runs after each completed
monitor round so Discord and monitor state persist before research. Up to five
search results are considered. Each job uses at most six OpenAI calls (one
identification plus five cover comparisons), each bounded to 600 output tokens;
input images use low detail. Provider-side Apify per-run charge cap is supplied.
Daily job counts limit usage, not total dollar spending across providers; configure
provider billing controls too. Do not describe these controls as a guaranteed
combined monthly dollar cap.

Reports include video links, retrieved timestamps, views, likes, like rate,
video age and an explicit AI-cover-match label. Only videos up to 30 days old
with an AI cover-match confidence >=0.9 can become a viral candidate at >=100k
views and >=3% likes/views. These are configurable product criteria in code, not
proof of virality, growth, sales, ad spend or full-video verification. Missing,
stale, invalid or visually uncertain evidence never becomes a positive verdict.
No results means the five-video sample did not establish virality, not that the
product has no TikTok presence. The pipeline does not yet measure growth over time.

research_jobs.json lives alongside seen.json in /data. Queued data contains product
metadata and research output, never credentials. Daily admission is persisted before
paid calls. Ambiguous paid failures and send attempts are not automatically replayed;
manual review is needed to avoid duplicate spending/messages. Queue cap is 500 jobs.
Disable RESEARCH_ENABLED to stop paid research without affecting notifications.

API sources:
- https://core.telegram.org/bots/api#sendphoto
- https://developers.openai.com/api/docs/guides/images-vision
- https://docs.apify.com/api/v2/actors-runs-post
- https://apify.com/clockworks/tiktok-scraper/input-schema
- https://apify.com/clockworks/tiktok-scraper/output-schema
