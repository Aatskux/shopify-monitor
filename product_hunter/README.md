# AI Product Hunter — isolated first phase

This branch does **not** change the existing monitor or Discord notifications.

- `telegram.py`: Telegram Bot API notification transport.
- `research.py`: product identification, search query generation, and evidence-based video scoring.
- TikTok search and ad metrics are **not connected** yet. Do not present estimates as live data.

## Configuration

Create a Telegram bot with @BotFather. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Railway environment variables or GitHub Actions secrets. Never commit credentials.

## Next integration steps

1. Add a tested Telegram adapter to the new-product event in `shopify_monitor.py`.
2. Connect an authorized TikTok video-data provider and verify freshness, coverage and cost.
3. Fetch video thumbnails/frames for visual product matching; distinguish same product from same category.
4. Save timestamped ad snapshots to persistent storage and trigger scaling alerts only with supporting evidence.
5. Run tests and stage rollout before disabling Discord.

The repository currently has a GitHub Actions schedule in addition to the user's Railway deployment. Confirm which one is active to avoid duplicate notifications.
