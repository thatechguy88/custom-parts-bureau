"""
app.py — Flask application for The Custom Parts Bureau.

Routes:
  Pages:  /, /quote/<id>, /status/<id>, /dashboard
  API:    /api/upload, /api/quote/<id>, /api/checkout/<id>,
          /api/webhook, /api/status/<id>, /api/dashboard-data

Pipeline runs in background threads after upload:
  analyze_stl → estimate_cost → generate_quote → generate_reasoning
"""

import json
import os
import sys
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
from models import init_db, create_job, update_job, get_job, get_all_jobs
from adapters import (
    quote_to_stripe_dict,
    inject_machine_hours,
    jobs_to_dashboard_data,
)
from stl_analyzer import analyze_stl
from cost_estimator import estimate_cost
from quote_generator import generate_quote, Decision
from nemotron_reasoning import generate_reasoning, generate_chat_response

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

NEMOCLAW_WEBHOOK_URL = os.environ.get("NEMOCLAW_WEBHOOK_URL", "http://nemoclaw:8788/webhooks/job-review")

def trigger_agent_review(job_id, filename, email):
    """Notify the NemoClaw Hermes agent about a completed quote analysis."""
    try:
        payload = {
            "job_id": job_id,
            "filename": filename,
            "email": email,
            "event": "job.created"
        }
        # Fire POST to NemoClaw's hermes webhook
        response = requests.post(NEMOCLAW_WEBHOOK_URL, json=payload, timeout=5)
        print(f"[AGENT WEBHOOK] Notified agent for job {job_id}, status code: {response.status_code}")
    except Exception as e:
        print(f"[AGENT WEBHOOK] Warning: failed to notify agent: {e}")


# ═══════════════════════════════════════════════════════════════
# PIPELINE — runs in background thread
# ═══════════════════════════════════════════════════════════════

def run_pipeline(job_id, stl_path, material="PLA", rush=False):
    """
    Execute the full analysis pipeline in a background thread.

    Flow: analyze → cost → quote → nemotron reasoning → persist to DB
    """
    try:
        # ── Stage 1: Analyze geometry ────────────────────────
        update_job(job_id, status="analyzing")
        print(f"[PIPELINE] {job_id} — analyzing {stl_path}")

        analysis = analyze_stl(stl_path)
        print(f"[PIPELINE] {job_id} — analysis complete: confidence={analysis.structural_confidence:.1f}")

        # ── Stage 2: Estimate cost ───────────────────────────
        estimate = estimate_cost(analysis, material=material, rush=rush)
        print(f"[PIPELINE] {job_id} — cost estimated: ${estimate.breakdown.total_usd:.2f}")

        # ── Stage 3: Generate quote ──────────────────────────
        quote = generate_quote(analysis, material=material, rush=rush, estimate=estimate)
        quote_dict = quote.to_dict()
        print(f"[PIPELINE] {job_id} — quote generated: {quote.decision.value}")

        # ── Stage 4: Nemotron reasoning ──────────────────────
        inject_machine_hours(quote_dict, estimate)
        nemotron_result = {"explanation": "", "success": False}
        try:
            nemotron_result = generate_reasoning(quote_dict)
            if nemotron_result.get("success"):
                print(f"[PIPELINE] {job_id} — Nemotron reasoning complete")
            else:
                print(f"[PIPELINE] {job_id} — Nemotron failed, using built-in reasoning")
        except Exception as e:
            print(f"[PIPELINE] {job_id} — Nemotron error: {e}, using built-in reasoning")

        # ── Stage 5: Determine status & overrides ────────────
        final_decision = quote.decision.value
        total_usd = estimate.breakdown.total_usd
        margin_usd = estimate.breakdown.margin_amount_usd
        margin_pct = estimate.breakdown.margin_pct
        line_items = quote_dict.get("line_items", [])
        
        if nemotron_result.get("success"):
            # Check decision override
            override = nemotron_result.get("decision_override")
            if override in ("ACCEPT", "CONDITIONAL", "REJECT"):
                final_decision = override
                print(f"[PIPELINE] {job_id} — Nemotron decision override: {quote.decision.value} -> {override}")
            
            # Apply surcharge if suggested
            surcharge_pct = nemotron_result.get("margin_surcharge_pct", 0.0)
            if surcharge_pct > 0.0:
                print(f"[PIPELINE] {job_id} — Applying Nemotron risk surcharge: +{surcharge_pct}%")
                surcharge_amount = estimate.breakdown.subtotal_usd * (surcharge_pct / 100.0)
                margin_usd += surcharge_amount
                margin_pct += surcharge_pct
                total_usd += surcharge_amount
                
                # Patch line items list to reflect dynamic surcharge changes
                for item in line_items:
                    if item.get("label", "").startswith("Margin"):
                        item["label"] = f"Margin ({margin_pct:.0f}%)"
                        item["cost_usd"] = margin_usd
                        item["detail"] = "Confidence-adjusted risk margin + AI surcharge"
        
        final_status = "rejected" if final_decision == "REJECT" else "quoted"

        # ── Stage 6: Persist to DB ───────────────────────────
        bb = analysis.bounding_box
        update_job(
            job_id,
            status=final_status,
            decision=final_decision,
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
            margin_usd=margin_usd,
            margin_pct=margin_pct,
            total_usd=total_usd,
            reasoning_text=quote.reasoning_text,
            nemotron_explanation=nemotron_result.get("explanation", ""),
            line_items_json=json.dumps(line_items),
        )
        print(f"[PIPELINE] {job_id} — complete → {final_status}")
        
        # Trigger event-driven agent review webhook in NemoClaw
        job_details = get_job(job_id)
        email = job_details.get("email") if job_details else "unknown@email.com"
        trigger_agent_review(job_id, filename=analysis.filename, email=email)

    except Exception as e:
        traceback.print_exc()
        update_job(
            job_id,
            status="rejected",
            decision="REJECT",
            reasoning_text=f"Pipeline error: {str(e)}",
            nemotron_explanation=f"Analysis failed: {str(e)}",
        )
        print(f"[PIPELINE] {job_id} — FAILED: {e}")


# ═══════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def landing():
    """Upload page — drag/drop STL + email."""
    return render_template("landing.html")


@app.route("/quote/<job_id>")
def quote_page(job_id):
    """Quote display — loading animation → quote → pay."""
    job = get_job(job_id)
    if not job:
        abort(404)
    return render_template("quote.html", job_id=job_id)


@app.route("/status/<job_id>")
def status_page(job_id):
    """Order tracking — timeline view."""
    job = get_job(job_id)
    if not job:
        abort(404)

    # Direct Stripe check if redirected back with success param
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
                    print(f"[STATUS] Direct payment verified for {job_id}")
            except Exception as e:
                print(f"[STATUS] Direct payment verification failed: {e}")

    return render_template("status.html", job_id=job_id)


@app.route("/dashboard")
def dashboard():
    """P&L dashboard — serves the existing dashboard.html."""
    return send_from_directory(
        Path(__file__).parent, "dashboard.html"
    )


# ═══════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    Receive STL file + email.
    Saves file, creates job, kicks off pipeline in background.
    Returns: {job_id, status}
    """
    # Validate file
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    if not file.filename.lower().endswith(".stl"):
        return jsonify({"error": "Only .stl files are accepted"}), 400

    # Validate email
    email = request.form.get("email", "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required"}), 400

    # Save file
    original_name = file.filename
    safe_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
    stl_path = UPLOAD_DIR / safe_name
    file.save(str(stl_path))

    # Fast validation for face complexity (prevent CPU denial-of-service)
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

    # Generate job ID (same format as quote_generator)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    name_prefix = Path(original_name).stem[:8].upper().replace(" ", "_")
    job_id = f"Q-{date_str}-{name_prefix}"

    # Ensure unique ID
    if get_job(job_id):
        job_id = f"{job_id}-{uuid.uuid4().hex[:4].upper()}"

    # Create DB record
    create_job(job_id, original_name, email, str(stl_path))

    material = request.form.get("material", "PLA")
    rush = request.form.get("rush", "false").lower() == "true"

    # Start pipeline in background using the thread pool executor
    executor.submit(run_pipeline, job_id, str(stl_path), material, rush)

    return jsonify({
        "job_id": job_id,
        "status": "uploaded",
        "message": "Analysis started",
    }), 202


@app.route("/api/quote/<job_id>", methods=["GET"])
def api_quote(job_id):
    """
    Return quote data for a job.
    Frontend polls this until status != 'analyzing'.
    """
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
    """
    Create a Stripe Checkout Session for a quoted job.
    Returns: {checkout_url}
    """
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] not in ("quoted",):
        return jsonify({"error": f"Job is not in a payable state (status: {job['status']})"}), 400

    # Build the quote dict in the format stripe_integration expects
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

    # Build URLs
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
        # Mock mode — simulate checkout
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
    """
    Stripe webhook handler.
    Handles checkout.session.completed events.
    """
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")

    # If we have a webhook secret, verify the signature
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
            # Missing webhook secret: only permit unverified payloads in dev debug mode
            if not app.debug:
                print("[WEBHOOK] Security violation: STRIPE_WEBHOOK_SECRET is missing in non-debug/production mode.")
                return jsonify({"error": "Configuration error: Webhook verification required"}), 500
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                return jsonify({"error": "Invalid JSON"}), 400
    else:
        # Stripe not installed (Mock mode) — parse directly
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON"}), 400

    # Handle checkout.session.completed
    event_type = event.get("type", "")
    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        session_id = session.get("id", "")

        if session_id:
            # Find the job by stripe_session_id
            all_jobs = get_all_jobs()
            for job in all_jobs:
                if job.get("stripe_session_id") == session_id:
                    update_job(
                        job["id"],
                        status="paid",
                        stripe_payment_status="paid",
                    )
                    print(f"[WEBHOOK] Payment confirmed for {job['id']}")
                    break
            else:
                print(f"[WEBHOOK] No job found for session {session_id}")

    return jsonify({"received": True}), 200


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id):
    """Return job status and details for the status page."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "job_id": job["id"],
        "filename": job["filename"],
        "email": job["email"],
        "status": job["status"],
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
    """
    Agent updates the job decision (and explanation) after review.
    Synchronizes decision and status.
    """
    data = request.get_json() or {}
    decision = data.get("decision")
    reasoning = data.get("reasoning", "")
    
    if not decision or decision not in ("ACCEPT", "CONDITIONAL", "REJECT"):
        return jsonify({"error": "Valid decision is required (ACCEPT/CONDITIONAL/REJECT)"}), 400
        
    # Sync database status based on decision
    status = "rejected" if decision == "REJECT" else "quoted"
    
    updated = update_job(
        job_id,
        decision=decision,
        nemotron_explanation=reasoning,
        status=status
    )
    if not updated:
        return jsonify({"error": "Job not found"}), 404
        
    print(f"[AGENT CALLBACK] Job {job_id} updated by agent to decision={decision}, status={status}")
    return jsonify({"status": "ok", "job_id": job_id})


@app.route("/api/agent-review/<job_id>", methods=["GET"])
def agent_review(job_id):
    """Return job data for agent review."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/dashboard-data", methods=["GET"])
def api_dashboard_data():
    """
    Return all jobs in the format the dashboard expects.
    Dashboard JS falls back to mock data if <5 jobs returned.
    """
    jobs = get_all_jobs()
    dashboard_data = jobs_to_dashboard_data(jobs)
    return jsonify(dashboard_data)


@app.route("/api/dashboard-chat", methods=["POST"])
def api_dashboard_chat():
    """
    Operator panel live chat assistant.
    Receives user query, loads jobs from db, and calls Nemotron.
    """
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400
        
    jobs = get_all_jobs()
    response_text = generate_chat_response(message, jobs)
    return jsonify({"response": response_text})


@app.route('/api/job/<job_id>/print', methods=['POST'])
def move_to_print(job_id):
    """Update job status from paid to printing."""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    if job.get("status") != "paid":
        return jsonify({"error": "Job must be paid before moving to print"}), 400
        
    update_job(job_id, status="printing")
    return jsonify({"status": "printing"})


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
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5001))
    print(f"║   Binding to {host}:{port}                          ║")
    print("╚═══════════════════════════════════════════════════╝")
    app.run(debug=True, host=host, port=port)
