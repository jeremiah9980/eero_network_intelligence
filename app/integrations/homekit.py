"""HomeKit integration — exposes occupancy sensors via HAP-python.

One occupancy sensor per person (e.g. "Jeremiah Home") plus a combined
"Family Home" sensor. Optional: enable with `homekit.enabled: true` and
`pip install HAP-python[QRCode]` (kept out of core requirements because it
pulls in heavier crypto dependencies).
"""

import logging, threading

log = logging.getLogger('presence.homekit')


class HomeKitBridge:
    def __init__(self, cfg, persons):
        self.cfg = cfg.get('homekit', {}) or {}
        self.persons = persons
        self.driver = None
        self._sensors = {}

    def start(self):
        try:
            from pyhap.accessory import Accessory, Bridge
            from pyhap.accessory_driver import AccessoryDriver
            from pyhap.const import CATEGORY_SENSOR
        except ImportError:
            log.warning('homekit enabled but HAP-python is not installed — run: pip install "HAP-python[QRCode]"')
            return False

        class OccupancySensor(Accessory):
            category = CATEGORY_SENSOR

            def __init__(self, driver, display_name):
                super().__init__(driver, display_name)
                serv = self.add_preload_service('OccupancySensor')
                self.char = serv.configure_char('OccupancyDetected', value=0)

        self.driver = AccessoryDriver(port=int(self.cfg.get('port', 51826)),
                                      persist_file=self.cfg.get('persist_file', './data/homekit.state'))
        bridge = Bridge(self.driver, 'eero Presence')
        for p in [*self.persons, 'Family']:
            sensor = OccupancySensor(self.driver, f'{p} Home')
            self._sensors[p] = sensor
            bridge.add_accessory(sensor)
        self.driver.add_accessory(accessory=bridge)
        threading.Thread(target=self.driver.start, daemon=True, name='homekit').start()
        log.info('homekit bridge started persons=%s pin=%s', self.persons, self.driver.state.pincode.decode())
        return True

    def publish_summary(self, summary):
        if not self.driver:
            return
        persons = summary.get('persons') or {}
        for p, home in persons.items():
            if p in self._sensors:
                self._sensors[p].char.set_value(1 if home else 0)
        if 'Family' in self._sensors:
            self._sensors['Family'].char.set_value(1 if any(persons.values()) else 0)

    def stop(self):
        if self.driver:
            self.driver.stop()
            self.driver = None
