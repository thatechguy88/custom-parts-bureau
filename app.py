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
from pathlib import Path
from datetime import datetime, timezone

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
from nemotron_reasoning import generate_reasoning

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


# ═══════════════════════════════════════════════════════════════
# PIPELINE — runs in background thread
# ═══════════════════════════════════════════════════════════════

def run_pipeline(job_id, stl_path):
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
        estimate = estimate_cost(analysis)
        print(f"[PIPELINE] {job_id} — cost estimated: ${estimate.breakdown.total_usd:.2f}")

        # ── Stage 3: Generate quote ──────────────────────────
        quote = generate_quote(analysis, estimate)
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

        # ── Stage 5: Determine status ────────────────────────
        final_status = "rejected" if quote.decision == Decision.REJECT else "quoted"

        # ── Stage 6: Persist to DB ───────────────────────────
        bb = analysis.bounding_box
        update_job(
            job_id,
            status=final_status,
            decision=quote.decision.value,
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
            nemotron_explanation=nemotron_result.get("explanation", ""),
            line_items_json=json.dumps(quote_dict.get("line_items", [])),
        )
        print(f"[PIPELINE] {job_id} — complete → {final_status}")

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

    # Generate job ID (same format as quote_generator)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    name_prefix = Path(original_name).stem[:8].upper().replace(" ", "_")
    job_id = f"Q-{date_str}-{name_prefix}"

    # Ensure unique ID
    if get_job(job_id):
        job_id = f"{job_id}-{uuid.uuid4().hex[:4].upper()}"

    # Create DB record
    create_job(job_id, original_name, email, str(stl_path))

    # Start pipeline in background
    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id, str(stl_path)),
        daemon=True,
    )
    thread.start()

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
    if STRIPE_WEBHOOK_SECRET and STRIPE_AVAILABLE:
        import stripe
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            print(f"[WEBHOOK] Signature verification failed: {e}")
            return jsonify({"error": "Invalid signature"}), 400
    else:
        # No webhook secret — parse payload directly (dev mode)
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


@app.route("/api/dashboard-data", methods=["GET"])
def api_dashboard_data():
    """
    Return all jobs in the format the dashboard expects.
    Dashboard JS falls back to mock data if <5 jobs returned.
    """
    jobs = get_all_jobs()
    dashboard_data = jobs_to_dashboard_data(jobs)
    return jsonify(dashboard_data)


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
