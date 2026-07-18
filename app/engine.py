"""Presence engine — polls eero, detects transitions, writes history.

Completely independent of the web layer: no FastAPI imports. A host (the web
app, a CLI, a test) constructs it with a config dict and a DB connection, and
calls poll_once() on a schedule. Listeners (Slack, MQTT, HomeKit, WebSocket
bridges) receive each generated event dict.

Event types: ARRIVED / LEFT (person devices), ONLINE / OFFLINE (other
devices), ROAMED (gateway change), RENAMED (name change).

Departures require `offline_confirmation_polls` consecutive missed polls
(default 2) before LEFT/OFFLINE fires, which suppresses false departures from
a single dropped poll or a device briefly sleeping its radio.
"""

import json, time, logging

from .db import add_event, add_alert, record_poll, record_node_sample
from .eero_api import get_adapter
from .security import grade

log = logging.getLogger('presence.engine')

PERSON_EVENTS = {True: ('ARRIVED', 'LEFT'), False: ('ONLINE', 'OFFLINE')}


class PresenceEngine:
    def __init__(self, cfg, con, listeners=None):
        self.cfg = cfg
        self.con = con
        self.listeners = list(listeners or [])
        self.adapter = get_adapter(cfg)
        self.confirm = max(1, int(cfg.get('offline_confirmation_polls', 2)))
        # notification dedupe: (mac, type) -> last dispatch ts
        self._recent = {}
        self._dedupe_window = max(60, int(cfg.get('poll_interval_seconds', 300)) - 5)

    # ── config helpers ────────────────────────────────────────────────────
    def watched(self, d):
        m = (d.get('mac') or '').lower()
        n = d.get('name') or ''
        for w in self.cfg.get('devices', []):
            mm = [x.lower() for x in (w.get('match', {}).get('macs') or [])]
            nn = w.get('match', {}).get('names') or []
            if (m and m in mm) or n in nn:
                return w
        return None

    def person_for(self, d, w=None):
        w = self.watched(d) if w is None else w
        return (w or {}).get('person') or None

    def persons(self):
        return sorted({w['person'] for w in self.cfg.get('devices', []) if w.get('person')})

    # ── presence summary (used by MQTT / HomeKit / API) ───────────────────
    def presence_summary(self):
        rows = [dict(r) for r in self.con.execute('SELECT * FROM devices')]
        person_state = {p: False for p in self.persons()}
        online = 0
        for r in rows:
            if r.get('online'):
                online += 1
                p = r.get('person')
                if p in person_state:
                    person_state[p] = True
        return {
            'persons': person_state,
            'people_home': sum(1 for v in person_state.values() if v),
            'devices_online': online,
            'devices_total': len(rows),
        }

    # ── polling ───────────────────────────────────────────────────────────
    def poll_once(self):
        started = time.time()
        devices, latency, error = [], None, None
        try:
            devices, latency = self.adapter.fetch()
            ok = True
        except Exception as ex:
            ok = False
            error = str(ex)
            log.error('poll failed error=%s', error)

        self.con.execute('INSERT INTO snapshots(ts,source,raw) VALUES(?,?,?)',
                         (int(started), 'api' if ok else f'api-error: {error}', json.dumps(devices)))
        self.con.commit()

        events = self._apply(devices) if ok else []
        record_poll(self.con, started, time.time(), latency, ok, error, len(devices), len(events))
        log.info('poll ok=%s devices=%d events=%d latency_ms=%s duration_ms=%d',
                 ok, len(devices), len(events), latency, int((time.time() - started) * 1000))

        for ev in events:
            self._dispatch(ev)
        return events

    # ── state machine ─────────────────────────────────────────────────────
    def _apply(self, devices):
        now = int(time.time())
        events = []
        rows = {r['mac']: dict(r) for r in self.con.execute('SELECT * FROM devices')}
        first_poll = self.con.execute("SELECT v FROM state WHERE k='initialized'").fetchone() is None
        seen = set()

        for d in devices:
            mac = (d.get('mac') or '').lower()
            if not mac or not bool(d.get('online', True)):
                continue  # devices the API lists as offline count as absent
            seen.add(mac)
            w = self.watched(d)
            person = self.person_for(d, w)
            prev = rows.get(mac)
            name = d.get('name') or (prev or {}).get('name') or mac
            up_ev, down_ev = PERSON_EVENTS[person is not None]

            if prev is None:
                self._write_device(mac, d, name, person, now, online=1, online_since=now, misses=0, first_seen=now)
                if not first_poll:
                    events.append(self._mk(up_ev, mac, name, person, d, now, is_new=True))
                continue

            was_online = bool(prev.get('online'))
            if not was_online:
                # On the very first poll pre-existing rows (v1 migration, DSAR
                # import) all carry online=0 — seed their state silently instead
                # of announcing an "arrival" for every connected device.
                if not first_poll:
                    events.append(self._mk(up_ev, mac, name, person, d, now))
                self._write_device(mac, d, name, person, now, online=1, online_since=now, misses=0)
            else:
                if prev.get('name') and name != prev['name']:
                    events.append(self._mk('RENAMED', mac, name, person, d, now,
                                           extra={'previous_name': prev['name']}))
                gw = d.get('gateway')
                if gw and prev.get('gateway') and gw != prev['gateway']:
                    events.append(self._mk('ROAMED', mac, name, person, d, now,
                                           extra={'from_gateway': prev['gateway'], 'to_gateway': gw}))
                    record_node_sample(self.con, now, mac, gw, d.get('rssi'), 'roam')
                elif d.get('rssi') is not None and d.get('rssi') != prev.get('rssi'):
                    record_node_sample(self.con, now, mac, gw or prev.get('gateway'), d.get('rssi'), 'sample')
                self._write_device(mac, d, name, person, now, online=1,
                                   online_since=prev.get('online_since') or now, misses=0)

        # absent devices: count misses, confirm departures
        for mac, prev in rows.items():
            if mac in seen or not prev.get('online'):
                continue
            misses = int(prev.get('misses') or 0) + 1
            if misses >= self.confirm:
                person = prev.get('person')
                _, down_ev = PERSON_EVENTS[bool(person)]
                session = now - int(prev.get('online_since') or now)
                events.append(self._mk(down_ev, mac, prev.get('name') or mac, person, prev, now,
                                       extra={'session_seconds': session,
                                              'last_seen_ts': self._parse_ts(prev.get('last_seen'))}))
                self.con.execute('UPDATE devices SET online=0, misses=0, online_since=NULL WHERE mac=?', (mac,))
            else:
                self.con.execute('UPDATE devices SET misses=? WHERE mac=?', (misses, mac))
        self.con.commit()

        if first_poll:
            self.con.execute("INSERT INTO state(k,v) VALUES('initialized','1') ON CONFLICT(k) DO NOTHING")
            self.con.commit()
        return events

    @staticmethod
    def _parse_ts(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _write_device(self, mac, d, name, person, now, **fields):
        base = {
            'name': name, 'hostname': d.get('hostname'), 'manufacturer': d.get('manufacturer'),
            'vendor': d.get('manufacturer') or d.get('vendor'), 'profile': d.get('profile'),
            'person': person, 'network_id': d.get('network_id'), 'ip': d.get('ip'),
            'last_ip': d.get('ip') or d.get('last_ip'), 'gateway': d.get('gateway'),
            'rssi': d.get('rssi'), 'last_seen': now, 'raw': json.dumps(d, default=str),
        }
        base.update(fields)
        first_seen = base.pop('first_seen', None)
        cols = ','.join(base)
        marks = ','.join('?' * len(base))
        sets = ','.join(f'{c}=excluded.{c}' for c in base)
        self.con.execute(
            f'INSERT INTO devices(mac,first_seen,{cols}) VALUES(?,?,{marks}) '
            f'ON CONFLICT(mac) DO UPDATE SET {sets}',
            (mac, first_seen, *base.values()))

    def _mk(self, type_, mac, name, person, d, now, is_new=False, extra=None):
        w = self.watched({'mac': mac, 'name': name})
        severity, notify = grade(self.cfg, type_, w, is_new=is_new)
        ev = {
            'ts': now, 'type': type_, 'mac': mac, 'name': name, 'person': person,
            'ip': d.get('ip') or d.get('last_ip'), 'gateway': d.get('gateway'),
            'rssi': d.get('rssi'), 'profile': d.get('profile'), 'network_id': d.get('network_id'),
            'is_new': is_new, 'severity': severity, 'notify': notify,
            'title': self._title(type_, name, person, is_new, extra),
        }
        ev.update(extra or {})
        return ev

    @staticmethod
    def _title(type_, name, person, is_new, extra):
        who = person or name
        if is_new and type_ in ('ONLINE', 'ARRIVED'):
            return f'Unknown device joined the network: {name}' if not person else f'New device for {person}: {name}'
        return {
            'ARRIVED': f'{who} arrived home ({name})',
            'LEFT': f'{who} left home ({name})',
            'ONLINE': f'{name} came online',
            'OFFLINE': f'{name} went offline',
            'ROAMED': f"{name} roamed {(extra or {}).get('from_gateway')} → {(extra or {}).get('to_gateway')}",
            'RENAMED': f"{(extra or {}).get('previous_name')} renamed to {name}",
        }.get(type_, f'{name}: {type_}')

    # ── event fan-out ─────────────────────────────────────────────────────
    def _dispatch(self, ev):
        add_event(self.con, ev)
        if ev['severity'] != 'info' or ev['notify']:
            add_alert(self.con, ev, notified=ev['notify'])

        key = (ev['mac'], ev['type'])
        if ev['notify'] and time.time() - self._recent.get(key, 0) < self._dedupe_window:
            log.info('notification deduped mac=%s type=%s', ev['mac'], ev['type'])
            ev = {**ev, 'notify': False}
        elif ev['notify']:
            self._recent[key] = time.time()

        for listener in self.listeners:
            try:
                listener(ev)
            except Exception as ex:
                log.error('listener failed listener=%s error=%s', getattr(listener, '__name__', listener), ex)
