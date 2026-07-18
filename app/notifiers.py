import os
import requests
from datetime import datetime, timezone

SEVERITY_STYLE = {
    'info':     {'emoji': ':information_source:', 'color': '#3987e5', 'label': 'Info'},
    'notice':   {'emoji': ':bell:',               'color': '#0ca30c', 'label': 'Notice'},
    'warning':  {'emoji': ':warning:',            'color': '#fab219', 'label': 'Warning'},
    'critical': {'emoji': ':rotating_light:',     'color': '#d03b3b', 'label': 'Critical'},
}


def webhook_urls(config):
    """Config-listed webhook URLs plus SLACK_WEBHOOK_URL from the environment."""
    n = (config.get('notifications', {}) or {})
    urls = [u for u in (n.get('webhook_urls') or []) if u]
    env_url = os.environ.get('SLACK_WEBHOOK_URL', '').strip()
    if env_url and env_url not in urls:
        urls.append(env_url)
    return urls


def _device_detail(payload):
    payload = payload or {}
    name = payload.get('name') or payload.get('hostname') or payload.get('mac') or 'Unknown device'
    mac = payload.get('mac') or 'unknown-mac'
    ip = payload.get('ip') or payload.get('last_ip') or 'unknown-ip'
    network = payload.get('network_id') or 'unknown-network'
    profile = payload.get('profile') or 'no-profile'
    return name, mac, ip, network, profile


def slack_body(text, payload=None, severity='info'):
    name, mac, ip, network, profile = _device_detail(payload)
    style = SEVERITY_STYLE.get(severity, SEVERITY_STYLE['info'])
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    return {
        'text': f"{style['emoji']} [{style['label']}] {text}",
        'attachments': [
            {
                'color': style['color'],
                'blocks': [
                    {
                        'type': 'section',
                        'text': {
                            'type': 'mrkdwn',
                            'text': f"{style['emoji']} *eero Presence Security — {style['label']}*\n{text}"
                        }
                    },
                    {
                        'type': 'section',
                        'fields': [
                            {'type': 'mrkdwn', 'text': f'*Device:*\n{name}'},
                            {'type': 'mrkdwn', 'text': f'*MAC:*\n`{mac}`'},
                            {'type': 'mrkdwn', 'text': f'*IP:*\n`{ip}`'},
                            {'type': 'mrkdwn', 'text': f'*Profile:*\n{profile}'},
                            {'type': 'mrkdwn', 'text': f'*Network:*\n{network}'},
                            {'type': 'mrkdwn', 'text': f'*Time:*\n{ts}'},
                        ]
                    }
                ]
            }
        ],
    }


def send(config, text, payload=None, severity='info'):
    """Post a presence alert to every configured webhook. Returns delivery results."""
    payload = payload or {}
    body = slack_body(text, payload, severity)
    results = []

    for url in webhook_urls(config):
        try:
            r = requests.post(url, json=body, timeout=8)
            results.append({'url': url[:40] + '…', 'status': r.status_code, 'ok': r.ok})
        except Exception as ex:
            results.append({'url': url[:40] + '…', 'status': None, 'ok': False, 'error': str(ex)})

    n = (config.get('notifications', {}) or {})
    if n.get('pushcut_url'):
        try:
            requests.post(n['pushcut_url'], json={'text': text, **payload}, timeout=8)
        except Exception:
            pass

    return results


def send_test(config):
    """Send a test notification so webhook wiring can be verified end to end."""
    return send(
        config,
        'Test notification — presence security dashboard is connected to Slack.',
        {'name': 'Dashboard self-test', 'mac': 'n/a', 'ip': 'n/a'},
        severity='info',
    )
