import sqlite3, json, time
from pathlib import Path
SCHEMA='''
CREATE TABLE IF NOT EXISTS devices(mac TEXT PRIMARY KEY, name TEXT, hostname TEXT, vendor TEXT, profile TEXT, network_id TEXT, first_seen TEXT, last_seen TEXT, last_ip TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS presence_events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mac TEXT, name TEXT, event TEXT, ip TEXT, network_id TEXT, signal TEXT, profile TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, source TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
'''
def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True); con=sqlite3.connect(path, check_same_thread=False); con.row_factory=sqlite3.Row; con.executescript(SCHEMA); return con
def upsert_device(con,d):
    mac=(d.get('mac') or d.get('MAC Address') or '').lower();
    if not mac: return
    name=d.get('name') or d.get('Nickname') or d.get('nickname') or d.get('hostname') or d.get('Hostname') or mac
    con.execute('''INSERT INTO devices(mac,name,hostname,vendor,profile,network_id,first_seen,last_seen,last_ip,raw)
    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET name=excluded.name,hostname=excluded.hostname,vendor=excluded.vendor,profile=excluded.profile,network_id=excluded.network_id,last_seen=excluded.last_seen,last_ip=excluded.last_ip,raw=excluded.raw''',
    (mac,name,d.get('hostname') or d.get('Hostname'),d.get('vendor') or d.get('Org Name'),d.get('profile') or d.get('profileName'),d.get('network_id') or d.get('Network ID'),d.get('created') or d.get('first_seen'),d.get('last_seen') or d.get('Last Seen At'),d.get('ip') or d.get('ip_address'),json.dumps(d)))
    con.commit()
def add_event(con,d,event):
    con.execute('INSERT INTO presence_events(ts,mac,name,event,ip,network_id,signal,profile,raw) VALUES(?,?,?,?,?,?,?,?,?)',(int(time.time()),(d.get('mac') or '').lower(),d.get('name'),event,d.get('ip'),d.get('network_id'),str(d.get('signal') or ''),d.get('profile'),json.dumps(d)))
    con.commit()
def list_rows(con, table, limit=200):
    return [dict(r) for r in con.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?', (limit,))]
