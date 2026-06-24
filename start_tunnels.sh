#!/bin/bash
set -e

echo "=========================================================="
echo "Starting E2E Tunnels for Custom Parts Bureau"
echo "=========================================================="

# 1. Apply Ngrok policy to NemoClaw
echo "Applying ngrok network policy to NemoClaw..."
if [ -f "ngrok-policy.yaml" ]; then
    SANDBOX_NAME=$(nemoclaw list --json | grep '"defaultSandbox"' | head -n 1 | cut -d'"' -f4)
    if [ -z "$SANDBOX_NAME" ]; then
        SANDBOX_NAME="hackathon01"
    fi
    echo "Targeting sandbox: $SANDBOX_NAME"
    nemoclaw "$SANDBOX_NAME" policy-add --from-file ngrok-policy.yaml
else
    echo "Warning: ngrok-policy.yaml not found!"
fi

# 2. Start Ngrok
echo "Starting ngrok on port 5001..."
# Kill any existing ngrok
killall ngrok 2>/dev/null || true
nohup ngrok http 5001 > ngrok.log 2>&1 &

echo "Waiting for ngrok to initialize..."
sleep 3

NGROK_URL=$(curl -s localhost:4040/api/tunnels | grep -o 'https://[a-zA-Z0-9.-]*\.ngrok-free\.app' | head -n 1)

if [ -z "$NGROK_URL" ]; then
    echo "Error: Could not extract Ngrok URL. Is ngrok installed and configured?"
    exit 1
fi
echo "Ngrok URL: $NGROK_URL"

# 3. Start NemoClaw Tunnel
echo "Starting NemoClaw tunnel..."
# Kill any existing cloudflared tunnel spawned by nemoclaw
killall cloudflared 2>/dev/null || true

# Get the sandbox's actual dashboard port
DASHBOARD_PORT=$(nemoclaw list --json | grep -A 20 '"name": "'"$SANDBOX_NAME"'"' | grep '"dashboardPort"' | head -n 1 | grep -o '[0-9]\+' || true)
if [ -z "$DASHBOARD_PORT" ]; then
    DASHBOARD_PORT=8642
fi
echo "Using dashboard port: $DASHBOARD_PORT"

nohup cloudflared tunnel --url http://localhost:$DASHBOARD_PORT > nemoclaw-tunnel.log 2>&1 &

echo "Waiting for NemoClaw tunnel to initialize..."
sleep 5

NEMOCLAW_TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' nemoclaw-tunnel.log | tail -n 1 || true)

if [ -z "$NEMOCLAW_TUNNEL_URL" ]; then
    echo "Error: Could not extract Cloudflared URL from NemoClaw tunnel logs."
    exit 1
fi
NEMOCLAW_WEBHOOK_URL="${NEMOCLAW_TUNNEL_URL}/webhooks/job-review"
echo "NemoClaw Webhook URL: $NEMOCLAW_WEBHOOK_URL"

# 4. Update .env file
echo "Updating .env file with tunnel URLs..."
if [ ! -f ".env" ]; then
    touch .env
fi

# Remove old entries
sed -i.bak '/^NGROK_URL=/d' .env
sed -i.bak '/^NEMOCLAW_WEBHOOK_URL=/d' .env

# Add new entries
echo "NGROK_URL=$NGROK_URL" >> .env
echo "NEMOCLAW_WEBHOOK_URL=$NEMOCLAW_WEBHOOK_URL" >> .env

echo "=========================================================="
echo "TUNNELS RUNNING!"
echo "NGROK_URL=$NGROK_URL"
echo "NEMOCLAW_WEBHOOK_URL=$NEMOCLAW_WEBHOOK_URL"
echo "The .env file has been updated."
echo "You can now run ./start.sh to launch the Flask app."
echo "=========================================================="
