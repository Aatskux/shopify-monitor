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
Do not give the worker Discord credentials or APIFY_TOKEN. The new reports use web search,
not the old Apify queue. Preserve the OpenAI project's existing $5 hard monthly limit;
worker additionally admits at most 60 attempts/calendar month, max 6 search calls and
6000 generated tokens per attempt. These count limits are not a monetary billing cap.
Railway always-on resource usage is billed separately; do not change paid plans.

Reports compare US and UK, competition/prices, audiences, positioning, evidence gaps,
conditional economics and test plans. Claims cite clickable public web sources.
Search visibility is not sales evidence; TikTok virality is not inferred. Missing product
page or source access must be disclosed. A report without citations is rejected.
Raw provider errors/URLs/credentials are never logged. store=false on OpenAI requests.

Rollback: set MANUAL_RESEARCH_ENABLED=false on the monitor and worker, stop the worker.
Keep RESEARCH_ENABLED=false. Existing product detection, Discord and Signal stay unchanged.
