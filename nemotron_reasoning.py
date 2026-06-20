#!/usr/bin/env python3
"""
Nemotron 3 Ultra integration for The Custom Parts Bureau.

Calls Nemotron via the NemoClaw inference proxy to generate
natural language reasoning for 3D print quotes.

Inside NemoClaw sandbox: uses https://inference.local/v1
Outside sandbox: uses NVIDIA API directly (needs NVIDIA_API_KEY env var)

Usage:
    from nemotron_reasoning import generate_reasoning
    
    result = generate_reasoning(quote_data)
    print(result["explanation"])
"""

import json
import os
import sys
from typing import Optional

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests


# Detect environment
IN_SANDBOX = os.path.exists("/sandbox/.hermes")
INFERENCE_URL = os.getenv("NEMOCLAW_INFERENCE_BASE_URL", "https://inference.local/v1")
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1"
# Hermes internal API — always reachable from cpb-app via container network
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://172.19.0.2:8642/v1")
MODEL = "nvidia/nemotron-3-ultra-550b-a55b"  # The big one for reasoning
HERMES_MODEL = "hermes-agent"  # Used when routing through Hermes proxy


def load_env():
    """Load .env file into os.environ."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def get_api_config() -> tuple[str, str, str]:
    """Get API URL, auth header, and model.

    Priority:
      1. HERMES_API_URL env var (Hermes proxy — always reachable in sandbox network)
      2. NVIDIA_API_KEY env var (direct NVIDIA API — blocked in sandbox egress)
      3. inference.local fallback (NemoClaw internal, unreliable)
    """
    load_env()

    # Prefer Hermes proxy — reachable from cpb-app on the container network.
    # Hermes forwards to the configured Nemotron model internally.
    hermes_url = os.getenv("HERMES_API_URL", HERMES_API_URL)
    if hermes_url:
        return hermes_url, "Bearer hermes", HERMES_MODEL

    # Direct NVIDIA API (blocked by sandbox egress proxy, kept as fallback)
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if api_key:
        return NVIDIA_API_URL, f"Bearer {api_key}", MODEL

    # Last resort: inference.local proxy
    return INFERENCE_URL, "Bearer dummy", MODEL


SYSTEM_PROMPT = """You are an expert 3D printing engineer at The Custom Parts Bureau.
You analyze technical 3D print data and provide a detailed analysis response in JSON format.

JSON schema to follow:
{
  "explanation": "Under 150 words professional explanation for the customer. If rejecting, state the specific physical metrics violating limits. If conditional, mention structural risks. If accepted, note print suitability.",
  "decision_override": "ACCEPT", "CONDITIONAL", or "REJECT". You can override the recommended decision to a safer state (e.g. ACCEPT -> CONDITIONAL, or CONDITIONAL -> REJECT) if you see significant structural risk from the metrics. Otherwise, match the recommended decision.",
  "margin_surcharge_pct": 0.0,
  "suggested_repairs": "Suggest concrete steps the customer can take to repair the geometry if rejected or conditional. Or empty string if accepted."
}

Respond ONLY with the raw JSON block. Do not include markdown formatting or commentary outside the JSON."""

USER_PROMPT_TEMPLATE = """Analyze this technical analysis context:

RECOMMENDED DECISION: {decision}
PHYSICAL DATA:
- Volume: {volume:.2f} cm³ ({weight:.1f}g PLA)
- Bounding box: {bbox}
- Triangles: {triangles}
- Overhang: {overhang_pct:.1f}% of surface
- Min wall thickness: {min_wall:.3f} mm (tolerance: {tolerance:.1f} mm)
- Structural confidence: {confidence:.1f}/100

RULE REASONING:
{reasoning_text}

Analyze the structural validity and return the JSON object."""


def generate_reasoning(quote) -> dict:
    """
    Generate natural language reasoning for a 3D print quote.
    
    Args:
        quote: Quote dataclass or dict from quote_generator with analysis data
    
    Returns:
        dict with keys: explanation, decision_override, margin_surcharge_pct, suggested_repairs, model, usage, success
    """
    # Convert dataclass to dict if needed
    if hasattr(quote, "to_dict"):
        quote = quote.to_dict()
    
    api_url, api_key, model = get_api_config()
    
    # Extract data from quote
    analysis = quote.get("analysis_summary", {})
    reasoning_text = quote.get("reasoning_text", "No analysis available.")
    line_items = {item["label"]: item["cost_usd"] for item in quote.get("line_items", [])}
    
    # Build the user prompt
    wall = analysis.get("wall_thickness", {})
    overhang = analysis.get("overhang", {})
    bbox = analysis.get("bounding_box_mm", {})
    
    user_prompt = USER_PROMPT_TEMPLATE.format(
        decision=quote.get("decision", "UNKNOWN"),
        reasoning_text=reasoning_text,
        volume=analysis.get("volume_cm3", 0),
        weight=analysis.get("volume_cm3", 0) * 1.24,  # PLA density ~1.24 g/cm³
        bbox=f"{bbox.get('x', 0)} × {bbox.get('y', 0)} × {bbox.get('z', 0)} mm",
        triangles=analysis.get("triangle_count", 0),
        overhang_pct=overhang.get("percentage", 0),
        min_wall=wall.get("min_mm", 0),
        tolerance=wall.get("tolerance_mm", 0.8),
        confidence=analysis.get("structural_confidence", 0),
    )
    
    # Call Nemotron
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    
    try:
        response = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        
        content = data["choices"][0]["message"]["content"].strip()
        
        # Clean markdown wrappers if present
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()
            
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {
                "explanation": content,
                "decision_override": quote.get("decision", "ACCEPT"),
                "margin_surcharge_pct": 0.0,
                "suggested_repairs": ""
            }
            
        usage = data.get("usage", {})
        
        return {
            "explanation": parsed.get("explanation", ""),
            "decision_override": parsed.get("decision_override", quote.get("decision", "ACCEPT")),
            "margin_surcharge_pct": float(parsed.get("margin_surcharge_pct", 0.0) or 0.0),
            "suggested_repairs": parsed.get("suggested_repairs", ""),
            "model": model,
            "usage": usage,
            "success": True,
        }
    
    except requests.exceptions.RequestException as e:
        return {
            "explanation": f"[Nemotron API error: {e}]",
            "decision_override": quote.get("decision", "ACCEPT"),
            "margin_surcharge_pct": 0.0,
            "suggested_repairs": "",
            "model": model,
            "usage": {},
            "success": False,
            "error": str(e),
        }


CHAT_SYSTEM_PROMPT = """You are a professional assistant and business administrator for The Custom Parts Bureau.
You have access to the real-time jobs database of the 3D printing bureau.
Your task is to answer operator questions about the queue, active jobs, financial metrics (revenue, margins), materials, and structural reasons for any rejections.

Analyze the jobs database context carefully and respond to the query.
Be concise (under 100 words), technical, and direct. Do not decorate responses, keep it direct. Do not hallucinate data that is not in the context.
If a job was rejected, reference the physical reason (min wall thickness, watertight status, or overhang percentage) from the analysis details.

Current jobs data in the system:
{jobs_summary}
"""

def generate_chat_response(query: str, jobs: list[dict]) -> str:
    """
    Generate a dynamic operator assistant response using Nemotron 3 Ultra.
    """
    try:
        # Formulate jobs summary
        summary_lines = []
        for j in jobs:
            status = j.get("status", "unknown")
            decision = j.get("decision", "N/A")
            total = j.get("total_usd", 0.0) or 0.0
            margin_pct = j.get("margin_pct", 0.0) or 0.0
            filename = j.get("filename", "unknown")
            confidence = j.get("confidence", 0.0) or 0.0
            min_wall = j.get("min_wall_mm", 0.0) or 0.0
            overhang = j.get("overhang_pct", 0.0) or 0.0
            reasoning = j.get("nemotron_explanation") or j.get("reasoning_text") or ""
            
            line = (
                f"- Job {j.get('id')}: {filename} | Status: {status} | Decision: {decision} "
                f"| Price: ${total:.2f} | Margin: {margin_pct:.1f}% | Confidence: {confidence:.1f}% "
                f"| Min Wall: {min_wall:.2f}mm | Overhang: {overhang:.1f}%"
            )
            if reasoning:
                line += f" | Reasoning: {reasoning[:120]}..."
            summary_lines.append(line)
            
        jobs_summary = "\n".join(summary_lines) if summary_lines else "No jobs in the database currently."
        
        api_url, api_key, model = get_api_config()
        print(f"[CHAT AGENT] Routing to {api_url} model={model}")
        
        headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT.format(jobs_summary=jobs_summary)},
                {"role": "user", "content": query},
            ],
            "temperature": 0.2,
            "max_tokens": 300,
        }
        
        response = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
        
    except Exception as e:
        print(f"[CHAT AGENT] Error calling Nemotron: {e}")
        # Structured fallback — answers real questions from the live DB
        query_lower = query.lower()

        # --- Job listing / overview ---
        if any(kw in query_lower for kw in ("list", "all jobs", "which", "what jobs", "show")):
            if not jobs:
                return "No jobs in the database yet."
            lines = [f"{len(jobs)} job(s) in the system:"]
            for j in jobs:
                lines.append(
                    f"  \u2022 {j.get('id') or '?'} | {j.get('filename') or '?'} | {j.get('status') or '?'} | "
                    f"{j.get('decision') or 'N/A'} | ${j.get('total_usd') or 0.0:.2f} | "
                    f"confidence {j.get('confidence') or 0.0:.0f}%"
                )
            return "\n".join(lines)

        # --- Revenue ---
        if "revenue" in query_lower:
            paid = [j for j in jobs if j.get("status") in ("completed", "printing", "paid")]
            revenue = sum(j.get("total_usd", 0.0) or 0.0 for j in paid)
            return f"Revenue from {len(paid)} paid/active job(s): ${revenue:.2f}."

        # --- Margin ---
        if "margin" in query_lower:
            paid = [j for j in jobs if j.get("status") in ("completed", "printing", "paid")]
            margins = [j.get("margin_pct") or 0.0 for j in paid if (j.get("margin_pct") or 0) > 0]
            avg = sum(margins) / len(margins) if margins else 0.0
            lines = [f"Average margin: {avg:.1f}% across {len(margins)} job(s)."]
            for j in paid:
                lines.append(f"  \u2022 {j.get('filename') or '?'}: {j.get('margin_pct') or 0.0:.1f}%")
            return "\n".join(lines)

        # --- Queue / next to print ---
        if any(kw in query_lower for kw in ("queue", "next", "pending")):
            quoted = [j for j in jobs if j.get("status") == "quoted"]
            if not quoted:
                return "No jobs currently waiting in the quoted queue."
            j = quoted[0]
            return (
                f"Next in queue: {j.get('id') or '?'} ({j.get('filename') or '?'}) — "
                f"${j.get('total_usd') or 0.0:.2f}, decision {j.get('decision') or 'N/A'}, "
                f"confidence {j.get('confidence') or 0.0:.0f}%."
            )

        # --- Rejected jobs ---
        if "reject" in query_lower:
            rejected = [j for j in jobs if j.get("decision") == "REJECT" or j.get("status") == "rejected"]
            if not rejected:
                return "No rejected jobs in the database."
            lines = [f"{len(rejected)} rejected job(s):"]
            for j in rejected:
                reason = (j.get("reasoning_text") or "")[:120]
                lines.append(f"  • {j.get('id')} ({j.get('filename')}): {reason}")
            return "\n".join(lines)

        # --- Help ---
        if "help" in query_lower:
            return "Ask about: list jobs, revenue, margin, queue / next, rejected, or a specific filename."

        # --- Filename match ---
        for j in jobs:
            fn = j.get("filename", "").replace(".stl", "").lower()
            if fn and fn in query_lower:
                return (
                    f"{j.get('id') or '?'} ({j.get('filename') or '?'}): status={j.get('status') or '?'}, "
                    f"decision={j.get('decision') or 'N/A'}, total=${j.get('total_usd') or 0.0:.2f}, "
                    f"confidence={j.get('confidence') or 0.0:.0f}%, "
                    f"min wall={j.get('min_wall_mm') or 0.0:.2f}mm, overhang={j.get('overhang_pct') or 0.0:.1f}%."
                )

        # Generic overview when nothing matched
        lines = [f"[Fallback] {len(jobs)} job(s) on record. Quick summary:"]
        for j in jobs:
            lines.append(
                f"  \u2022 {j.get('id') or '?'} | {j.get('filename') or '?'} | {j.get('status') or '?'} | "
                f"{j.get('decision') or 'N/A'} | ${j.get('total_usd') or 0.0:.2f}"
            )
        lines.append("Try asking: list jobs, margin, revenue, queue, or a filename.")
        return "\n".join(lines)


# --- CLI Demo ---
if __name__ == "__main__":
    print("🧠 Nemotron 3 Ultra Reasoning Test")
    print("=" * 60)
    print(f"Environment: {'NemoClaw Sandbox' if IN_SANDBOX else 'Local'}")
    print(f"Endpoint: {INFERENCE_URL if IN_SANDBOX else NVIDIA_API_URL}")
    print(f"Model: {MODEL}")
    print()
    
    # Test quote
    test_quote = {
        "quote_id": "Q-TEST-001",
        "decision": "REJECTED",
        "total_usd": 0.0,
        "line_items": [],
        "reasoning_text": (
            "WALL THICKNESS WARNING\n"
            "Minimum wall thickness: 0.130 mm (tolerance: 0.8 mm). "
            "98% of samples below tolerance.\n\n"
            "STRUCTURAL CONFIDENCE: 22.0/100 — Critical. "
            "Model will likely fail during printing."
        ),
        "analysis_summary": {
            "volume_cm3": 0.80,
            "triangle_count": 24,
            "bounding_box_mm": {"x": 30.0, "y": 30.0, "z": 30.0},
            "overhang": {"percentage": 45.0},
            "wall_thickness": {"min_mm": 0.13, "tolerance_mm": 0.8},
            "structural_confidence": 22.0,
            "machine_hours": 0.1,
        },
    }
    
    print("📋 Test: REJECTED model (thin walls, low confidence)")
    print("   Calling Nemotron 3 Ultra...")
    print()
    
    result = generate_reasoning(test_quote)
    
    if result["success"]:
        print("✅ Nemotron response:")
        print("-" * 60)
        print(result["explanation"])
        print("-" * 60)
        print(f"\nTokens used: {result['usage']}")
    else:
        print(f"❌ Error: {result['error']}")
    
    print("\n" + "=" * 60)
    print("Nemotron integration ready! 🎉")
