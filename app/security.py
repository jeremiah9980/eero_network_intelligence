"""Security classification for presence events.

Turns raw presence events (discovered / entered / left) into graded security
alerts so the dashboard and notifiers can treat "known iPhone came home" and
"never-seen device joined the network" very differently.
"""

SEVERITIES = ('info', 'notice', 'warning', 'critical')


def classify(event, device, watched_entry, first_poll=False):
    """Return (severity, title) for a presence event.

    severity is one of SEVERITIES; title is a human-readable alert headline.
    """
    name = device.get('name') or device.get('mac') or 'Unknown device'

    if event == 'discovered':
        if first_poll:
            # Initial inventory sync — everything looks "new", don't alarm.
            return 'info', f'{name} inventoried'
        if watched_entry is None:
            return 'warning', f'Unknown device joined the network: {name}'
        return 'notice', f'New watched device discovered: {name}'

    if event == 'entered':
        if watched_entry is not None:
            return 'notice', f'{name} entered the network'
        return 'info', f'{name} entered the network'

    if event == 'left':
        if watched_entry is not None:
            return 'notice', f'{name} left the network'
        return 'info', f'{name} left the network'

    return 'info', f'{name}: {event}'


def should_notify(cfg, severity, watched_entry):
    """Notify Slack for any watched device change, and always for warnings+."""
    n = cfg.get('notifications', {}) or {}
    if SEVERITIES.index(severity) >= SEVERITIES.index('warning'):
        return bool(n.get('notify_on_new_device', True))
    if watched_entry is not None:
        return bool(watched_entry.get('notify', True))
    return False
