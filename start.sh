#!/usr/bin/env bash
# (Re)start FomoFinder on 127.0.0.1:8787. TUNNEL=1 also tries a cloudflared quick tunnel (blocked from the agent box: egress only allows HTTP/S).
cd "$(dirname "$0")"
pkill -f "uvicorn app:app --host 127.0.0.1 --port 8787"; pkill -f "cloudflared tunnel --no-autoupdate"; sleep 1
if [ -x ./venv/bin/uvicorn ]; then
  UVICORN=./venv/bin/uvicorn
elif [ -x ../venv/bin/uvicorn ]; then
  UVICORN=../venv/bin/uvicorn
else
  UVICORN=uvicorn
fi
nohup "$UVICORN" app:app --host 127.0.0.1 --port 8787 --proxy-headers > /tmp/fomofinder.log 2>&1 &
echo "uvicorn pid $!"
if [ "$TUNNEL" = 1 ]; then
  nohup "${CLOUDFLARED:-$HOME/.local/bin/cloudflared}" tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8787 > /tmp/fomofinder-tunnel.log 2>&1 &
  echo "cloudflared pid $!"
  for i in $(seq 40); do U=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/fomofinder-tunnel.log | head -1); [ -n "$U" ] && break; sleep 1; done
  echo "$U"
fi
