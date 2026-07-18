# eero Network Intelligence

Full presence-monitoring app for eero networks: dashboard, SQLite history, REST API, WebSocket feed, notifications, Docker, Cloudflare Tunnel support, and DSAR import.

## Quick start
```bash
cd eero_network_intelligence
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
mkdir -p data
cp /path/to/personal_data.zip data/personal_data.zip
python -m app.main --config config/config.yaml --import-dsar data/personal_data.zip
python -m app.main --config config/config.yaml --run
```
Open http://localhost:8080

## Docker
```bash
cp config/config.example.yaml config/config.yaml
mkdir -p data
cp personal_data.zip data/personal_data.zip
docker compose up --build
```

## Cloudflare Tunnel
Create a Cloudflare Tunnel to `http://eero-intel:8080`, then put the token in `.env`:
```bash
CLOUDFLARE_TUNNEL_TOKEN=your-token
```
Run:
```bash
docker compose up -d
```

## APIs
- `GET /api/devices`
- `GET /api/events`
- `POST /api/poll`
- `WS /ws`

## Notes
The live eero API is unofficial/reverse-engineered, so this app isolates it in `app/eero_api.py`. Your DSAR export is imported into SQLite and used as a reliable historical inventory even if live auth changes.
