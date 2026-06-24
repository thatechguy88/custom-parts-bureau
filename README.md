# 🔧 The Custom Parts Bureau — STL Analysis, Quoting, and P&L System

> **Submission for the Hermes Agent Accelerated Business Hackathon** (NVIDIA / Stripe / Nous Research)

An AI-powered 3D printing business that analyzes uploaded STL files, estimates manufacturing costs, dynamically adjusts margins based on risk, and makes accept/reject decisions with natural language reasoning powered by Nemotron 3 Ultra. 

The centerpiece is the **"refusal moment"** — automatically rejecting unprintable or structurally flawed models (such as thin walls or extreme overhangs) with physics-grounded explanation text, and handling Stripe Checkouts dynamically.

---

## 🚀 Quick Start (Web Server)

The application is structured to run locally on your host or directly inside a Docker container.

### Method 1: Local Virtual Environment
```bash
# 1. Activate the environment and install dependencies
source venv/bin/activate
pip install -r requirements.txt

# 2. Start the Flask application
python3 app.py
```
*Access the interface at: **http://localhost:5001***

### Method 2: Inside Docker (Port 8080)
If you are running in a dockerized environment, bind the workspace and map the ports:
```bash
# 1. Build and run the python environment container
docker run -d --name project-server \
  -v "$(pwd)":/project \
  -p 8080:8080 \
  -e PORT=8080 \
  -e HOST=0.0.0.0 \
  -w /project \
  python:3.11-slim \
  sh -c "pip install -r requirements.txt && python3 app.py"
```
*Access the interface at: **http://localhost:8080***

---

## 🗺️ Interface Map

| URL | What It Serves |
|-----|----------------|
| `/` | **Landing Page**: Drag-and-drop STL upload zone & client email capture |
| `/quote/<job_id>` | **Quoting Terminal**: Displays spinning 3D loading wireframe, itemized costs, and Nemotron reasoning |
| `/status/<job_id>` | **Order Timeline**: Pulse-glowing status tracking (Uploaded ➔ Analyzed ➔ Paid ➔ Printing ➔ Done) |
| `/dashboard` | **Operator Panel**: Real-time financial P&L metrics, Kanban queues, and agent chat logs |

---

## 🛠️ Architecture

```
custom-parts-bureau/
├── app.py                  ➔ Flask Application (API routers & background threads)
├── models.py               ➔ Database Layer (SQLite schema & Job CRUD operations)
├── adapters.py             ➔ Mismatch Bridges (Adapts quote output to Stripe & Dashboard)
├── db_sync.py              ➔ Host-side Daemon syncing the local database to the sandbox
├── templates/              ➔ Monospace dark-CRT style views (Jinja2)
│   ├── base.html           ➔ Shared visual shell (scanlines, custom fonts)
│   ├── landing.html        ➔ Drag/drop upload page
│   ├── quote.html          ➔ 3-stage loading and quote cards
│   └── status.html         ➔ Progress bar order status
├── stl_analyzer.py         ➔ Ray-cast wall thickness, overhang, & watertight checks
├── cost_estimator.py       ➔ dynamic risk margin cost compiler
├── quote_generator.py      ➔ Prepares quote items
├── stripe_integration.py   ➔ Stripe Checkout Session and payment links generator
```

---

## 🔑 Environment Configuration (`.env`)

Create a `.env` file in the root directory:
```ini
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
NVIDIA_API_KEY=nvapi-...
```

* **Stripe Sandbox:** Handles test card entries (`4242 4242 4242 4242`).
* **Direct Verification Fallback:** Bypasses local webhook constraints by directly polling the Stripe API on redirection (`?payment=success`), immediately transitioning job status to `paid`.
* **Nemotron Fail-fast:** The reasoning API is configured with a 15-second timeout. If the remote endpoint is congested, the system falls back to structured engineering summaries to keep the customer loading screen fast.

---

## 📦 Sandbox Agent Integration (NemoClaw & Hermes)

The backend natively integrates with the Hermes agent running inside a NemoClaw OpenShell sandbox.

1. **Event-Driven Trigger**: Flask triggers the agent via a signed HTTP Webhook (`job.created`) when a new STL is processed. This webhook hits a Cloudflare tunnel pointing to port 8642.
2. **Internal Proxy Routing**: Inside the sandbox, a custom Python reverse proxy (which replaces the default socat) intercepts the traffic. It routes `/webhooks/*` endpoints to the Hermes webhook platform (8644) and routes everything else to the main API server (18642).
3. **Air-Gapped Decisions**: The Hermes agent reads the job from its local SQLite database, performs reasoning, and makes a decision based on the business rules.
4. **Callback with Bypass**: Hermes generates a JSON payload with its decision and uses Python's `urllib` to send an HTTP POST request back to the Flask app via Ngrok. The strict NemoClaw proxy permits this request because of a custom network policy allowing `*.ngrok-free.app`. Hermes also attaches the `ngrok-skip-browser-warning: 1` header to successfully bypass the Ngrok free tier interstitial page.
5. **Database Sync & Local Poller**: The HTTP POST hits `/api/agent-decide/<job_id>` on the Flask app, which updates the SQLite database with the decision. A background thread detects the decision and advances the pipeline state to `quoted`, allowing the customer to checkout without relying on inbound proxy connections.
