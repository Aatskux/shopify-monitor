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

