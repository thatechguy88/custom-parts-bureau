# The Custom Parts Bureau — System Architecture Plan

## Overview

A 3D printing business run by an AI agent. Customers upload STL files through a web interface. The agent (Hermes in NemoClaw) reviews each job, makes pricing decisions, monitors system health, and reports to the operator via Telegram.

**Core principle:** The agent is a participant, not a script runner. It wakes up when events happen, not on a timer.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    CUSTOMER                              │
│            (uploads STL via web UI)                      │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              FLASK APP (Local/Docker)                    │
│         Flask app running on port 5001                   │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ Web UI      │  │ REST API    │  │ SQLite DB       │ │
│  │ (human)     │  │ (AI agent)  │  │ (shared state)  │ │
│  └─────────────┘  └──────┬──────┘  └─────────────────┘ │
│                          │                               │
│  ┌───────────────────────▼─────────────────────────┐   │
│  │ Webhook Trigger                                 │   │
│  │ On job.created → POST to NemoClaw Tunnel URL    │   │
│  └─────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │ 
┌──────────────────────▼──────────────────────────────────┐
│              PUBLIC INTERNET TUNNELS                     │
│  Flask App exposed via Cloudflared Quick Tunnel          │
│  (e.g., https://app.trycloudflare.com)                   │
│                                                          │
│  NemoClaw exposed via `nemoclaw tunnel start`            │
│  (e.g., https://nemoclaw.trycloudflare.com)              │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              NEMOCLAW SANDBOX                            │
│         Hermes Agent + Managed Inference (local)         │
│                                                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │ Webhook Receiver                                │   │
│  │ Path: /webhooks/job-review                      │   │
│  │                                                 │   │
│  │ Agent Tools & Constraints:                      │   │
│  │ - curl app API (via Flask's Cloudflare URL)     │   │
│  │ - Network Egress Policy: whitelist Flask URL    │   │
│  │ - Filesystem: Read/Write ONLY in `/workspace`   │   │
│  │ - Telegram (report to operator)                 │   │
│  └─────────────────────────────────────────────────┘   │
│                                                          │
│  Agent Responsibilities:                                 │
│  1. Review each new quote (QA)                          │
│  2. Override decision if needed (Nemotron reasoning)    │
│  3. Monitor server health                               │
│  4. Report summary to Telegram                          │
└─────────────────────────────────────────────────────────┘
```

---

## Networking & Security Setup

NemoClaw uses a strict "deny-by-default" egress proxy and blocks local Server-Side Request Forgery (SSRF). To allow the Flask app and the Agent to communicate, we use public tunnels to bypass the local Docker network completely.

### 1. Run the Flask App

```bash
# Run the app locally or in Docker, exposed on port 5001
docker run -d --name cpb-app \
  -p 5001:5001 \
  -v ~/custom-parts-bureau:/app \
  -w /app \
  python:3.11-slim \
  sh -c 'pip install -q flask trimesh numpy scipy stripe requests && python3 app.py'
```

### 2. Expose the Flask App (Cloudflared)

Start a Quick Tunnel to expose the Flask app to the internet.

```bash
cloudflared tunnel --url http://localhost:5001
```
*Note the generated public URL (e.g., `https://flask-app.trycloudflare.com`).*

### 3. Expose NemoClaw Webhooks (Cloudflared)

Inside NemoClaw, start the inbound tunnel so Hermes can receive webhooks from Flask.

```bash
nemoclaw tunnel start
```
*Note the generated NemoClaw public URL (e.g., `https://nemoclaw-agent.trycloudflare.com`).*

### 4. NemoClaw Network Policy (Egress)

The NemoClaw proxy will block the agent from reaching the Flask app unless whitelisted. 
1. Run NemoClaw in `monitor-only` mode initially to capture required endpoints.
2. Create a policy file `cpb-policy.yaml`:
```yaml
network_policies:
  flask_app:
    name: "CPB Flask App"
    endpoints:
      - host: "flask-app.trycloudflare.com" # Replace with your Cloudflare URL
        port: 443
        protocol: rest
        enforcement: enforce
    rules:
      - allow: { method: GET, path: "/**" }
      - allow: { method: POST, path: "/**" }
```
3. Apply it to the sandbox: `nemoclaw policy-add --from-file cpb-policy.yaml`

---

## Hermes Webhook Setup

### 1. Create webhook subscription (inside NemoClaw sandbox)

```bash
hermes webhook subscribe job-review \
  --prompt "New job uploaded. Job ID: {job_id}. Filename: {filename}. Email: {email}. 

Review this job:
1. Fetch the job details from https://flask-app.trycloudflare.com/api/quote/{job_id}
2. Verify the analysis makes sense (check geometry, costs, decision)
3. If the decision seems wrong, override it and explain why
4. If everything looks good, confirm it
5. Update the job status via POST https://flask-app.trycloudflare.com/api/agent-decide/{job_id}
6. Report your decision to Telegram

CRITICAL INSTRUCTIONS:
- Be concise. Focus on whether the decision is justified by the data.
- If you need to write any scripts or temporary files, you MUST use the /workspace directory.
- Use inference.local for any required Nemotron reasoning." \
  --events "job.created" \
  --deliver telegram \
  --description "Reviews each new quote and validates the AI decision"
```

### 2. Flask app triggers webhook on job creation

In `app.py`, after creating a job:

```python
import requests

def trigger_agent_review(job_id, filename, email):
    """Notify the agent about a new job."""
    try:
        # POST to NemoClaw's PUBLIC Cloudflare tunnel URL
        NEMOCLAW_WEBHOOK_URL = "https://nemoclaw-agent.trycloudflare.com/webhooks/job-review"
        
        requests.post(
            NEMOCLAW_WEBHOOK_URL,
            json={
                "job_id": job_id,
                "filename": filename,
                "email": email,
                "event": "job.created"
            },
            timeout=5
        )
    except Exception as e:
        print(f"Agent notification failed: {e}")
        # Non-critical — job still gets created
```

### 3. New API endpoint for agent decisions

```python
@app.route('/api/agent-decide/<job_id>', methods=['POST'])
def agent_decide(job_id):
    """Agent updates job decision after review."""
    data = request.json
    conn = get_db()
    conn.execute('''UPDATE jobs SET 
        decision = ?, 
        nemotron_explanation = ?,
        updated_at = ? 
        WHERE id = ?''',
        (data['decision'], data['reasoning'], 
         datetime.now().isoformat(), job_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})
```

---

## Agent Configuration

### Nemo's Role (event-driven)

**Trigger 1: New Job Review**
- Event: `job.created` (webhook from Flask app)
- Action: Fetch job → verify analysis → confirm or override → report

**Trigger 2: Health Check** (cron, every 5 min — this is fine for monitoring)
- Check disk, API response, database integrity
- Report anomalies to Telegram

**Trigger 3: Payment Confirmation** (webhook from Stripe)
- Event: `payment.received` (webhook from Flask app)
- Action: Update status → notify operator "ready to print"

---

## Implementation Steps

### Phase 1: Tunnel Setup & Security
1. Run app container (exposed on `5001`)
2. Start Flask app Cloudflared Quick Tunnel
3. Start NemoClaw inbound tunnel
4. Apply NemoClaw egress policy (`cpb-policy.yaml`) for the Flask app's tunnel URL

### Phase 2: Webhook Integration
1. Update `trigger_agent_review()` in `app.py` to use NemoClaw's tunnel URL
2. Add `/api/agent-decide/<job_id>` endpoint
3. Create webhook subscription in NemoClaw
4. Test: upload STL → verify agent receives webhook via tunnel → verify agent reaches Flask app via proxy

### Phase 3: Agent Configuration
1. Write agent prompt with `/workspace` constraints
2. Configure health check cron
3. Test: agent reviews a job → decision updated → Telegram notification sent

---

## Testing Checklist

- [ ] App container starts and serves landing page
- [ ] Cloudflared quick tunnel exposes Flask app
- [ ] NemoClaw tunnel exposes webhook receiver
- [ ] Network policy applied and `monitor-only` mode tested
- [ ] STL upload creates job in database
- [ ] Webhook fires on job creation and reaches agent
- [ ] Agent successfully fetches job details via Flask tunnel URL
- [ ] Agent decision updates the database
- [ ] Agent reports to Telegram
- [ ] Health check cron runs and reports

---

## Key Commands

```bash
# Start Flask App and Tunnel
docker run -d --name cpb-app -p 5001:5001 -v ~/custom-parts-bureau:/app -w /app python:3.11-slim sh -c 'pip install -q flask trimesh numpy scipy stripe requests && python3 app.py'
cloudflared tunnel --url http://localhost:5001

# Inside NemoClaw — start tunnel and apply policy
nemoclaw tunnel start
nemoclaw policy-add --from-file cpb-policy.yaml

# Set up webhook
hermes webhook subscribe job-review \
  --prompt "New job: {job_id} ({filename}). Review at https://flask-app.trycloudflare.com/api/quote/{job_id}. Update at https://flask-app.trycloudflare.com/api/agent-decide/{job_id}. Report to Telegram. MUST ONLY USE /workspace DIRECTORY." \
  --events "job.created" \
  --deliver telegram

# Test webhook
hermes webhook test job-review --payload '{"job_id":"test123","filename":"bracket.stl","email":"test@test.com"}'
```
