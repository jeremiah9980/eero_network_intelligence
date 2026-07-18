#!/usr/bin/env bash
# Verify the presence platform deployment end to end:
#   1) app health   2) Slack webhook delivery   3) Cloudflare tunnel   4) public dashboard
#
# Usage:
#   ./scripts/verify.sh                                  # local checks only
#   ./scripts/verify.sh https://presence.yourdomain.com  # also verify the public URL
set -uo pipefail

HOST_URL="${1:-}"
BASE="${BASE_URL:-http://localhost:8080}"
FAILURES=0
pass(){ printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES+1)); }
skip(){ printf '  SKIP %s\n' "$1"; }

echo "1) App at $BASE"
HEALTH=$(curl -sf --max-time 5 "$BASE/api/health" || true)
if [ -z "$HEALTH" ]; then
  fail "no response — start it: docker compose up -d  (or: python -m app.main --config config/config.yaml --run)"
else
  if echo "$HEALTH" | grep -q '"status": *"healthy"'; then
    pass "health: healthy"
  else
    fail "app responded but degraded — check poll errors: curl $BASE/api/polls"
  fi
fi

echo "2) Slack webhook"
if echo "$HEALTH" | grep -q '"slack_configured": *true'; then
  pass "webhook configured"
  TEST=$(curl -sf -X POST --max-time 20 "$BASE/api/notify/test" || true)
  if echo "$TEST" | grep -q '"ok": *true'; then
    pass "test notification delivered — check your Slack channel"
  else
    fail "delivery failed: ${TEST:-no response} — webhook URL may be revoked or wrong"
  fi
elif [ -n "$HEALTH" ]; then
  fail "no webhook configured — put SLACK_WEBHOOK_URL=https://hooks.slack.com/... in .env and restart"
fi

echo "3) Cloudflare tunnel"
if command -v docker >/dev/null 2>&1 && docker compose ps cloudflared >/dev/null 2>&1; then
  STATE=$(docker compose ps --format '{{.State}}' cloudflared 2>/dev/null | head -1)
  if [ "$STATE" = "running" ]; then
    pass "cloudflared container running"
  else
    fail "cloudflared container state: ${STATE:-not created} — set CLOUDFLARE_TUNNEL_TOKEN in .env and: docker compose up -d"
  fi
  CONNS=$(docker compose logs cloudflared 2>/dev/null | grep -c "Registered tunnel connection" || true)
  if [ "${CONNS:-0}" -ge 1 ]; then
    pass "$CONNS tunnel connection(s) registered with Cloudflare edge"
  else
    fail "no registered tunnel connections — bad/missing token, or outbound blocked; see: docker compose logs cloudflared"
  fi
else
  skip "docker cloudflared service not found (bare metal? run: cloudflared tunnel run --token \$CLOUDFLARE_TUNNEL_TOKEN)"
fi

echo "4) Public dashboard"
if [ -n "$HOST_URL" ]; then
  PUB=$(curl -sf --max-time 10 "$HOST_URL/api/health" || true)
  if echo "$PUB" | grep -q '"ok"'; then
    pass "dashboard reachable at $HOST_URL"
  else
    fail "no healthy response from $HOST_URL — in Zero Trust, the tunnel's Public Hostname must point to HTTP://eero-intel:8080"
  fi
else
  skip "no public URL given — rerun as: ./scripts/verify.sh https://presence.yourdomain.com"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$FAILURES check(s) failed."
  exit 1
fi
