import json, os, time, logging

log = logging.getLogger('presence.eero')

# This adapter intentionally isolates the unofficial eero library so the app
# keeps running if eero changes auth.


class EeroAdapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.retries = int(cfg.get('api_retries', 3))

    def fetch(self):
        """Return (devices, api_latency_ms). Retries with exponential backoff."""
        delay = 1
        for attempt in range(self.retries):
            t0 = time.time()
            try:
                devices = self._fetch_live()
                return devices, int((time.time() - t0) * 1000)
            except Exception as ex:
                if attempt == self.retries - 1:
                    raise RuntimeError(f'eero API unavailable/auth failed: {ex}')
                log.warning('eero fetch failed attempt=%d error=%s retry_in=%ds', attempt + 1, ex, delay)
                time.sleep(delay)
                delay *= 2

    def _fetch_live(self):
        from eero import Eero
        e = Eero()
        e.login(self.cfg['auth_source'])
        # Most community libs differ here. Keep raw export hook available.
        nets = e.get_networks() if hasattr(e, 'get_networks') else []
        devices = []
        for net in nets:
            clients = e.get_clients(net.get('url') or net.get('id')) if hasattr(e, 'get_clients') else []
            for c in clients:
                devices.append(normalize(c, net))
        return devices


class FakeAdapter:
    """Reads devices from a JSON file each poll — edit the file (or point
    EERO_FAKE_FILE elsewhere) to simulate arrivals, departures and roaming
    without touching the eero cloud. Used for demos and tests."""

    def __init__(self, cfg):
        self.path = os.environ.get('EERO_FAKE_FILE') or cfg.get('fake_devices_file', './data/fake_devices.json')

    def fetch(self):
        t0 = time.time()
        with open(self.path) as f:
            devices = [normalize(c) for c in json.load(f)]
        return devices, max(1, int((time.time() - t0) * 1000))


def get_adapter(cfg):
    if os.environ.get('EERO_ADAPTER', cfg.get('adapter', 'eero')) == 'fake':
        return FakeAdapter(cfg)
    return EeroAdapter(cfg)


def normalize(c, net=None):
    src = c.get('source') or {}
    return {
        'mac': (c.get('mac') or c.get('mac_address') or '').lower(),
        'name': c.get('nickname') or c.get('name') or c.get('hostname'),
        'hostname': c.get('hostname'),
        'ip': c.get('ip') or c.get('ip_address'),
        'online': bool(c.get('connected') or c.get('online')),
        'rssi': c.get('rssi') or c.get('signal_strength') or (c.get('connectivity') or {}).get('signal'),
        'gateway': c.get('gateway') or src.get('location') or src.get('display_name'),
        'manufacturer': c.get('manufacturer') or c.get('vendor'),
        'profile': (c.get('profile') or {}).get('name') if isinstance(c.get('profile'), dict) else c.get('profile'),
        'network_id': (net or {}).get('id'),
        'raw': c,
    }
