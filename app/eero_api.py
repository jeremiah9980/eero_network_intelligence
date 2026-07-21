import json, os, re, time, logging
import requests

log = logging.getLogger('presence.eero')

# Native client for the unofficial eero cloud API (api-user.e2ro.com).
# Kept isolated here so the rest of the app survives if eero changes auth.
API = 'https://api-user.e2ro.com/2.2'


class EeroCloud:
    """Session management + device fetch against the eero cloud.

    One-time auth: start_login() sends a verification code to the account
    email/phone, verify() confirms it; the session token is stored in
    session_file and refreshed automatically on 401.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.session_file = cfg.get('session_file', './data/eero_session.cookie')

    # ── session ───────────────────────────────────────────────────────────
    def _token(self):
        try:
            return open(self.session_file).read().strip() or None
        except OSError:
            return None

    def _save(self, token):
        os.makedirs(os.path.dirname(self.session_file) or '.', exist_ok=True)
        with open(self.session_file, 'w') as f:
            f.write(token)

    def _req(self, method, path, retry=True, **kw):
        tok = self._token()
        if not tok:
            raise RuntimeError('not logged in — run: python -m app.main --config config/config.yaml --login')
        r = requests.request(method, API + path, cookies={'s': tok}, timeout=15, **kw)
        if r.status_code in (401, 403) and retry:
            self._refresh()
            return self._req(method, path, retry=False, **kw)
        r.raise_for_status()
        return (r.json() or {}).get('data', {})

    def _refresh(self):
        r = requests.post(API + '/login/refresh', cookies={'s': self._token() or ''}, timeout=15)
        r.raise_for_status()
        new = ((r.json() or {}).get('data') or {}).get('user_token')
        if not new:
            raise RuntimeError('session refresh failed — re-run --login')
        self._save(new)
        log.info('eero session refreshed')

    # ── one-time login flow ───────────────────────────────────────────────
    def start_login(self, ident):
        """Request a verification code; stores the pre-verify session token."""
        r = requests.post(API + '/login', json={'login': ident}, timeout=15)
        r.raise_for_status()
        self._save(r.json()['data']['user_token'])

    def verify(self, code):
        r = requests.post(API + '/login/verify', json={'code': str(code).strip()},
                          cookies={'s': self._token() or ''}, timeout=15)
        r.raise_for_status()

    def install_token(self, token):
        """Install an externally captured session token (e.g. from a browser
        session on my.eero.com that authenticated via Amazon)."""
        self._save(token.strip())

    # ── data ──────────────────────────────────────────────────────────────
    def devices(self):
        acct = self._req('GET', '/account')
        out = []
        nets = ((acct.get('networks') or {}).get('data')) or []
        want = (self.cfg.get('network_name') or '').strip().lower()
        if want:
            matched = [n for n in nets if (n.get('name') or '').strip().lower() == want]
            if matched:
                nets = matched
            else:
                log.warning('network_name %r not found on account (networks: %s) — polling all',
                            self.cfg.get('network_name'), [n.get('name') for n in nets])
        for net in nets:
            url = net.get('url') or ''
            path = url[len('/2.2'):] if url.startswith('/2.2') else url
            if not path:
                continue
            for d in self._req('GET', f'{path}/devices') or []:
                out.append(normalize(d, net))
        return out


class EeroAdapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cloud = EeroCloud(cfg)
        self.retries = int(cfg.get('api_retries', 3))

    def fetch(self):
        """Return (devices, api_latency_ms). Retries with exponential backoff."""
        delay = 1
        for attempt in range(self.retries):
            t0 = time.time()
            try:
                return self.cloud.devices(), int((time.time() - t0) * 1000)
            except Exception as ex:
                if attempt == self.retries - 1:
                    raise RuntimeError(f'eero API unavailable/auth failed: {ex}')
                log.warning('eero fetch failed attempt=%d error=%s retry_in=%ds', attempt + 1, ex, delay)
                time.sleep(delay)
                delay *= 2


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


def _rssi(c):
    v = (c.get('connectivity') or {}).get('signal') or c.get('rssi') or c.get('signal_strength')
    if isinstance(v, str):
        m = re.search(r'-?\d+', v)
        return int(m.group()) if m else None
    return v


def normalize(c, net=None):
    src = c.get('source') or {}
    profile = c.get('profile')
    return {
        'mac': (c.get('mac') or c.get('mac_address') or '').lower(),
        'name': c.get('nickname') or c.get('name') or c.get('hostname'),
        'hostname': c.get('hostname'),
        'ip': c.get('ip') or c.get('ip_address') or (c.get('ips') or [None])[0],
        'online': bool(c.get('connected') or c.get('online')),
        'rssi': _rssi(c),
        'gateway': c.get('gateway') or src.get('location') or src.get('display_name'),
        'manufacturer': c.get('manufacturer') or c.get('vendor'),
        'profile': profile.get('name') if isinstance(profile, dict) else profile,
        'network_id': (net or {}).get('id') or ((net or {}).get('url') or '').rsplit('/', 1)[-1] or None,
        'raw': c,
    }
