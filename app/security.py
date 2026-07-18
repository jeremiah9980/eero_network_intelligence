"""Security grading for presence events.

Maps engine events (ARRIVED / LEFT / ONLINE / OFFLINE / ROAMED / RENAMED) to a
severity and decides which deserve a notification, so "known iPhone came home"
and "never-seen device joined the network" are treated very differently.
"""

SEVERITIES = ('info', 'notice', 'warning', 'critical')


def grade(cfg, event_type, watched_entry, is_new=False, first_poll=False):
    """Return (severity, notify) for an engine event."""
    n = cfg.get('notifications', {}) or {}
    slack_on = bool(n.get('slack_enabled', True))

    if first_poll:
        return 'info', False

    if is_new and watched_entry is None:
        # Never-before-seen device on the network — the security case.
        return 'warning', slack_on and bool(n.get('notify_on_new_device', True))

    if event_type in ('ARRIVED', 'LEFT'):
        notify = watched_entry is None or bool(watched_entry.get('notify', True))
        return 'notice', slack_on and notify

    if event_type in ('ONLINE', 'OFFLINE'):
        if watched_entry is not None:
            return 'notice', slack_on and bool(watched_entry.get('notify', True))
        return 'info', False

    # ROAMED / RENAMED — inventory bookkeeping, never notified.
    return 'info', False
