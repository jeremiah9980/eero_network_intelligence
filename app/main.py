import asyncio, json, time, yaml, argparse, os
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from .db import connect, upsert_device, add_event, add_alert, list_rows, events_per_hour, count_since
from .dsar_import import import_dsar
from .eero_api import EeroAdapter
from .notifiers import send, send_test, webhook_urls
from .security import classify, should_notify

load_dotenv()

CFG = {}
CON = None
CLIENTS = set()
SCHED = None
app = FastAPI(title='eero Presence Security Dashboard')

async def broadcast(msg):
    for ws in list(CLIENTS):
        try:
            await ws.send_json(msg)
        except Exception:
            CLIENTS.discard(ws)

def load_cfg(path):
    with open(path) as f:
        return yaml.safe_load(f)

def watched(cfg, d):
    m = (d.get('mac') or '').lower()
    n = d.get('name') or ''
    for w in cfg.get('devices', []):
        mm = [x.lower() for x in (w.get('match', {}).get('macs') or [])]
        nn = w.get('match', {}).get('names') or []
        if (m and m in mm) or n in nn:
            return w
    return None

async def handle_event(d, ev, first_poll=False):
    w = watched(CFG, d)
    severity, title = classify(ev, d, w, first_poll=first_poll)
    notify = should_notify(CFG, severity, w)
    add_event(CON, d, ev)
    if severity != 'info' or notify:
        add_alert(CON, d, ev, severity, title, notified=notify)
    if notify:
        # requests is blocking; keep the event loop responsive.
        await asyncio.to_thread(send, CFG, title, d, severity)
    await broadcast({'type': 'presence', 'event': ev, 'severity': severity, 'title': title, 'device': d})

async def poll():
    global CFG, CON
    try:
        devices = EeroAdapter(CFG).fetch_devices()
        source = 'api'
    except Exception as ex:
        devices = []
        source = f'api-error: {ex}'

    CON.execute(
        'INSERT INTO snapshots(ts,source,raw) VALUES(?,?,?)',
        (int(time.time()), source, json.dumps(devices))
    )
    CON.commit()

    row = CON.execute("SELECT v FROM state WHERE k='online'").fetchone()
    prev = json.loads(row['v']) if row else {}
    first_poll = not prev
    now = dict(prev)

    for d in devices:
        upsert_device(CON, d)
        mac = (d.get('mac') or '').lower()
        if not mac:
            continue
        online = bool(d.get('online', True))
        now[mac] = online

        if prev.get(mac) is not None and prev[mac] != online:
            await handle_event(d, 'entered' if online else 'left')
        elif mac not in prev:
            await handle_event(d, 'discovered', first_poll=first_poll)

    CON.execute(
        "INSERT INTO state(k,v) VALUES('online',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (json.dumps(now),)
    )
    CON.commit()

@app.get('/')
def home():
    return FileResponse('web/index.html')

@app.get('/api/health')
def health():
    return {'ok': True, 'devices': len(list_rows(CON, 'devices', 100000)) if CON else 0}

@app.get('/api/devices')
def devices():
    rows = list_rows(CON, 'devices', 1000)
    row = CON.execute("SELECT v FROM state WHERE k='online'").fetchone()
    online = json.loads(row['v']) if row else {}
    for r in rows:
        r['online'] = bool(online.get((r.get('mac') or '').lower(), False))
        r['watched'] = watched(CFG, r) is not None
    return rows

@app.get('/api/events')
def events(limit: int = 200):
    return list_rows(CON, 'presence_events', limit)

@app.get('/api/alerts')
def alerts(limit: int = 100):
    return list_rows(CON, 'alerts', limit)

@app.get('/api/stats')
def stats():
    day_ago = int(time.time()) - 86400
    devs = list_rows(CON, 'devices', 100000)
    row = CON.execute("SELECT v FROM state WHERE k='online'").fetchone()
    online = json.loads(row['v']) if row else {}
    snap = CON.execute('SELECT ts, source FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    sev = {r['severity']: r['n'] for r in CON.execute(
        'SELECT severity, COUNT(*) AS n FROM alerts WHERE ts>=? GROUP BY severity', (day_ago,))}
    return {
        'network': CFG.get('network_name'),
        'devices_total': len(devs),
        'devices_online': sum(1 for v in online.values() if v),
        'devices_watched': sum(1 for d in devs if watched(CFG, d)),
        'events_24h': count_since(CON, 'presence_events', day_ago),
        'alerts_24h': count_since(CON, 'alerts', day_ago),
        'alerts_by_severity_24h': sev,
        'events_per_hour': events_per_hour(CON, 24),
        'last_poll': dict(snap) if snap else None,
        'slack_configured': bool(webhook_urls(CFG)),
        'poll_interval_seconds': CFG.get('poll_interval_seconds', 300),
    }

@app.post('/api/poll')
async def manual_poll():
    await poll()
    return {'ok': True}

@app.post('/api/notify/test')
async def notify_test():
    results = await asyncio.to_thread(send_test, CFG)
    return {'ok': all(r.get('ok') for r in results) if results else False,
            'configured': bool(webhook_urls(CFG)), 'results': results}

@app.websocket('/ws')
async def ws(websocket: WebSocket):
    await websocket.accept()
    CLIENTS.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    finally:
        CLIENTS.discard(websocket)

@app.on_event('startup')
async def startup_event():
    global SCHED
    if SCHED is None:
        SCHED = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        SCHED.add_job(lambda: asyncio.create_task(poll()), 'interval', seconds=CFG.get('poll_interval_seconds', 300))
        SCHED.start()
        await poll()

@app.on_event('shutdown')
async def shutdown_event():
    global SCHED
    if SCHED:
        SCHED.shutdown(wait=False)
        SCHED = None

def create_app(config_path='config/config.example.yaml'):
    global CFG, CON
    CFG = load_cfg(config_path)
    CON = connect(CFG.get('database', './data/eero_intel.db'))
    if os.path.exists(CFG.get('dsarexport', '')):
        import_dsar(CFG['dsarexport'], CON)
    if not any(getattr(r, 'path', None) == '/static' for r in app.routes):
        app.mount('/static', StaticFiles(directory='web'), name='static')
    return app

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--config', default='config/config.example.yaml')
    p.add_argument('--import-dsar')
    p.add_argument('--run', action='store_true')
    a = p.parse_args()

    CFG = load_cfg(a.config)
    CON = connect(CFG.get('database', './data/eero_intel.db'))

    if a.import_dsar:
        print(f'imported {import_dsar(a.import_dsar, CON)} rows')

    if a.run:
        import uvicorn
        create_app(a.config)
        uvicorn.run(app, host=CFG['web']['host'], port=CFG['web']['port'])
