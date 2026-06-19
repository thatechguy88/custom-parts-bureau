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
MODEL = "nvidia/nemotron-3-ultra-550b-a55b"  # The big one for reasoning


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
    """Get API URL, key, and model based on environment."""
    load_env()
    
    if IN_SANDBOX:
        # Inside NemoClaw: try NVIDIA API directly (inference.local doesn't resolve via Python requests)
        api_key = os.getenv("NVIDIA_API_KEY", "")
        if api_key:
            return NVIDIA_API_URL, f"Bearer {api_key}", MODEL
        # Fallback to inference proxy
        return INFERENCE_URL, "Bearer dummy", MODEL
    else:
        # Outside sandbox: use NVIDIA API directly
        api_key = os.getenv("NVIDIA_API_KEY", "")
        if not api_key:
            raise ValueError(
                "NVIDIA_API_KEY not set. Get one from https://build.nvidia.com"
            )
        return NVIDIA_API_URL, f"Bearer {api_key}", MODEL


SYSTEM_PROMPT = """You are a professional 3D printing engineer at The Custom Parts Bureau.

Your role is to analyze technical 3D print data and provide clear, professional explanations for customers. You should:

1. Translate technical metrics into plain language
2. Be specific about risks and why they matter
3. Offer practical solutions when possible
4. Be honest — if something is unprintable, say so clearly
5. Keep explanations under 200 words

Tone: Professional but approachable. Like a trusted engineer explaining to a client."""

USER_PROMPT_TEMPLATE = """Analyze this 3D print quote and provide a professional explanation for the customer.

DECISION: {decision}
The model has been {decision_lower}.

TECHNICAL ANALYSIS:
{reasoning_text}

COST ESTIMATE:
- Material: ${material:.2f}
- Machine time: ${machine:.2f} (est. {hours:.1f} hours)
- Support material: ${support:.2f}
- Total: ${total:.2f}

MODEL METRICS:
- Volume: {volume:.2f} cm³ ({weight:.1f}g PLA)
- Bounding box: {bbox}
- Triangles: {triangles}
- Overhang: {overhang_pct:.1f}% of surface
- Min wall thickness: {min_wall:.3f} mm (tolerance: {tolerance:.1f} mm)
- Structural confidence: {confidence:.1f}/100

Write a clear, professional explanation for the customer.
If rejecting, explain specifically what's wrong and why it can't be printed.
If conditionally accepting, explain the risks and what the customer should know.
If accepting, highlight what makes this a good print.
Keep it under 200 words."""


def generate_reasoning(quote) -> dict:
    """
    Generate natural language reasoning for a 3D print quote.
    
    Args:
        quote: Quote dataclass or dict from quote_generator with analysis data
    
    Returns:
        dict with "explanation" (str), "model" (str), "usage" (dict)
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
    integrity = analysis.get("integrity", {})
    bbox = analysis.get("bounding_box_mm", {})
    
    user_prompt = USER_PROMPT_TEMPLATE.format(
        decision=quote.get("decision", "UNKNOWN"),
        decision_lower=quote.get("decision", "unknown").lower(),
        reasoning_text=reasoning_text,
        material=line_items.get("PLA Filament", 0),
        machine=line_items.get("Machine Time", 0),
        hours=analysis.get("machine_hours", 0),
        support=line_items.get("Support Material", 0),
        total=quote.get("total_usd", 0),
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
        "temperature": 0.3,
        "max_tokens": 500,
    }
    
    try:
        response = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        
        explanation = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        
        return {
            "explanation": explanation,
            "model": model,
            "usage": usage,
            "success": True,
        }
    
    except requests.exceptions.RequestException as e:
        return {
            "explanation": f"[Nemotron API error: {e}]",
            "model": model,
            "usage": {},
            "success": False,
            "error": str(e),
        }


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
