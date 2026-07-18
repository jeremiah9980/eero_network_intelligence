# eero Presence Intelligence Platform

A production-oriented home presence intelligence platform for eero networks. It polls the eero cloud API on a schedule, detects arrivals and departures with debounced state transitions, keeps a complete SQLite history, and exposes presence via a real-time dashboard, REST API, WebSockets, Slack, Home Assistant (MQTT), and HomeKit.

## How it works

```
             Scheduler (every 5 min)
                      │
                      ▼
               eero Cloud API          ← retry + exponential backoff
                      │
                      ▼
              Presence Engine          ← framework-independent (app/engine.py)
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
     SQLite       WebSocket       Slack
        │             │
        ▼             ▼
    REST API      Dashboard
        │
        ▼
  Home Assistant (MQTT) · HomeKit
```

**Events:** `ARRIVED` / `LEFT` for person devices (config entries with `person:`), `ONLINE` / `OFFLINE` for everything else, plus `ROAMED` (gateway change) and `RENAMED`. Departures require **two consecutive missed polls** (`offline_confirmation_polls`) before firing, which suppresses false departures. Never-before-seen devices raise a security **warning** alert.

**History:** `devices` (current state incl. gateway/RSSI/online-since), `presence_events` (every transition), `poll_history` (duration, API latency, success, device count), `node_history` (roaming + signal samples), `alerts` (graded security alerts).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
cp .env.example .env            # put your Slack webhook URL in .env
mkdir -p data
python -m app.main --config config/config.yaml --run
```

Open http://localhost:8080

### Demo without eero credentials

Set `adapter: fake` in `config.yaml` and create `data/fake_devices.json` (a JSON array of devices with `mac`, `nickname`, `connected`, `ip`, `gateway`, `rssi`). Edit the file between polls to simulate arrivals/departures.

## Slack notifications

Create a Slack **Incoming Webhook** and put it in `.env` (gitignored — never commit webhook URLs):

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Notifications are Block Kit cards, sent **only on real state changes** (deduplicated):

- 🟢 **ARRIVED** / 🔴 **LEFT** for person devices — with MAC, IP, gateway, RSSI, and session duration on departure
- ⚠️ unknown-device warnings (`notify_on_new_device`)
- 🏠 a **daily summary** at `notifications.daily_summary_time` — who's home, arrivals/departures today

Verify wiring with the **Test Slack Alert** button or `curl -X POST localhost:8080/api/notify/test`.

## Home Assistant (MQTT)

Set `mqtt.enabled: true` and point it at your broker. Entities appear via MQTT discovery:

- `device_tracker.<person>` (`home` / `away`, source_type router)
- `sensor.people_home`, `sensor.active_devices`
- raw events on `home/presence/events`

## HomeKit

Set `homekit.enabled: true` and `pip install "HAP-python[QRCode]"`. Exposes an occupancy sensor per person ("Jeremiah Home", …) plus "Family Home". The pairing PIN is printed in the logs on startup.

## API

```text
GET  /api/presence        # who's home: persons + online devices with durations
GET  /api/devices         # inventory with online/person/watched flags
GET  /api/device/{mac}    # history, sessions, analytics, signal + IP history
GET  /api/events          # every transition
GET  /api/alerts          # graded security alerts
GET  /api/polls           # poll diagnostics (duration, latency, errors)
GET  /api/analytics       # daily arrivals/departures, most active, heatmap
GET  /api/dashboard       # combined dashboard payload
GET  /api/health          # healthy/degraded + last poll age
POST /api/poll            # poll now
POST /api/notify/test     # send a test Slack notification
POST /api/notify/summary  # send the daily summary now
WS   /ws                  # live updates
```

## Reliability

- API + Slack retries with exponential backoff
- Notification deduplication window (one alert per device/event per poll interval)
- Watchdog job re-triggers polling if the scheduler stalls (`/api/health` reports `degraded`)
- Structured logging (`LOG_LEVEL=DEBUG` for verbose)
- Graceful shutdown of scheduler, MQTT, and HomeKit

## Docker

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env   # add SLACK_WEBHOOK_URL
docker compose up --build -d
```

## Cloudflare Tunnel

`cloudflared tunnel --url http://localhost:8080`, or set `CLOUDFLARE_TUNNEL_TOKEN` in `.env` and the bundled `cloudflared` service publishes the dashboard behind your named tunnel.

## Security notes

Do not commit: `config/config.yaml`, `.env` / webhook URLs, session cookies, DSAR exports, or SQLite databases. This project intentionally ships only `config.example.yaml` and `.env.example`.
