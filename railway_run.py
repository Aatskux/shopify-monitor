"""Run one monitor round with persistent state in the mounted Railway volume."""
import os, shutil, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('MONITOR_DATA_DIR','/data'))
if not DATA.is_dir() or not os.path.ismount(DATA):
    raise RuntimeError('Mount a Railway Volume at /data before running the monitor')
if not (DATA/'stores.json').exists():
    shutil.copyfile(ROOT/'stores.json',DATA/'stores.json')
os.chdir(DATA)
sys.path.insert(0,str(ROOT))
sys.argv=[str(ROOT/'shopify_monitor.py'),'--once']
from product_hunter import catalog_sync, tracked_catalog
removed = tracked_catalog.remove_excluded(DATA/'stores.json')
if removed:
    print('[catalog] removed pilocea.com from persistent store list', flush=True)
import shopify_monitor
# Intake must update persistent stores before the monitor loads its catalogue.
token=os.environ.get('DISCORD_BOT_TOKEN','').strip()
channel=os.environ.get('AI_STORES_CHANNEL_ID','').strip()
if token and channel:
    import discord_intake
    try:
        discord_intake.run(token,channel)
    except Exception as error:
        print('Discord intake failed: '+discord_intake.describe_error(error),flush=True)
else:
    print('Discord intake disabled: configure DISCORD_BOT_TOKEN and AI_STORES_CHANNEL_ID',flush=True)
if not os.environ.get('DISCORD_WEBHOOK') and not os.environ.get('SALES_WEBHOOK'):
    shopify_monitor.get_webhook=lambda:None
    print('Discord delivery disabled; monitor state and configured Signal feed remain active',flush=True)
def sync_catalogue():
    try:
        if catalog_sync.publish(DATA):
            print('[catalog] published tracked-store catalogue', flush=True)
    except Exception as error:
        print(f'[catalog] sync failed: {type(error).__name__}', flush=True)

if removed and (DATA/'image_hashes.json').exists():
    sync_catalogue()
shopify_monitor.main()
sync_catalogue()
