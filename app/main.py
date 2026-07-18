import asyncio, json, time, yaml, argparse, os
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .db import connect, upsert_device, add_event, list_rows
from .dsar_import import import_dsar
from .eero_api import EeroAdapter
from .notifiers import send

CFG = {}
CON = None
CLIENTS = set()
SCHED = None
app = FastAPI(title='eero Network Intelligence')

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
    now = {}

    for d in devices:
        upsert_device(CON, d)
        mac = (d.get('mac') or '').lower()
        if not mac:
            continue
        online = bool(d.get('online', True))
        now[mac] = online

        if prev.get(mac) is not None and prev[mac] != online:
            ev = 'entered' if online else 'left'
            add_event(CON, d, ev)
            if watched(CFG, d):
                send(CFG, f"{d.get('name') or mac} {ev} eero network", d)
            await broadcast({'type': 'presence', 'event': ev, 'device': d})
        elif mac not in prev:
            add_event(CON, d, 'discovered')
            await broadcast({'type': 'discovered', 'device': d})

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
    return list_rows(CON, 'devices', 1000)

@app.get('/api/events')
def events(limit: int = 200):
    return list_rows(CON, 'presence_events', limit)

@app.post('/api/poll')
async def manual_poll():
    await poll()
    return {'ok': True}

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
        SCHED.add_job(lambda: asyncio.create_task(poll()), 'interval', seconds=CFG.get('poll_interval_seconds', 60))
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
