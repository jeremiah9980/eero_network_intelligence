"""MQTT / Home Assistant integration.

Publishes person presence to `<base_topic>/<person>` as `home` / `away` and
exposes Home Assistant MQTT-discovery entities:

- device_tracker.<person>          (source_type: router)
- sensor.people_home
- sensor.active_devices

Enabled with `mqtt.enabled: true`; requires paho-mqtt (in requirements).
"""

import json, logging, re

log = logging.getLogger('presence.mqtt')


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


class MqttPublisher:
    def __init__(self, cfg, persons):
        m = cfg.get('mqtt', {}) or {}
        self.cfg = m
        self.persons = persons
        self.base = m.get('base_topic', 'home/presence')
        self.disc = m.get('discovery_prefix', 'homeassistant')
        self.client = None

    def start(self):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='eero-presence')
        if self.cfg.get('username'):
            self.client.username_pw_set(self.cfg['username'], self.cfg.get('password') or None)
        self.client.connect(self.cfg.get('host', 'localhost'), int(self.cfg.get('port', 1883)), 60)
        self.client.loop_start()
        self._publish_discovery()
        log.info('mqtt connected host=%s', self.cfg.get('host', 'localhost'))

    def _pub(self, topic, payload, retain=True):
        if self.client:
            self.client.publish(topic, payload if isinstance(payload, str) else json.dumps(payload), retain=retain)

    def _publish_discovery(self):
        device = {'identifiers': ['eero_presence'], 'name': 'eero Presence Platform',
                  'manufacturer': 'eero-presence', 'model': 'presence-engine'}
        for p in self.persons:
            s = _slug(p)
            self._pub(f'{self.disc}/device_tracker/eero_presence_{s}/config', {
                'name': p, 'unique_id': f'eero_presence_{s}',
                'state_topic': f'{self.base}/{s}',
                'payload_home': 'home', 'payload_not_home': 'away',
                'source_type': 'router', 'device': device,
            })
        for key, name, unit in (('people_home', 'People Home', 'people'),
                                ('active_devices', 'Active Devices', 'devices')):
            self._pub(f'{self.disc}/sensor/eero_presence_{key}/config', {
                'name': name, 'unique_id': f'eero_presence_{key}',
                'state_topic': f'{self.base}/{key}',
                'unit_of_measurement': unit, 'state_class': 'measurement', 'device': device,
            })

    def publish_summary(self, summary):
        for person, home in (summary.get('persons') or {}).items():
            self._pub(f'{self.base}/{_slug(person)}', 'home' if home else 'away')
        self._pub(f'{self.base}/people_home', str(summary.get('people_home', 0)))
        self._pub(f'{self.base}/active_devices', str(summary.get('devices_online', 0)))

    def handle_event(self, ev):
        self._pub(f'{self.base}/events', ev, retain=False)

    def stop(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None
