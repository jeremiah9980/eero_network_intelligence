# eero Presence Security Dashboard

A cloud/API-oriented eero network presence **security** app: live device tracking, graded security alerts, Slack notifications, and a real-time dashboard.

## Features

- Live device presence tracking (enter / leave / discovered)
- **Security alert engine** — every presence event is classified `info` / `notice` / `warning` / `critical`; an unknown device joining the network raises a `warning` alert
- **Slack notifications** via Incoming Webhook (Block Kit, severity color-coded), plus Discord / Teams / Pushcut generic webhooks
- Real-time security dashboard: stat tiles, 24h activity chart, alerts feed, device inventory with online/watched status, live WebSocket updates
- One-click **Test Slack Alert** button to verify webhook wiring end to end
- Historical SQLite database (devices, events, alerts, snapshots)
- REST API + WebSocket feed
- Docker Compose deployment, Cloudflare Tunnel friendly
- DSAR import support for your eero personal data export

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

## Slack notifications

Create a Slack **Incoming Webhook** and put it in `.env` (gitignored — never commit webhook URLs):

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

The app picks it up automatically (locally and via Docker Compose). Verify with the **Test Slack Alert** button on the dashboard, or:

```bash
curl -X POST http://localhost:8080/api/notify/test
```

What gets sent to Slack:

- watched devices (from `config.yaml` `devices:` list) entering or leaving the network
- any never-before-seen device joining the network (`notify_on_new_device: true`)

## Docker

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env   # add SLACK_WEBHOOK_URL
docker compose up --build -d
```

## Cloudflare Tunnel

Install `cloudflared`, then run:

```bash
cloudflared tunnel --url http://localhost:8080
```

Or set `CLOUDFLARE_TUNNEL_TOKEN` in `.env` and the bundled `cloudflared` service will publish the dashboard behind your named tunnel.

## API

```text
GET  /api/health
GET  /api/stats          # tiles: online/watched/alerts/events + per-hour activity
GET  /api/devices        # inventory with online + watched flags
GET  /api/events
GET  /api/alerts         # graded security alerts
POST /api/poll           # poll eero now
POST /api/notify/test    # send a test Slack notification
WS   /ws                 # live updates
```

## Security notes

Do not commit:

- `config/config.yaml`
- `.env` / webhook URLs
- session cookies
- DSAR exports
- SQLite database files

This project intentionally ships only with `config.example.yaml` and `.env.example`.

## Presence strategy

The best identifier order is:

1. eero device/client ID
2. MAC address
3. stable hostname/nickname

Names like `iPhone` are ambiguous, so import DSAR data first when possible.
