import asyncio, json, time, yaml, argparse, os, logging, signal
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from .db import (connect, list_rows, events_per_hour, count_since, device_sessions)
from .dsar_import import import_dsar
from .engine import PresenceEngine
from .notifiers import (notify_event, notify_daily_summary, send_test, webhook_urls, fmt_duration)

load_dotenv()
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('presence.web')

CFG = {}
CON = None
ENGINE = None
CLIENTS = set()
SCHED = None
MQTT = None
HOMEKIT = None
LOOP = None
app = FastAPI(title='eero Presence Intelligence Platform')


async def broadcast(msg):
    for ws in list(CLIENTS):
        try:
            await ws.send_json(msg)
        except Exception:
            CLIENTS.discard(ws)


def load_cfg(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── engine listeners (run in the engine's poll thread) ────────────────────
def _slack_listener(ev):
    if ev.get('notify'):
        notify_event(CFG, ev)


def _ws_listener(ev):
    if LOOP and CFG.get('websocket_enabled', True):
        asyncio.run_coroutine_threadsafe(broadcast({'type': 'event', 'event': ev}), LOOP)


def _mqtt_listener(ev):
    if MQTT:
        MQTT.handle_event(ev)


async def scheduled_poll():
    events = await asyncio.to_thread(ENGINE.poll_once)
    summary = ENGINE.presence_summary()
    if MQTT:
        await asyncio.to_thread(MQTT.publish_summary, summary)
    if HOMEKIT:
        HOMEKIT.publish_summary(summary)
    if events:
        await broadcast({'type': 'refresh', 'summary': summary})
    return events


async def watchdog():
    """Self-heal: if the last poll is overdue, run one now."""
    interval = CFG.get('poll_interval_seconds', 300)
    row = CON.execute('SELECT started_ts FROM poll_history ORDER BY id DESC LIMIT 1').fetchone()
    if row and time.time() - row['started_ts'] > 3 * interval:
        log.error('watchdog: last poll %ds ago (interval %ds) — forcing poll',
                  int(time.time() - row['started_ts']), interval)
        await scheduled_poll()


async def daily_summary_job():
    return await asyncio.to_thread(notify_daily_summary, CFG, build_daily_summary())


def build_daily_summary():
    midnight = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    s = ENGINE.presence_summary()
    home = [dict(r)['name'] for r in CON.execute('SELECT name FROM devices WHERE online=1 ORDER BY name')]
    return {
        'date': time.strftime('%Y-%m-%d'),
        'people_home': s['people_home'], 'devices_online': s['devices_online'], 'home_names': home,
        'arrivals': count_since(CON, 'presence_events', midnight, "AND event IN ('ARRIVED','ONLINE')"),
        'departures': count_since(CON, 'presence_events', midnight, "AND event IN ('LEFT','OFFLINE')"),
        'events': count_since(CON, 'presence_events', midnight),
    }


# ── pages ─────────────────────────────────────────────────────────────────
@app.get('/')
def home():
    return FileResponse('web/index.html')


# ── presence & inventory ──────────────────────────────────────────────────
@app.get('/api/presence')
def presence():
    s = ENGINE.presence_summary()
    now = int(time.time())
    online = [dict(r) for r in CON.execute('SELECT * FROM devices WHERE online=1 ORDER BY online_since')]
    return {
        **s,
        'home': [{'name': d['name'], 'mac': d['mac'], 'person': d['person'],
                  'gateway': d['gateway'], 'rssi': d['rssi'], 'ip': d['ip'],
                  'online_since': d['online_since'],
                  'duration': fmt_duration(now - d['online_since']) if d['online_since'] else None}
                 for d in online],
    }


@app.get('/api/devices')
def devices():
    rows = list_rows(CON, 'devices', 1000)
    for r in rows:
        r['watched'] = ENGINE.watched(r) is not None
        r.pop('raw', None)
    return rows


@app.get('/api/device/{mac}')
def device_detail(mac: str):
    mac = mac.lower()
    row = CON.execute('SELECT * FROM devices WHERE mac=?', (mac,)).fetchone()
    if not row:
        raise HTTPException(404, 'unknown device')
    d = dict(row)
    week_ago = int(time.time()) - 7 * 86400
    sessions = device_sessions(CON, mac)
    recent = [s for s in sessions if s['start'] >= week_ago]
    online_seconds = sum(s['seconds'] for s in recent)
    events = [dict(r) for r in CON.execute(
        'SELECT ts,event,name,ip,signal FROM presence_events WHERE mac=? ORDER BY ts DESC LIMIT 100', (mac,))]
    return {
        'device': d,
        'events': events,
        'sessions': sessions,
        'signal_history': [dict(r) for r in CON.execute(
            'SELECT ts,gateway,rssi,change FROM node_history WHERE mac=? ORDER BY ts DESC LIMIT 200', (mac,))],
        'ip_history': [dict(r) for r in CON.execute(
            'SELECT DISTINCT ip FROM presence_events WHERE mac=? AND ip IS NOT NULL LIMIT 20', (mac,))],
        'analytics': {
            'sessions_7d': len(recent),
            'online_pct_7d': round(100 * online_seconds / (7 * 86400), 1),
            'avg_session': fmt_duration(online_seconds // len(recent)) if recent else None,
        },
    }


# ── history & diagnostics ─────────────────────────────────────────────────
@app.get('/api/events')
def events(limit: int = 200):
    return list_rows(CON, 'presence_events', limit)


@app.get('/api/alerts')
def alerts(limit: int = 100):
    return list_rows(CON, 'alerts', limit)


@app.get('/api/polls')
def polls(limit: int = 50):
    return list_rows(CON, 'poll_history', limit)


@app.get('/api/analytics')
def analytics():
    now = int(time.time())
    daily = [dict(r) for r in CON.execute('''
        SELECT date(ts,'unixepoch','localtime') AS day,
               SUM(event IN ('ARRIVED','ONLINE')) AS arrivals,
               SUM(event IN ('LEFT','OFFLINE')) AS departures
        FROM presence_events WHERE ts>=? GROUP BY day ORDER BY day''', (now - 14 * 86400,))]
    active = [dict(r) for r in CON.execute('''
        SELECT mac, name, COUNT(*) AS events FROM presence_events
        WHERE ts>=? GROUP BY mac ORDER BY events DESC LIMIT 10''', (now - 7 * 86400,))]
    heat = [dict(r) for r in CON.execute('''
        SELECT CAST(strftime('%w', ts,'unixepoch','localtime') AS INT) AS dow,
               CAST(strftime('%H', ts,'unixepoch','localtime') AS INT) AS hour,
               COUNT(*) AS n
        FROM presence_events WHERE ts>=? GROUP BY dow, hour''', (now - 28 * 86400,))]
    return {'daily': daily, 'most_active': active, 'occupancy_heatmap': heat}


@app.get('/api/health')
def health():
    interval = CFG.get('poll_interval_seconds', 300)
    last = CON.execute('SELECT * FROM poll_history ORDER BY id DESC LIMIT 1').fetchone()
    last = dict(last) if last else None
    age = int(time.time()) - last['started_ts'] if last else None
    degraded = last is None or not last['success'] or (age is not None and age > 3 * interval)
    return {
        'ok': not degraded, 'status': 'degraded' if degraded else 'healthy',
        'last_poll': last, 'last_poll_age_seconds': age,
        'devices': CON.execute('SELECT COUNT(*) AS n FROM devices').fetchone()['n'],
        'ws_clients': len(CLIENTS), 'slack_configured': bool(webhook_urls(CFG)),
        'mqtt': MQTT is not None, 'homekit': HOMEKIT is not None,
    }


# ── dashboard payload ─────────────────────────────────────────────────────
@app.get('/api/dashboard')
def dashboard():
    now = int(time.time())
    day_ago = now - 86400
    pres = presence()
    last_polls = list_rows(CON, 'poll_history', 12)
    evs = [dict(r) for r in CON.execute('SELECT * FROM presence_events ORDER BY ts DESC LIMIT 40')]
    for e in evs:
        try:
            e['detail'] = json.loads(e.pop('raw') or '{}')
        except Exception:
            e['detail'] = {}
    sev = {r['severity']: r['n'] for r in CON.execute(
        'SELECT severity, COUNT(*) AS n FROM alerts WHERE ts>=? GROUP BY severity', (day_ago,))}
    return {
        'network': CFG.get('network_name'),
        'presence': pres,
        'cards': {
            'devices_online': pres['devices_online'], 'devices_total': pres['devices_total'],
            'people_home': pres['people_home'], 'people_total': len(ENGINE.persons()),
            'last_poll': dict(last_polls[0]) if last_polls else None,
            'poll_interval': CFG.get('poll_interval_seconds', 300),
            'alerts_24h': count_since(CON, 'alerts', day_ago),
            'alerts_by_severity_24h': sev,
            'events_24h': count_since(CON, 'presence_events', day_ago),
            'slack_configured': bool(webhook_urls(CFG)),
        },
        'arrivals': [e for e in evs if e['event'] in ('ARRIVED', 'ONLINE')][:6],
        'departures': [e for e in evs if e['event'] in ('LEFT', 'OFFLINE')][:6],
        'timeline': evs[:30],
        'alerts': list_rows(CON, 'alerts', 30),
        'polls': last_polls,
        'events_per_hour': events_per_hour(CON, 24),
    }


# ── actions ───────────────────────────────────────────────────────────────
@app.post('/api/poll')
async def manual_poll():
    events = await scheduled_poll()
    return {'ok': True, 'events': len(events)}


@app.post('/api/notify/test')
async def notify_test():
    results = await asyncio.to_thread(send_test, CFG)
    return {'ok': all(r.get('ok') for r in results) if results else False,
            'configured': bool(webhook_urls(CFG)), 'results': results}


@app.post('/api/notify/summary')
async def notify_summary():
    results = await daily_summary_job()
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


# ── lifecycle ─────────────────────────────────────────────────────────────
@app.on_event('startup')
async def startup_event():
    global SCHED, LOOP, MQTT, HOMEKIT
    LOOP = asyncio.get_running_loop()

    if (CFG.get('mqtt', {}) or {}).get('enabled'):
        try:
            from .integrations.mqtt import MqttPublisher
            MQTT = MqttPublisher(CFG, ENGINE.persons())
            await asyncio.to_thread(MQTT.start)
        except Exception as ex:
            log.error('mqtt disabled error=%s', ex)
            MQTT = None
    if (CFG.get('homekit', {}) or {}).get('enabled'):
        try:
            from .integrations.homekit import HomeKitBridge
            hk = HomeKitBridge(CFG, ENGINE.persons())
            HOMEKIT = hk if hk.start() else None
        except Exception as ex:
            log.error('homekit disabled error=%s', ex)
            HOMEKIT = None

    if SCHED is None:
        interval = CFG.get('poll_interval_seconds', 300)
        SCHED = AsyncIOScheduler(event_loop=LOOP)
        SCHED.add_job(scheduled_poll, 'interval', seconds=interval, id='poll',
                      max_instances=1, coalesce=True)
        SCHED.add_job(watchdog, 'interval', seconds=max(60, interval), id='watchdog')
        t = (CFG.get('notifications', {}) or {}).get('daily_summary_time')
        if t and webhook_urls(CFG):
            try:
                hour, minute = str(t).strip().split(':')
                SCHED.add_job(daily_summary_job, CronTrigger(hour=int(hour), minute=int(minute)), id='daily_summary')
            except (ValueError, TypeError):
                log.error('invalid notifications.daily_summary_time=%r — expected "HH:MM"; daily summary disabled', t)
                t = None
        SCHED.start()
        log.info('scheduler started interval=%ds summary=%s', interval, t or 'off')
        await scheduled_poll()


@app.on_event('shutdown')
async def shutdown_event():
    global SCHED
    log.info('shutting down gracefully')
    if SCHED:
        SCHED.shutdown(wait=False)
        SCHED = None
    if MQTT:
        MQTT.stop()
    if HOMEKIT:
        HOMEKIT.stop()


def create_app(config_path='config/config.example.yaml'):
    global CFG, CON, ENGINE
    CFG = load_cfg(config_path)
    CON = connect(CFG.get('database', './data/eero_intel.db'))
    ENGINE = PresenceEngine(CFG, CON, listeners=[_slack_listener, _ws_listener, _mqtt_listener])
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
