import os, time, logging
import requests
from datetime import datetime, timezone

log = logging.getLogger('presence.notify')

EVENT_STYLE = {
    'ARRIVED':  {'emoji': ':large_green_circle:', 'color': '#0ca30c', 'label': 'ARRIVED'},
    'LEFT':     {'emoji': ':red_circle:',         'color': '#d03b3b', 'label': 'LEFT'},
    'ONLINE':   {'emoji': ':large_blue_circle:',  'color': '#3987e5', 'label': 'ONLINE'},
    'OFFLINE':  {'emoji': ':white_circle:',       'color': '#898781', 'label': 'OFFLINE'},
    'ROAMED':   {'emoji': ':signal_strength:',    'color': '#3987e5', 'label': 'ROAMED'},
    'RENAMED':  {'emoji': ':label:',              'color': '#3987e5', 'label': 'RENAMED'},
}
SEVERITY_STYLE = {
    'warning':  {'emoji': ':warning:',        'color': '#fab219'},
    'critical': {'emoji': ':rotating_light:', 'color': '#d03b3b'},
}


def webhook_urls(config):
    """Config-listed webhook URLs plus SLACK_WEBHOOK_URL from the environment."""
    n = (config.get('notifications', {}) or {})
    urls = [u for u in (n.get('webhook_urls') or []) if u]
    env_url = os.environ.get('SLACK_WEBHOOK_URL', '').strip()
    if env_url and env_url not in urls:
        urls.append(env_url)
    return urls


def fmt_duration(seconds):
    if seconds is None:
        return '–'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    return f'{seconds // 3600}h {seconds % 3600 // 60}m'


def _fmt_time(ts=None):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')


def _post(url, body, retries=3):
    delay = 1
    for attempt in range(retries):
        try:
            r = requests.post(url, json=body, timeout=8)
            if r.ok:
                return {'status': r.status_code, 'ok': True}
            if r.status_code < 500:
                return {'status': r.status_code, 'ok': False}
        except Exception as ex:
            if attempt == retries - 1:
                return {'status': None, 'ok': False, 'error': str(ex)}
        time.sleep(delay)
        delay *= 2
    return {'status': None, 'ok': False, 'error': 'retries exhausted'}


def _deliver(config, body):
    results = []
    for url in webhook_urls(config):
        res = _post(url, body)
        res['url'] = url[:40] + '…'
        results.append(res)
        log.info('slack delivery status=%s ok=%s', res.get('status'), res.get('ok'))
    return results


def event_blocks(ev):
    """Block Kit message for an engine presence event."""
    style = dict(EVENT_STYLE.get(ev.get('type'), EVENT_STYLE['ONLINE']))
    style.update(SEVERITY_STYLE.get(ev.get('severity'), {}))
    fields = [
        {'type': 'mrkdwn', 'text': f"*Device:*\n{ev.get('name') or 'Unknown'}"},
        {'type': 'mrkdwn', 'text': f"*MAC:*\n`{ev.get('mac') or 'n/a'}`"},
    ]
    if ev.get('person'):
        fields.append({'type': 'mrkdwn', 'text': f"*Person:*\n{ev['person']}"})
    if ev.get('ip'):
        fields.append({'type': 'mrkdwn', 'text': f"*IP:*\n`{ev['ip']}`"})
    if ev.get('gateway'):
        fields.append({'type': 'mrkdwn', 'text': f"*Gateway:*\n{ev['gateway']}"})
    if ev.get('rssi') is not None:
        fields.append({'type': 'mrkdwn', 'text': f"*RSSI:*\n{ev['rssi']} dBm"})
    if ev.get('session_seconds') is not None:
        fields.append({'type': 'mrkdwn', 'text': f"*Session:*\n{fmt_duration(ev['session_seconds'])}"})
    fields.append({'type': 'mrkdwn', 'text': f"*Time:*\n{_fmt_time(ev.get('ts'))}"})

    headline = f"{style['emoji']} *{style['label']}* — {ev.get('title')}"
    return {
        'text': f"[{style['label']}] {ev.get('title')}",
        'attachments': [{
            'color': style['color'],
            'blocks': [
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': headline}},
                {'type': 'section', 'fields': fields[:10]},
            ],
        }],
    }


def notify_event(config, ev):
    """Deliver one engine event to all webhooks (call off the event loop)."""
    return _deliver(config, event_blocks(ev))


def daily_summary_blocks(summary):
    home = summary.get('home_names') or []
    lines = '\n'.join(f'• {n}' for n in home) or '_Nobody home_'
    return {
        'text': f"Daily summary — {summary.get('people_home', 0)} home, "
                f"{summary.get('arrivals', 0)} arrivals, {summary.get('departures', 0)} departures",
        'attachments': [{
            'color': '#3987e5',
            'blocks': [
                {'type': 'section', 'text': {'type': 'mrkdwn',
                    'text': f":house: *Daily Presence Summary — {summary.get('date')}*"}},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f'*Currently home:*\n{lines}'}},
                {'type': 'section', 'fields': [
                    {'type': 'mrkdwn', 'text': f"*Today's arrivals:*\n{summary.get('arrivals', 0)}"},
                    {'type': 'mrkdwn', 'text': f"*Today's departures:*\n{summary.get('departures', 0)}"},
                    {'type': 'mrkdwn', 'text': f"*Devices online:*\n{summary.get('devices_online', 0)}"},
                    {'type': 'mrkdwn', 'text': f"*Events today:*\n{summary.get('events', 0)}"},
                ]},
            ],
        }],
    }


def notify_daily_summary(config, summary):
    return _deliver(config, daily_summary_blocks(summary))


def send_test(config):
    """Send a test notification so webhook wiring can be verified end to end."""
    return notify_event(config, {
        'type': 'ONLINE', 'severity': 'info',
        'title': 'Test notification — presence platform is connected to Slack.',
        'name': 'Dashboard self-test', 'mac': 'n/a', 'ts': int(time.time()),
    })
