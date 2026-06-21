#!/bin/bash
set -e

echo "Starting Docker Compose environment..."
docker compose up -d

echo "Waiting for Cloudflared tunnel to establish..."
sleep 5

FLASK_TUNNEL_URL=$(docker logs cpb-tunnel 2>&1 | grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' | tail -n 1)

if [ -z "$FLASK_TUNNEL_URL" ]; then
    echo "Error: Could not extract Cloudflared URL from logs. Check 'docker logs cpb-tunnel'."
    exit 1
fi

echo "Flask app exposed at: $FLASK_TUNNEL_URL"

echo "Generating cpb-policy.yaml..."
cat <<EOF > cpb-policy.yaml
network_policies:
  flask_app:
    name: "CPB Flask App"
    endpoints:
      - host: "${FLASK_TUNNEL_URL#https://}"
        port: 443
        protocol: rest
        enforcement: enforce
    rules:
      - allow: { method: GET, path: "/**" }
      - allow: { method: POST, path: "/**" }
EOF

echo "Applying network policy to NemoClaw..."
nemoclaw policy-add --from-file cpb-policy.yaml

echo "Starting NemoClaw inbound tunnel (running in background)..."
# Assuming tunnel start might block, we run it in background
nohup nemoclaw tunnel start > nemoclaw-tunnel.log 2>&1 &
sleep 5

echo ""
echo "=========================================================="
echo "SETUP COMPLETE!"
echo "Flask App URL: $FLASK_TUNNEL_URL"
echo ""
echo "Next Steps:"
echo "1. Run 'nemoclaw status' or check 'nemoclaw-tunnel.log' to get your NemoClaw Webhook URL."
echo "2. Add that URL to your .env file as: NEMOCLAW_WEBHOOK_URL=<url>/webhooks/job-review"
echo "3. Restart the flask app: docker restart cpb-app"
echo "4. Copy the agent prompt from agent_setup_prompt.md and paste it to Hermes."
echo "=========================================================="
