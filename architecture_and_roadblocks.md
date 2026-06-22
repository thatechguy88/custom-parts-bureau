# Custom Parts Bureau (CPB) - Architecture & Project Overview

This document outlines the expected end-to-end loop, current system architecture, active roadblocks, and strict system restrictions for the automated 3D printing bureau project.

## 1. Expected End-to-End Loop

The expected customer flow seamlessly integrates a standard web application with an autonomous AI reasoning agent:

1. **Upload & Initial Processing**: A customer uploads an STL file via the Flask web interface.
2. **Baseline Analysis (Flask)**: The Flask app's background thread processes the STL file (calculating volume, bounding box, wall thickness, and overhangs using `trimesh`).
3. **Cost Estimation (Flask)**: The app estimates base costs (material, machine time, support material) and applies a standard margin.
4. **Agent Webhook Trigger**: The Flask app fires a signed HTTP Webhook to the NemoClaw sandbox gateway, delivering the baseline metrics to trigger an agent review.
5. **AI Reasoning (Hermes Agent)**: The autonomous agent inside the NemoClaw sandbox receives the webhook. It reads the full job details directly from its local copy of the SQLite database (`cpb.db`).
6. **Agent Decision**: The agent uses an LLM to evaluate the physical constraints and P&L (e.g., rejecting if margin < $2.50 or walls are too thin). It writes its final `ACCEPT` or `REJECT` decision and reasoning back to the local database.
7. **Database Sync**: A background process (`db_sync.py`) running on the host immediately detects the DB change and synchronizes the SQLite file back to the Flask application.
8. **Flask Poller**: A background poller in the Flask app detects the agent's decision inside the synced `cpb.db` and advances the job state to `quoted` or `rejected`, firing a final notification to Telegram.
9. **Customer Checkout**: The Flask UI polls the API and updates. If accepted, the user is presented with the quote, agent reasoning, and a Stripe Checkout button.
10. **Payment Confirmation**: A Stripe webhook confirms payment and updates the job state to `paid`, completing the loop.

## 2. Current Architecture Overview

The system is distributed across three main environments on the Mac host:

### Flask App Container (`cpb-app`)
- **Stack**: Python, Flask, SQLite.
- **Role**: Handles the frontend UI, STL geometry analysis via background threads, and Stripe API interactions.
- **Network**: Runs in Docker Desktop on the `openshell-docker` network.

### NemoClaw Sandbox (`openshell-hackathon01`)
- **Stack**: Isolated, air-gapped Docker namespace.
- **Role**: Hosts the **Hermes AI Agent**. The agent uses the NVIDIA Nemotron API for reasoning and has a Telegram interface for manual oversight.
- **Gateway**: Exposes a webhook listener (port 8644) that the Flask app attempts to call to trigger the agent.

### Mac Host / File Sync Layer
- **Role**: Bridges the gap between the Flask app and the sandbox.
- **Data Sync**: A Python script (`db_sync.py`) runs continuously on the host, copying `cpb.db` back and forth between the Flask app's directory and the sandbox's `/opt/hermes/workspace` directory every 2 seconds.

## 3. Roadblocks Solved

1. **Egress Proxy Blocking (Callback Failure)**
   - **Issue**: The NemoClaw sandbox transparent proxy explicitly blocks outbound HTTPS requests. This prevented the agent from using a traditional HTTP callback to notify Flask of its decision.
   - **Resolution**: We completely bypassed sandbox outbound networking. Instead of an HTTP callback, the agent writes its decision to the SQLite database. `db_sync.py` synchronizes the file back to the host, and a background thread in Flask (`db_decision_poller`) detects the change and advances the pipeline. This perfectly local, reliable synchronization architecture eliminates the need for Ngrok tunnels and complex network policies.

2. **Docker Mac Networking (Webhook Connectivity)**
   - **Issue**: Previously, the Flask app struggled to reach the agent's webhook listener securely due to Docker-for-Mac networking isolation.
   - **Resolution**: Implemented proper webhook URL routing via `.env` overrides to reliably hit the OpenShell gateway, coupled with strict HMAC signature verification.

## 4. Known Restrictions & Guardrails

These are strict constraints that **must not be bypassed**:

> [!WARNING]
> **No Host-Side Network Hacks**
> Per `AGENTS.md` rules: Do NOT use complex host-side hacks (like injecting `socat`, manually altering iptables, or manually curling API endpoints) to bypass broken sandbox infrastructure. If the sandbox network fails, rely on standard NemoClaw recovery tools or ask the user to manually intervene.

> [!CAUTION]
> **Air-Gapped Sandbox Constraints**
> The sandbox is strictly air-gapped. Nothing goes in or out except via configured webhook gateways, Telegram polling, and the mounted `/workspace` directory. 
> - The sandbox agent **cannot** make HTTP requests to the Flask app (due to proxy blocks).
> - The agent must read data exclusively from `/workspace/cpb.db`.

> [!IMPORTANT]
> **Sandbox Tooling Restrictions**
> - The `sqlite3` CLI is **not installed** inside the sandbox environment. The agent must always use the Python `sqlite3` module to interact with the database.
> - Sandbox instances are highly ephemeral. Re-creating the sandbox destroys transient state; always rely on the host-synced `cpb.db` for source-of-truth job data.
