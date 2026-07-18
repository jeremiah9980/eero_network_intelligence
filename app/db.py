import sqlite3, json, time
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS devices(
  mac TEXT PRIMARY KEY, name TEXT, hostname TEXT, vendor TEXT, manufacturer TEXT,
  profile TEXT, person TEXT, network_id TEXT, ip TEXT, last_ip TEXT, gateway TEXT,
  rssi INTEGER, online INTEGER DEFAULT 0, online_since INTEGER, misses INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS presence_events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mac TEXT, name TEXT, event TEXT, ip TEXT, network_id TEXT, signal TEXT, profile TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, source TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, severity TEXT, title TEXT, mac TEXT, name TEXT, event TEXT, notified INTEGER DEFAULT 0, raw TEXT);
CREATE TABLE IF NOT EXISTS poll_history(id INTEGER PRIMARY KEY AUTOINCREMENT, started_ts INTEGER, finished_ts INTEGER, duration_ms INTEGER, api_latency_ms INTEGER, success INTEGER, error TEXT, device_count INTEGER, event_count INTEGER);
CREATE TABLE IF NOT EXISTS node_history(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mac TEXT, gateway TEXT, rssi INTEGER, change TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON presence_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_mac ON presence_events(mac);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_polls_ts ON poll_history(started_ts);
CREATE INDEX IF NOT EXISTS idx_nodes_mac ON node_history(mac);
'''

# Columns added since v1 — applied to pre-existing databases on connect.
V2_DEVICE_COLUMNS = {
    'manufacturer': 'TEXT', 'person': 'TEXT', 'ip': 'TEXT', 'gateway': 'TEXT',
    'rssi': 'INTEGER', 'online': 'INTEGER DEFAULT 0', 'online_since': 'INTEGER',
    'misses': 'INTEGER DEFAULT 0',
}

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate(con)
    return con

def migrate(con):
    cols = {r['name'] for r in con.execute('PRAGMA table_info(devices)')}
    for c, t in V2_DEVICE_COLUMNS.items():
        if c not in cols:
            con.execute(f'ALTER TABLE devices ADD COLUMN {c} {t}')
    con.commit()

def upsert_device(con, d):
    # Legacy/DSAR-import path — engine.py maintains live state itself.
    mac = (d.get('mac') or d.get('MAC Address') or '').lower()
    if not mac:
        return
    name = d.get('name') or d.get('Nickname') or d.get('nickname') or d.get('hostname') or d.get('Hostname') or mac
    con.execute('''INSERT INTO devices(mac,name,hostname,vendor,profile,network_id,first_seen,last_seen,last_ip,raw)
    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET name=excluded.name,hostname=excluded.hostname,vendor=excluded.vendor,profile=excluded.profile,network_id=excluded.network_id,last_seen=excluded.last_seen,last_ip=excluded.last_ip,raw=excluded.raw''',
    (mac, name, d.get('hostname') or d.get('Hostname'), d.get('vendor') or d.get('Org Name'), d.get('profile') or d.get('profileName'), d.get('network_id') or d.get('Network ID'), d.get('created') or d.get('first_seen'), d.get('last_seen') or d.get('Last Seen At'), d.get('ip') or d.get('ip_address'), json.dumps(d)))
    con.commit()

def add_event(con, ev):
    con.execute('INSERT INTO presence_events(ts,mac,name,event,ip,network_id,signal,profile,raw) VALUES(?,?,?,?,?,?,?,?,?)',
        (ev.get('ts') or int(time.time()), ev.get('mac'), ev.get('name'), ev.get('type'), ev.get('ip'), ev.get('network_id'), str(ev.get('rssi') or ''), ev.get('profile'), json.dumps(ev)))
    con.commit()

def add_alert(con, ev, notified=False):
    con.execute('INSERT INTO alerts(ts,severity,title,mac,name,event,notified,raw) VALUES(?,?,?,?,?,?,?,?)',
        (ev.get('ts') or int(time.time()), ev.get('severity', 'info'), ev.get('title'), ev.get('mac'), ev.get('name'), ev.get('type'), 1 if notified else 0, json.dumps(ev)))
    con.commit()

def record_poll(con, started, finished, latency_ms, success, error, device_count, event_count):
    con.execute('INSERT INTO poll_history(started_ts,finished_ts,duration_ms,api_latency_ms,success,error,device_count,event_count) VALUES(?,?,?,?,?,?,?,?)',
        (int(started), int(finished), int((finished - started) * 1000), latency_ms, 1 if success else 0, error, device_count, event_count))
    con.commit()

def record_node_sample(con, ts, mac, gateway, rssi, change):
    # No commit here — called per-device inside a poll; the engine commits once
    # at the end of the batch.
    con.execute('INSERT INTO node_history(ts,mac,gateway,rssi,change) VALUES(?,?,?,?,?)', (ts, mac, gateway, rssi, change))

def list_rows(con, table, limit=200):
    return [dict(r) for r in con.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?', (limit,))]

def events_per_hour(con, hours=24):
    since = int(time.time()) - hours * 3600
    return [dict(r) for r in con.execute('SELECT (ts/3600)*3600 AS hour, COUNT(*) AS n FROM presence_events WHERE ts>=? GROUP BY hour ORDER BY hour', (since,))]

def count_since(con, table, since, where='', args=()):
    return con.execute(f'SELECT COUNT(*) AS n FROM {table} WHERE ts>=? {where}', (since, *args)).fetchone()['n']

def device_sessions(con, mac, limit=50):
    """Pair up ARRIVED/ONLINE → LEFT/OFFLINE transitions into sessions."""
    rows = [dict(r) for r in con.execute(
        "SELECT ts, event, raw FROM presence_events WHERE mac=? AND event IN ('ARRIVED','LEFT','ONLINE','OFFLINE') ORDER BY ts", (mac,))]
    sessions, start = [], None
    for r in rows:
        if r['event'] in ('ARRIVED', 'ONLINE'):
            start = r['ts']
        else:
            if start is None:
                # Departure with no recorded arrival (device predates history or
                # was inventoried on the first poll) — reconstruct from the
                # session duration stamped on the departure event.
                try:
                    secs = json.loads(r['raw'] or '{}').get('session_seconds')
                except ValueError:
                    secs = None
                if secs is not None:
                    start = r['ts'] - int(secs)
            if start is not None:
                sessions.append({'start': start, 'end': r['ts'], 'seconds': r['ts'] - start})
                start = None
    if start is not None:
        sessions.append({'start': start, 'end': None, 'seconds': int(time.time()) - start})
    return sessions[-limit:]
