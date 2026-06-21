"""
adapters.py — Schema bridges between pipeline modules.

Resolves all field-name mismatches WITHOUT modifying the original modules.
Three adapters:
  1. quote_to_stripe_dict  — Quote.to_dict() → stripe_integration expected format
  2. inject_machine_hours  — Patches analysis_summary for Nemotron
  3. job_to_dashboard_format — SQLite job row → dashboard JS object shape
"""

import json


def quote_to_stripe_dict(quote_dict):
    """
    Map quote_generator.Quote.to_dict() output → stripe_integration.create_checkout_session() input.

    Key renames:
      filename       → model_name
      total_usd      → total_cost
      reasoning_text  → reasoning
      label          → name       (per line item)
      cost_usd       → amount     (per line item)
    """
    stripe_items = []
    for item in quote_dict.get("line_items", []):
        # Skip the "Subtotal" line item — Stripe calculates its own total
        if item.get("label", "").lower() == "subtotal":
            continue
        # Skip margin line item — included in total but not a customer-facing charge
        if item.get("label", "").lower().startswith("margin"):
            continue
        stripe_items.append({
            "name": item["label"],
            "amount": item["cost_usd"],
        })

    return {
        "model_name": quote_dict.get("filename", "3D Print Job"),
        "total_cost": quote_dict.get("total_usd", 0.0),
        "confidence": quote_dict.get("confidence", 0),
        "decision": quote_dict.get("decision", "ACCEPT"),
        "reasoning": quote_dict.get("reasoning_text", ""),
        "line_items": stripe_items,
    }


def inject_machine_hours(quote_dict, cost_estimate):
    """
    Patch quote_dict["analysis_summary"]["machine_hours"] with the print time
    from CostEstimate. Nemotron reads this key but GeometryAnalysis.to_dict()
    never produces it.
    """
    if "analysis_summary" not in quote_dict:
        quote_dict["analysis_summary"] = {}

    print_time = 0.0
    if hasattr(cost_estimate, "context"):
        print_time = cost_estimate.context.estimated_print_time_hours
    elif isinstance(cost_estimate, dict):
        print_time = cost_estimate.get("context", {}).get(
            "estimated_print_time_hours", 0.0
        )

    quote_dict["analysis_summary"]["machine_hours"] = print_time
    return quote_dict


def job_to_dashboard_format(job):
    """
    Map a SQLite job row (dict) → the JS object shape the dashboard expects.

    The dashboard mock data uses these keys:
      id, filename, status, decision, confidence, total_usd,
      material_usd, machine_usd, support_usd, margin_usd, margin_pct,
      volume_cm3, triangles, bounding_box, min_wall_mm, overhang_pct,
      print_progress, print_eta, payment_status, stripe_session,
      reasoning, created_at, paid_at
    """
    # Map status to dashboard-compatible status
    status_map = {
        "uploaded": "paid",       # show as pending in queue
        "analyzing": "paid",
        "quoted": "paid",         # waiting for payment, show in queue
        "paying": "paid",
        "paid": "paid",
        "printing": "printing",
        "completed": "completed",
        "rejected": "on_hold",
    }

    # Map decision to dashboard format (ACCEPT → ACCEPTED)
    decision_raw = job.get("decision", "ACCEPT") or "ACCEPT"
    decision_map = {
        "ACCEPT": "ACCEPTED",
        "CONDITIONAL": "CONDITIONAL",
        "REJECT": "REJECTED",
    }

    # Determine payment_status
    stripe_status = job.get("stripe_payment_status", "")
    status = job.get("status", "uploaded")
    if status in ("paid", "printing", "completed"):
        payment_status = "paid"
    elif status == "rejected":
        payment_status = "refunded"
    elif stripe_status:
        payment_status = stripe_status
    else:
        payment_status = "pending"

    # Build reasoning — prefer Nemotron explanation, fall back to built-in if missing or error
    nemotron_exp = job.get("nemotron_explanation", "")
    if not nemotron_exp or "[Nemotron API error:" in nemotron_exp:
        reasoning = job.get("reasoning_text") or ""
    else:
        reasoning = nemotron_exp

    # Print progress simulation
    print_progress = 0
    print_eta = ""
    if status == "printing":
        print_progress = 0  # starts at 0, dashboard JS will increment it
        print_eta = "~2 hrs"
    elif status == "completed":
        print_progress = 100
        print_eta = ""

    return {
        "id": job.get("id", ""),
        "filename": job.get("filename", ""),
        "status": status_map.get(status, "paid"),
        "decision": decision_map.get(decision_raw, "ACCEPTED"),
        "confidence": job.get("confidence", 0) or 0,
        "total_usd": job.get("total_usd", 0) or 0,
        "material_usd": job.get("material_usd", 0) or 0,
        "machine_usd": job.get("machine_usd", 0) or 0,
        "support_usd": job.get("support_usd", 0) or 0,
        "margin_usd": job.get("margin_usd", 0) or 0,
        "margin_pct": job.get("margin_pct", 0) or 0,
        "volume_cm3": job.get("volume_cm3", 0) or 0,
        "triangles": job.get("triangle_count", 0) or 0,
        "bounding_box": job.get("bounding_box", "0 × 0 × 0") or "0 × 0 × 0",
        "min_wall_mm": job.get("min_wall_mm", 0) or 0,
        "overhang_pct": job.get("overhang_pct", 0) or 0,
        "print_progress": print_progress,
        "print_eta": print_eta,
        "payment_status": payment_status,
        "stripe_session": job.get("stripe_session_id", "") or "",
        "reasoning": reasoning,
        "created_at": job.get("created_at", ""),
        "paid_at": job.get("updated_at", "") if status in ("paid", "printing", "completed") else "",
    }


def jobs_to_dashboard_data(jobs):
    """
    Convert a list of job dicts to the full dashboard data payload.
    Returns {jobs: [...], notifications: [...]}.
    """
    dashboard_jobs = [job_to_dashboard_format(j) for j in jobs]

    # Generate notifications from recent jobs
    notifications = []
    for j in jobs[:10]:
        status = j.get("status", "")
        filename = j.get("filename", "unknown")
        total = j.get("total_usd", 0) or 0
        confidence = j.get("confidence", 0) or 0

        if status == "paid":
            notifications.append({
                "type": "success",
                "icon": "💰",
                "text": f"{filename} — payment received ${total:.2f}",
                "time": _extract_time(j.get("updated_at", "")),
            })
        elif status == "completed":
            notifications.append({
                "type": "info",
                "icon": "📦",
                "text": f"{filename} — print completed, ready to ship",
                "time": _extract_time(j.get("updated_at", "")),
            })
        elif status == "rejected":
            notifications.append({
                "type": "error",
                "icon": "❌",
                "text": f"{filename} — rejected (confidence {confidence:.1f}%)",
                "time": _extract_time(j.get("updated_at", "")),
            })
        elif status == "quoted":
            notifications.append({
                "type": "info",
                "icon": "📋",
                "text": f"{filename} — quote ready, awaiting payment",
                "time": _extract_time(j.get("updated_at", "")),
            })
        elif status == "analyzing":
            notifications.append({
                "type": "warning",
                "icon": "🔍",
                "text": f"{filename} — analysis in progress...",
                "time": _extract_time(j.get("updated_at", "")),
            })

    return {
        "jobs": dashboard_jobs,
        "notifications": notifications,
    }


def _extract_time(iso_str):
    """Extract HH:MM from an ISO timestamp string."""
    if not iso_str:
        return ""
    try:
        # Handle both 'T' separator and space
        time_part = iso_str.split("T")[1] if "T" in iso_str else iso_str.split(" ")[1]
        return time_part[:5]
    except (IndexError, AttributeError):
        return ""
