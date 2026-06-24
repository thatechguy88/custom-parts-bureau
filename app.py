"""
app.py — Flask application for The Custom Parts Bureau.

Routes:
  Pages:  /, /quote/<id>, /status/<id>, /dashboard
  API:    /api/upload, /api/quote/<id>, /api/checkout/<id>,
          /api/webhook, /api/status/<id>, /api/dashboard-data

Pipeline runs in background threads after upload:
  analyze_stl → estimate_cost → generate_quote → generate_reasoning
"""

import atexit
import json
import os
import sys
import time
import uuid
import threading
import traceback
import requests
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    abort,
)

# ── Local modules ────────────────────────────────────────────────
from models import (
    init_db, create_job, update_job, get_job, get_all_jobs, delete_job,
    update_job_stage, get_jobs_awaiting_decision, get_stale_jobs,
    get_midpipeline_jobs,
)
from adapters import (
    quote_to_stripe_dict,
    inject_machine_hours,
    jobs_to_dashboard_data,
)
from stl_analyzer import analyze_stl
from cost_estimator import estimate_cost
from quote_generator import generate_quote, Decision

# Stripe — import but don't fail if not installed yet
try:
    from stripe_integration import (
        create_checkout_session,
        init_stripe,
        verify_payment,
    )
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    print("⚠ stripe_integration not available — checkout will be mocked")

# ── App config ───────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB max upload

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Load .env manually (same pattern as existing modules)
def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

load_env()

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
executor = ThreadPoolExecutor(max_workers=2)

# Track in-flight pipeline futures for lifecycle management
_pipeline_futures = {}   # {job_id: Future}
_futures_lock = threading.Lock()

NEMOCLAW_WEBHOOK_URL = os.environ.get("NEMOCLAW_WEBHOOK_URL", "http://nemoclaw:8788/webhooks/job-review")
NEMOCLAW_HMAC_SECRET = os.environ.get("NEMOCLAW_HMAC_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
REVIEW_GROUP_CHAT_ID = os.environ.get("REVIEW_GROUP_CHAT_ID")
NGROK_URL = os.environ.get("NGROK_URL", "")


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not REVIEW_GROUP_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": REVIEW_GROUP_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] Failed to send message: {e}")


def trigger_agent_review(job_id, filename, email, margin, min_wall, confidence):
    """Webhook notification to NemoClaw Hermes agent."""
    if not NGROK_URL:
        print("[WEBHOOK] WARNING: NGROK_URL not set. Agent won't be able to callback!")
        
    payload = {
        "job_id": job_id,
        "filename": filename,
        "email": email,
        "event_type": "job.created",
        "margin": margin,
        "min_wall": min_wall,
        "confidence": confidence,
        "callback_url": f"{NGROK_URL.rstrip('/')}/api/agent-decide/{job_id}"
    }

    headers = {}
    if NEMOCLAW_HMAC_SECRET:
        import hmac
        import hashlib
        payload_bytes = json.dumps(payload).encode('utf-8')
        signature = hmac.new(
            NEMOCLAW_HMAC_SECRET.encode('utf-8'),
            payload_bytes, hashlib.sha256
        ).hexdigest()
        headers["X-Webhook-Signature"] = signature

    # Ensure webhook URL ends with the correct path
    webhook_url = NEMOCLAW_WEBHOOK_URL
    if not webhook_url.endswith("/webhooks/job-review"):
        webhook_url = webhook_url.rstrip("/") + "/webhooks/job-review"

    for attempt in range(2):
        try:
            response = requests.post(
                webhook_url, json=payload,
                headers=headers, timeout=5
            )
            print(f"[WEBHOOK] ✓ Notified agent for {job_id} (status {response.status_code})")
            return
        except Exception as e:
            if attempt == 0:
                print(f"[WEBHOOK] Attempt 1 failed for {job_id}: {e} — retrying in 2s...")
                time.sleep(2)
            else:
                print(f"[WEBHOOK] ✗ Webhook failed for {job_id}: {e}")




# ═══════════════════════════════════════════════════════════════
# STARTUP RECOVERY — re-queue interrupted pipeline jobs
# ═══════════════════════════════════════════════════════════════

def recover_stuck_jobs():
    """On startup, find and recover jobs stuck due to container restart.
    
    Two categories:
    1. Mid-pipeline (stage != 'reasoning'): pipeline thread died → re-submit
    2. Awaiting agent (stage == 'reasoning'): leave for poller/timeout to handle
    """
    mid_pipeline = get_midpipeline_jobs()
    if mid_pipeline:
        print(f"[RECOVERY] Found {len(mid_pipeline)} mid-pipeline jobs to re-queue")
        for job in mid_pipeline:
            job_id = job["id"]
            stl_path = job.get("stl_path", "")
            if stl_path and Path(stl_path).exists():
                print(f"[RECOVERY]   Re-queuing {job_id} (stage was: {job.get('analysis_stage')})")
                # Reset stage and re-submit
                update_job_stage(job_id, "uploaded")
                future = executor.submit(run_pipeline, job_id, stl_path)
                _track_future(job_id, future)
            else:
                print(f"[RECOVERY]   Cannot re-queue {job_id}: STL file missing ({stl_path})")
                update_job(
                    job_id,
                    status="rejected",
                    decision="REJECT",
                    reasoning_text="Pipeline interrupted and STL file is no longer available."
                )

    # Log jobs awaiting agent (they'll be handled by the poller/timeout)
    stuck_quoted = get_jobs_awaiting_decision()
    if stuck_quoted:
        print(f"[RECOVERY] {len(stuck_quoted)} job(s) awaiting agent decision (webhook callback expected)")


def db_decision_poller():
    """Poll DB for jobs where agent made a decision but Flask didn't process it yet."""
    from models import _get_conn
    print("[POLLER] Starting background DB decision poller...")
    while True:
        try:
            conn = _get_conn()
            cursor = conn.execute("SELECT id, agent_decision, agent_reasoning FROM jobs WHERE agent_decision IS NOT NULL AND status IN ('analyzing', 'reviewed')")
            jobs = cursor.fetchall()
            conn.close()

            for job in jobs:
                job_id = job["id"]
                decision = job["agent_decision"]
                reasoning = job["agent_reasoning"]
                
                # Sync database status based on decision
                status = "rejected" if decision == "REJECT" else "quoted"
                
                update_job(
                    job_id,
                    status=status,
                    decision=decision,
                    nemotron_explanation=reasoning,
                    analysis_stage=status
                )
                
                print(f"[POLLER] Job {job_id} updated to status={status} based on DB decision sync")
                
                # Send a notification to Telegram group chat
                icon = "✅" if decision == "ACCEPT" else "❌"
                msg = f"{icon} Hermes finished reviewing {job_id} (via DB Sync).\nDecision: {decision}\n\nReasoning:\n{reasoning}"
                send_telegram_message(msg)
                
        except Exception as e:
            print(f"[POLLER ERROR] {e}")
            
        time.sleep(3)


def _track_future(job_id, future):
    """Register a pipeline future with cleanup callback."""
    with _futures_lock:
        _pipeline_futures[job_id] = future

    def _on_done(f, jid=job_id):
        with _futures_lock:
            _pipeline_futures.pop(jid, None)
        exc = f.exception()
        if exc:
            print(f"[PIPELINE] Future for {jid} raised: {exc}")

    future.add_done_callback(_on_done)


# ═══════════════════════════════════════════════════════════════
# PIPELINE — runs in background thread
# ═══════════════════════════════════════════════════════════════

def run_pipeline(job_id, stl_path, material="PLA", rush=False):
    """
    Execute the full analysis pipeline in a background thread.
    """
    try:
        update_job_stage(job_id, "analyzing")
        update_job(job_id, status="analyzing")
        
        analysis = analyze_stl(stl_path)
        estimate = estimate_cost(analysis, material=material, rush=rush)
        quote = generate_quote(analysis, material=material, rush=rush, estimate=estimate)
        quote_dict = quote.to_dict()

        update_job_stage(job_id, "reasoning")
        
        bb = analysis.bounding_box
        update_job(
            job_id,
            status="analyzing",
            confidence=analysis.structural_confidence,
            volume_cm3=analysis.volume_cm3,
            surface_area_cm2=analysis.surface_area_cm2,
            triangle_count=analysis.triangle_count,
            bounding_box=f"{bb.x_mm:.1f} × {bb.y_mm:.1f} × {bb.z_mm:.1f}",
            overhang_pct=analysis.overhang.overhang_percentage,
            min_wall_mm=analysis.wall_thickness.min_thickness_mm,
            material_usd=estimate.breakdown.material_cost_usd,
            machine_usd=estimate.breakdown.machine_cost_usd,
            support_usd=estimate.breakdown.support_cost_usd,
            margin_usd=estimate.breakdown.margin_amount_usd,
            margin_pct=estimate.breakdown.margin_pct,
            total_usd=estimate.breakdown.total_usd,
            reasoning_text=quote.reasoning_text,
            line_items_json=json.dumps(quote_dict.get("line_items", [])),
        )

        job_details = get_job(job_id)
        email = job_details.get("email") if job_details else "unknown@email.com"
        
        send_telegram_message(f"🚨 New Job: {job_id} ({analysis.filename})\nMargin: ${estimate.breakdown.margin_amount_usd:.2f} | Wall: {analysis.wall_thickness.min_thickness_mm:.2f}mm | Conf: {analysis.structural_confidence}\nSending to Hermes via webhook...")
        
        trigger_agent_review(
            job_id=job_id,
            filename=analysis.filename,
            email=email,
            margin=estimate.breakdown.margin_amount_usd,
            min_wall=analysis.wall_thickness.min_thickness_mm,
            confidence=analysis.structural_confidence
        )
        return

    except Exception as e:
        traceback.print_exc()
        update_job(
            job_id,
            status="rejected",
            decision="REJECT",
            reasoning_text=f"Pipeline error: {str(e)}"
        )


# ═══════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/quote/<job_id>")
def quote_page(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)
    return render_template("quote.html", job_id=job_id)


@app.route("/status/<job_id>")
def status_page(job_id):
    job = get_job(job_id)
    if not job:
        abort(404)

    if request.args.get("payment") == "success" and job.get("status") in ("paying", "quoted", "uploaded"):
        session_id = job.get("stripe_session_id")
        if session_id and STRIPE_AVAILABLE:
            try:
                init_stripe()
                result = verify_payment(session_id)
                if result.get("payment_status") == "paid":
                    job = update_job(
                        job_id,
                        status="paid",
                        stripe_payment_status="paid"
                    )
            except Exception as e:
                print(f"[STATUS] Direct payment verification failed: {e}")

    return render_template("status.html", job_id=job_id)


@app.route("/dashboard")
def dashboard():
    return send_from_directory(
        Path(__file__).parent, "dashboard.html"
    )


# ═══════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    if not file.filename.lower().endswith(".stl"):
        return jsonify({"error": "Only .stl files are accepted"}), 400

    email = request.form.get("email", "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required"}), 400

    original_name = file.filename
    safe_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
    stl_path = UPLOAD_DIR / safe_name
    file.save(str(stl_path))

    try:
        import trimesh
        temp_mesh = trimesh.load(str(stl_path), force="mesh")
        if len(temp_mesh.faces) > 150000:
            stl_path.unlink(missing_ok=True)
            return jsonify({
                "error": f"Model complexity exceeds automated limits ({len(temp_mesh.faces):,} triangles found, max 150,000). Please simplify your mesh."
            }), 400
    except Exception as e:
        stl_path.unlink(missing_ok=True)
        return jsonify({"error": f"Invalid STL file format or corrupted model: {str(e)}"}), 400

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    name_prefix = Path(original_name).stem[:8].upper().replace(" ", "_")
    job_id = f"Q-{date_str}-{name_prefix}"

    if get_job(job_id):
        job_id = f"{job_id}-{uuid.uuid4().hex[:4].upper()}"

    create_job(job_id, original_name, email, str(stl_path))

    material = request.form.get("material", "PLA")
    rush = request.form.get("rush", "false").lower() == "true"

    future = executor.submit(run_pipeline, job_id, str(stl_path), material, rush)
    _track_future(job_id, future)

    return jsonify({
        "job_id": job_id,
        "redirect": f"/status/{job_id}",
        "status": "uploaded",
        "message": "Analysis started",
    }), 202


@app.route("/api/quote/<job_id>", methods=["GET"])
def api_quote(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "job_id": job["id"],
        "status": job["status"],
        "filename": job["filename"],
        "decision": job.get("decision"),
        "confidence": job.get("confidence"),
        "volume_cm3": job.get("volume_cm3"),
        "surface_area_cm2": job.get("surface_area_cm2"),
        "triangle_count": job.get("triangle_count"),
        "bounding_box": job.get("bounding_box"),
        "overhang_pct": job.get("overhang_pct"),
        "min_wall_mm": job.get("min_wall_mm"),
        "material_usd": job.get("material_usd"),
        "machine_usd": job.get("machine_usd"),
        "support_usd": job.get("support_usd"),
        "margin_pct": job.get("margin_pct"),
        "total_usd": job.get("total_usd"),
        "reasoning_text": job.get("reasoning_text"),
        "nemotron_explanation": job.get("nemotron_explanation"),
        "line_items": job.get("line_items", []),
        "created_at": job.get("created_at"),
    })


@app.route("/api/checkout/<job_id>", methods=["POST"])
def api_checkout(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] not in ("quoted",):
        return jsonify({"error": f"Job is not in a payable state (status: {job['status']})"}), 400

    quote_dict = {
        "quote_id": job["id"],
        "filename": job["filename"],
        "total_usd": job.get("total_usd", 0),
        "confidence": job.get("confidence", 0),
        "decision": job.get("decision", "ACCEPT"),
        "reasoning_text": job.get("reasoning_text", ""),
        "line_items": job.get("line_items", []),
    }
    stripe_dict = quote_to_stripe_dict(quote_dict)

    base_url = request.host_url.rstrip("/")
    success_url = f"{base_url}/status/{job_id}?payment=success"
    cancel_url = f"{base_url}/quote/{job_id}?payment=cancelled"

    if STRIPE_AVAILABLE:
        try:
            init_stripe()
            result = create_checkout_session(
                stripe_dict,
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=job.get("email"),
            )

            update_job(
                job_id,
                status="paying",
                stripe_session_id=result["session_id"],
                stripe_payment_status="unpaid",
            )

            return jsonify({
                "checkout_url": result["url"],
                "session_id": result["session_id"],
            })

        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": f"Stripe error: {str(e)}"}), 500
    else:
        mock_session_id = f"cs_test_mock_{uuid.uuid4().hex[:8]}"
        update_job(
            job_id,
            status="paid",
            stripe_session_id=mock_session_id,
            stripe_payment_status="paid",
        )
        return jsonify({
            "checkout_url": f"{base_url}/status/{job_id}?payment=success",
            "session_id": mock_session_id,
        })


@app.route("/api/webhook", methods=["POST"])
def api_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")

    if STRIPE_AVAILABLE:
        if STRIPE_WEBHOOK_SECRET:
            import stripe
            try:
                event = stripe.Webhook.construct_event(
                    payload, sig_header, STRIPE_WEBHOOK_SECRET
                )
            except (ValueError, stripe.error.SignatureVerificationError) as e:
                print(f"[WEBHOOK] Signature verification failed: {e}")
                return jsonify({"error": "Invalid signature"}), 400
        else:
            if not app.debug:
                print("[WEBHOOK] Security violation: STRIPE_WEBHOOK_SECRET is missing in non-debug/production mode.")
                return jsonify({"error": "Configuration error: Webhook verification required"}), 500
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                return jsonify({"error": "Invalid JSON"}), 400
    else:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON"}), 400

    event_type = event.get("type", "")
    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        session_id = session.get("id", "")

        if session_id:
            all_jobs = get_all_jobs()
            for job in all_jobs:
                if job.get("stripe_session_id") == session_id:
                    update_job(
                        job["id"],
                        status="paid",
                        stripe_payment_status="paid",
                    )
                    break
            else:
                print(f"[WEBHOOK] No job found for session {session_id}")

    return jsonify({"received": True}), 200


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "job_id": job["id"],
        "filename": job["filename"],
        "email": job["email"],
        "status": job["status"],
        "analysis_stage": job.get("analysis_stage", "uploaded"),
        "decision": job.get("decision"),
        "confidence": job.get("confidence"),
        "volume_cm3": job.get("volume_cm3"),
        "bounding_box": job.get("bounding_box"),
        "triangle_count": job.get("triangle_count"),
        "total_usd": job.get("total_usd"),
        "reasoning_text": job.get("reasoning_text"),
        "nemotron_explanation": job.get("nemotron_explanation"),
        "stripe_payment_status": job.get("stripe_payment_status"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    })


@app.route("/api/agent-decide/<job_id>", methods=["POST"])
def api_agent_decide(job_id):
    data = request.get_json() or {}
    decision = data.get("decision")
    reasoning = data.get("reasoning", "")
    
    if not decision or decision not in ("ACCEPT", "CONDITIONAL", "REJECT"):
        return jsonify({"error": "Valid decision is required (ACCEPT/CONDITIONAL/REJECT)"}), 400
        
    status = "rejected" if decision == "REJECT" else "quoted"
    analysis_stage = status
    
    updated = update_job(
        job_id,
        decision=decision,
        nemotron_explanation=reasoning,
        status=status,
        analysis_stage=analysis_stage
    )
    if not updated:
        return jsonify({"error": "Job not found"}), 404
        
    icon = "✅" if decision == "ACCEPT" else "❌"
    msg = f"{icon} Hermes finished reviewing {job_id}.\nDecision: {decision}\n\nReasoning:\n{reasoning}"
    send_telegram_message(msg)
    
    return jsonify({"status": "ok", "job_id": job_id})


@app.route("/api/agent-review/<job_id>", methods=["GET"])
def agent_review(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/dashboard-data", methods=["GET"])
def api_dashboard_data():
    jobs = get_all_jobs()
    dashboard_data = jobs_to_dashboard_data(jobs)
    return jsonify(dashboard_data)


@app.route("/api/dashboard-chat", methods=["POST"])
def api_dashboard_chat():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400
        
    jobs = get_all_jobs()
    from chat_engine import generate_chat_response
    response_text = generate_chat_response(message, jobs)
    return jsonify({"response": response_text})


@app.route('/api/job/<job_id>/print', methods=['POST'])
def move_to_print(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    if job.get("status") != "paid":
        return jsonify({"error": "Job must be paid before moving to print"}), 400
        
    update_job(job_id, status="printing")
    return jsonify({"status": "printing"})


@app.route('/api/job/<job_id>', methods=['DELETE'])
def api_delete_job(job_id):
    """Delete a job entirely."""
    deleted = delete_job(job_id)
    if not deleted:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"status": "ok", "message": f"Job {job_id} deleted."})


# ═══════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("status.html", job_id="NOT_FOUND"), 404


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large (max 50MB)"}), 413


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()

    print("╔═══════════════════════════════════════════════════╗")
    print("║   THE CUSTOM PARTS BUREAU — Server Starting      ║")
    print("║                                                   ║")
    print("║   Ngrok Webhook Integration active:               ║")
    print("║   • Hermes Agent reviews via HTTP callbacks       ║")
    print("║                                                   ║")

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5001))
    print(f"║   Binding to {host}:{port}                          ║")
    print("╚═══════════════════════════════════════════════════╝")

    # Startup recovery — re-queue jobs interrupted by container restart
    recover_stuck_jobs()

    # Start background poller for agent decisions via DB sync
    threading.Thread(target=db_decision_poller, daemon=True).start()

    # Graceful shutdown — signal executor to stop accepting new work
    atexit.register(executor.shutdown, wait=False)

    # Run Flask — use_reloader=False to prevent double-forking the
    # ThreadPoolExecutor and poller thread in debug mode
    app.run(debug=True, host=host, port=port, use_reloader=False)
