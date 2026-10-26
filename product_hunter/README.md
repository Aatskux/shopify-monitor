# Product notifications and manual research

The monitor sends a protected photo or text to the configured private Telegram group.
There is no automatic research admission, queue tick, or research status in product alerts.
Legacy jobs.py and the existing /data/research_jobs.json are dormant; do not enable them.
Keep RESEARCH_ENABLED=false on the monitor.

Set MANUAL_RESEARCH_ENABLED=true on monitor and worker only after the worker is ready.
Each new alert has a signed “Tutki tuote: US vai UK?” button. The signature binds the
entire original message and destination to the bot credential. No extra signing key.
The worker accepts only original bot messages in the private three-member group and
verifies the clicking user is a current group member. Recheck privacy before every send.
A busy worker rejects additional requests; it never queues them. Repeated product URLs
are durably deduplicated, including failed or interrupted attempts (no automatic retry).
Older alerts cannot acquire buttons retroactively without their original message IDs.

Deploy research_worker.py as a separate single-replica always-on service using
Dockerfile.research and railway.research.json, with its own persistent /data volume.
No cron, public domain or webhook. Only one getUpdates consumer may use the bot token.
Required variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PRIVATE_GROUP_ONLY=true,
OPENAI_API_KEY, OPENAI_RESEARCH_MODEL and MANUAL_RESEARCH_ENABLED=true.
Do not give the worker Discord credentials. The new reports use capped TikTok search and web search,
not the old automatic queue. Preserve the OpenAI project's existing $5 hard monthly limit;
worker additionally admits at most 10 attempts/calendar month, max 6 search calls and
2400 generated tokens per attempt. These count limits are not a monetary billing cap.
Railway always-on resource usage is billed separately; do not change paid plans.

Reports compare US and UK, competition/prices, audiences, positioning, evidence gaps,
conditional economics and test plans. Claims cite clickable public web sources.
Search visibility is not sales evidence; TikTok virality is not inferred. Missing product
page or source access must be disclosed. A report without citations is rejected.
Raw provider errors/URLs/credentials are never logged. store=false on OpenAI requests.

Rollback: set MANUAL_RESEARCH_ENABLED=false on the monitor and worker, stop the worker.
Keep RESEARCH_ENABLED=false. Existing product detection, Discord and Signal stay unchanged.

## Evidence card v2

Replaces the long report with one message (max 3700 UTF-16 units). One capped Apify run
uses two image-derived queries, up to ten Top-search results; dates are validated locally against a 30-day window.
Clockworks video-only filters currently fail with device-ID errors, so no date/sort filter is sent.
Invalid/stale metrics are rejected. Up to five video covers are compared with the product
image in one batch; only confident exact cover matches support a strong-video signal.
This is not full-video inspection, measured growth or buyer-country evidence. Similar
products are labelled separately. Observed views, like rate, date, shares and video URLs
are rendered deterministically. The market section has five concise lines with source links.

The v2 admission namespace allows one upgrade of an older report. Admission is conservatively
capped at TEN total attempts per calendar month, including older attempts in the same database.
Each attempt can start at most one $0.50-capped Apify run: at most $5 in new run ceilings/month.
Keep the existing $5 OpenAI project hard limit and the agreed 20 EUR combined API budget.
No paid plan, token scope, automatic retry or queue changes. The worker needs the existing
APIFY_TOKEN and APIFY_MAX_RUN_USD=0.5 in addition to its previous variables.
