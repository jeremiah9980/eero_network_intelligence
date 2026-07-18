import json, os, time
# This adapter intentionally isolates the unofficial eero library so the app keeps running if eero changes auth.
class EeroAdapter:
    def __init__(self, cfg): self.cfg=cfg
    def fetch_devices(self):
        try:
            from eero import Eero
            e=Eero()
            token=e.login(self.cfg['auth_source'])
            # Most community libs differ here. Keep raw export hook available.
            nets=e.get_networks() if hasattr(e,'get_networks') else []
            devices=[]
            for net in nets:
                clients=e.get_clients(net.get('url') or net.get('id')) if hasattr(e,'get_clients') else []
                for c in clients: devices.append(normalize(c, net))
            return devices
        except Exception as ex:
            raise RuntimeError(f'eero API unavailable/auth failed: {ex}')
def normalize(c, net=None):
    return {'mac':(c.get('mac') or c.get('mac_address') or '').lower(), 'name':c.get('nickname') or c.get('name') or c.get('hostname'), 'ip':c.get('ip') or c.get('ip_address'), 'online':bool(c.get('connected') or c.get('online')), 'signal':c.get('signal_strength'), 'profile':c.get('profile'), 'network_id':(net or {}).get('id'), 'raw':c}
