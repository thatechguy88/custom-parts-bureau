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
├── templates/              ➔ Monospace dark-CRT style views (Jinja2)
│   ├── base.html           ➔ Shared visual shell (scanlines, custom fonts)
│   ├── landing.html        ➔ Drag/drop upload page
│   ├── quote.html          ➔ 3-stage loading and quote cards
│   └── status.html         ➔ Progress bar order status
├── stl_analyzer.py         ➔ Ray-cast wall thickness, overhang, & watertight checks
├── cost_estimator.py       ➔ dynamic risk margin cost compiler
├── quote_generator.py      ➔ Decision rules (Accept >=70, Conditional 30-70, Reject <30)
├── nemotron_reasoning.py   ➔ NVIDIA API caller for Nemotron 3 Ultra (550B)
└── stripe_integration.py   ➔ Stripe Checkout Session and payment links generator
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

## 📦 Sandbox Agent Integration (NemoClaw)

The NemoClaw agent resides in the `openshell` container, mounting the project at `/sandbox/project`.

1. **System Monitoring:** The agent can poll server health via:
   `curl -s http://172.18.0.1:8080/api/dashboard-data`
2. **Quote Quality Audits:** The agent can run python QA scripts directly against the shared database file:
   `/sandbox/project/cpb.db`
3. **Internal Run:** The agent can run testing cycles inside its container using the virtualenv:
   `/sandbox/venv/bin/python /sandbox/project/app.py`
