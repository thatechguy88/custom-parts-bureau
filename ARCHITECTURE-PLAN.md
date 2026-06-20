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
│              APP CONTAINER (Docker)                      │
│         Flask app on port 5001                           │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ Web UI      │  │ REST API    │  │ SQLite DB       │ │
│  │ (human)     │  │ (AI agent)  │  │ (shared state)  │ │
│  └─────────────┘  └──────┬──────┘  └─────────────────┘ │
│                          │                               │
│  ┌───────────────────────▼─────────────────────────┐   │
│  │ Webhook Trigger                                 │   │
│  │ On job.created → POST to Hermes webhook         │   │
│  └─────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP (Docker network)
┌──────────────────────▼──────────────────────────────────┐
│              NEMOCLAW CONTAINER                          │
│         Hermes Agent + Nemotron 3 Ultra                  │
│                                                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │ Webhook: /webhooks/job-review                   │   │
│  │ Prompt: "New job {job_id}: {filename}..."       │   │
│  │                                                  │   │
│  │ Agent Tools:                                     │   │
│  │ - curl app API (read jobs, update status)        │   │
│  │ - SQLite query (review data)                     │   │
│  │ - Telegram (report to operator)                  │   │
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

## Docker Setup

### Network

```bash
docker network create cpb-network
```

### App Container

```bash
docker run -d --name cpb-app \
  --network cpb-network \
  -p 5001:5001 \
  -v ~/custom-parts-bureau:/app \
  -w /app \
  python:3.11-slim \
  sh -c 'pip install -q flask trimesh numpy scipy stripe requests && python3 app.py'
```

**Key:** The app runs on the Docker network. NemoClaw can reach it at `http://cpb-app:5001`.

### NemoClaw Container (existing)

Already running as `openshell-hackathon01-{uuid}`. Connect it to the network:

```bash
docker network connect cpb-network openshell-hackathon01-{uuid}
```

Now NemoClaw can reach the app at `http://cpb-app:5001`.

---

## Hermes Webhook Setup

### 1. Create webhook subscription (inside NemoClaw sandbox)

```bash
hermes webhook subscribe job-review \
  --prompt "New job uploaded. Job ID: {job_id}. Filename: {filename}. Email: {email}. 

Review this job:
1. Fetch the job details from http://cpb-app:5001/api/quote/{job_id}
2. Verify the analysis makes sense (check geometry, costs, decision)
3. If the decision seems wrong, override it and explain why
4. If everything looks good, confirm it
5. Update the job status via POST http://cpb-app:5001/api/agent-decide/{job_id}
6. Report your decision to Telegram

Be concise. Focus on whether the decision is justified by the data." \
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
        requests.post(
            "http://cpb-app:5001/webhooks/job-review",  # internal
            # Actually: POST to NemoClaw's hermes webhook
            "http://nemoclaw:8788/webhooks/job-review",
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

### Agent Prompt (for job review webhook)

```
You are the Quality Assurance Agent for The Custom Parts Bureau.

A new job has been uploaded. Review it:

1. Fetch job details: curl http://cpb-app:5001/api/quote/{job_id}
2. Check: Is the decision (ACCEPT/REJECT/CONDITIONAL) justified by the confidence score?
   - ≥70: should be ACCEPT
   - 40-69: should be CONDITIONAL
   - <40: should be REJECT
3. Check: Are the cost estimates reasonable?
   - Material: $0.01-$50
   - Machine: $0.10-$100
   - Total: $0.50-$200
4. Check: Does the Nemotron reasoning match the actual geometry data?
5. If anything looks wrong, override the decision with explanation

Update the job: curl -X POST http://cpb-app:5001/api/agent-decide/{job_id} \
  -d '{"decision": "...", "reasoning": "..."}'

Report to Telegram: brief summary of what you found.
```

---

## File Structure

```
custom-parts-bureau/
├── app.py                    # Flask backend (webhook triggers added)
├── models.py                 # SQLite models
├── adapters.py               # Pipeline adapters
├── stl_analyzer.py           # Geometry analysis
├── cost_estimator.py         # Cost estimation
├── quote_generator.py        # Quote generation
├── nemotron_reasoning.py     # Nemotron 3 Ultra integration
├── stripe_integration.py     # Stripe checkout
├── dashboard.html            # P&L dashboard
├── templates/
│   ├── base.html
│   ├── landing.html
│   ├── quote.html
│   └── status.html
├── test_stl/                 # Sample STL files
├── .env                      # API keys (gitignored)
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Implementation Steps

### Phase 1: Docker Setup
1. Create Docker network (`cpb-network`)
2. Run app container with volume mount
3. Connect NemoClaw to network
4. Verify connectivity (`curl http://cpb-app:5001/` from NemoClaw)

### Phase 2: Webhook Integration
1. Add `trigger_agent_review()` to `app.py` (POST to NemoClaw webhook)
2. Add `/api/agent-decide/<job_id>` endpoint
3. Create webhook subscription in NemoClaw (`hermes webhook subscribe`)
4. Test: upload STL → verify agent receives webhook → verify agent responds

### Phase 3: Agent Configuration
1. Write agent prompt for job review
2. Configure health check cron (every 5 min)
3. Test: agent reviews a job → decision updated → Telegram notification sent

### Phase 4: Polish
1. Dashboard shows agent decisions (QA pass/fail)
2. Status page shows agent review timestamp
3. Telegram notifications are clean and informative

---

## Testing Checklist

- [ ] App container starts and serves landing page
- [ ] NemoClaw can reach app at `http://cpb-app:5001`
- [ ] STL upload creates job in database
- [ ] Webhook fires on job creation
- [ ] Agent receives webhook and reviews job
- [ ] Agent decision updates the database
- [ ] Agent reports to Telegram
- [ ] Health check cron runs and reports
- [ ] Dashboard shows real-time status
- [ ] Stripe checkout works end-to-end

---

## Key Commands

```bash
# Start everything
docker network create cpb-network
docker run -d --name cpb-app --network cpb-network -p 5001:5001 -v ~/custom-parts-bureau:/app -w /app python:3.11-slim sh -c 'pip install -q flask trimesh numpy scipy stripe requests && python3 app.py'
docker network connect cpb-network openshell-hackathon01-{uuid}

# Inside NemoClaw — set up webhook
hermes webhook subscribe job-review \
  --prompt "New job: {job_id} ({filename}). Review at http://cpb-app:5001/api/quote/{job_id}. Update at http://cpb-app:5001/api/agent-decide/{job_id}. Report to Telegram." \
  --events "job.created" \
  --deliver telegram

# Test webhook
hermes webhook test job-review --payload '{"job_id":"test123","filename":"bracket.stl","email":"test@test.com"}'

# Check agent logs
hermes logs --follow
```
