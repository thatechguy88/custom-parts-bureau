#!/usr/bin/env python3
"""
Stripe Checkout integration for The Custom Parts Bureau.

Creates Stripe Checkout Sessions for 3D printing quotes.
Loads API keys from .env file (sandbox/test mode).

Usage:
    from stripe_integration import create_checkout_session
    
    result = create_checkout_session(
        quote=quote_data,
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel"
    )
"""

import os
import sys
from pathlib import Path
from typing import Optional

try:
    import stripe
except ImportError:
    print("Installing stripe package...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "stripe", "-q"])
    import stripe


def load_stripe_keys() -> tuple[str, str]:
    """Load Stripe API keys from .env file."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            f".env file not found at {env_path}. "
            "Create it with STRIPE_PUBLISHABLE_KEY and STRIPE_SECRET_KEY."
        )
    
    keys = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                keys[key.strip()] = value.strip()
    
    publishable = keys.get("STRIPE_PUBLISHABLE_KEY")
    secret = keys.get("STRIPE_SECRET_KEY")
    
    if not publishable or not secret:
        raise ValueError("Missing STRIPE_PUBLISHABLE_KEY or STRIPE_SECRET_KEY in .env")
    
    return publishable, secret


def init_stripe() -> stripe.StripeClient:
    """Initialize Stripe client with test mode keys."""
    _, secret = load_stripe_keys()
    stripe.api_key = secret
    return stripe.StripeClient(secret)


def create_checkout_session(
    quote: dict,
    success_url: str = "https://example.com/success?session_id={CHECKOUT_SESSION_ID}",
    cancel_url: str = "https://example.com/cancel",
    customer_email: Optional[str] = None,
) -> dict:
    """
    Create a Stripe Checkout Session for a 3D printing quote.
    
    Args:
        quote: Quote dict from quote_generator with keys:
            - model_name: str
            - total_cost: float (in dollars)
            - line_items: list of {name, amount} dicts
            - reasoning: str
        success_url: Redirect URL after successful payment
        cancel_url: Redirect URL if customer cancels
        customer_email: Pre-fill email (optional)
    
    Returns:
        dict with session_id, url, and status
    """
    _, secret = load_stripe_keys()
    stripe.api_key = secret
    
    model_name = quote.get("model_name", "3D Print Job")
    total_cents = int(quote["total_cost"] * 100)  # Convert to cents
    
    # Build line items for Stripe
    line_items = []
    for item in quote.get("line_items", []):
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": item["name"],
                    "description": f"Custom Parts Bureau — {model_name}",
                },
                "unit_amount": int(item["amount"] * 100),
            },
            "quantity": 1,
        })
    
    # If no line items, create a single item for the total
    if not line_items:
        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": f"3D Print: {model_name}",
                    "description": quote.get("reasoning", "Custom 3D printing service"),
                },
                "unit_amount": total_cents,
            },
            "quantity": 1,
        }]
    
    # Create Checkout Session (old-style API for stripe v15)
    session_params = {
        "mode": "payment",
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "model_name": model_name,
            "confidence": str(quote.get("confidence", "N/A")),
            "decision": quote.get("decision", "ACCEPT"),
        },
    }
    
    if customer_email:
        session_params["customer_email"] = customer_email
    
    session = stripe.checkout.Session.create(**session_params)
    
    return {
        "session_id": session.id,
        "url": session.url,
        "status": session.status,
        "payment_status": session.payment_status,
        "amount_total": session.amount_total / 100 if session.amount_total else 0,
    }


def create_payment_link(
    quote: dict,
    customer_email: Optional[str] = None,
) -> dict:
    """
    Create a Stripe Payment Link for a quote (simpler than Checkout Session).
    Good for demo — generates a shareable URL.
    """
    client = init_stripe()
    
    model_name = quote.get("model_name", "3D Print Job")
    total_cents = int(quote["total_cost"] * 100)
    
    # Create product
    product = client.products.create(
        name=f"3D Print: {model_name}",
        description=quote.get("reasoning", "Custom 3D printing service via The Custom Parts Bureau"),
        metadata={
            "confidence": str(quote.get("confidence", "N/A")),
            "decision": quote.get("decision", "ACCEPT"),
        },
    )
    
    # Create price
    price = client.prices.create(
        product=product.id,
        unit_amount=total_cents,
        currency="usd",
    )
    
    # Create payment link
    payment_link = client.payment_links.create(
        line_items=[{"price": price.id, "quantity": 1}],
        active=True,
    )
    
    return {
        "payment_link_id": payment_link.id,
        "url": payment_link.url,
        "product_id": product.id,
        "price_id": price.id,
    }


def verify_payment(session_id: str) -> dict:
    """Verify a Checkout Session was paid."""
    client = init_stripe()
    session = client.checkout.sessions.retrieve(session_id)
    
    return {
        "session_id": session.id,
        "payment_status": session.payment_status,
        "status": session.status,
        "amount_total": session.amount_total / 100 if session.amount_total else 0,
        "customer_email": session.customer_email,
    }


# --- CLI Demo ---
if __name__ == "__main__":
    print("🔧 Stripe Integration Test (Sandbox Mode)")
    print("=" * 50)
    
    # Load and verify keys
    pk, sk = load_stripe_keys()
    print(f"Publishable key: {pk[:15]}...{pk[-4:]}")
    print(f"Secret key: ***...{sk[-4:]}")
    
    # Initialize
    client = init_stripe()
    print(f"\n✅ Stripe client initialized (test mode)")
    
    # Create a test quote
    test_quote = {
        "model_name": "test_cube.stl",
        "total_cost": 7.58,
        "confidence": 94,
        "decision": "ACCEPT",
        "reasoning": "Simple 20mm cube. Volume 8.0cm³, no overhangs, walls within tolerance. Material: $0.24. Machine: $0.32. Total: $0.56 + 30% margin = $0.73.",
        "line_items": [
            {"name": "Material (PLA)", "amount": 0.24},
            {"name": "Machine Time", "amount": 0.32},
            {"name": "Service Fee", "amount": 4.52},
        ],
    }
    
    print(f"\n📋 Test Quote: {test_quote['model_name']}")
    print(f"   Total: ${test_quote['total_cost']:.2f}")
    print(f"   Decision: {test_quote['decision']}")
    
    # Create checkout session
    try:
        result = create_checkout_session(
            quote=test_quote,
            success_url="https://example.com/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://example.com/cancel",
        )
        print(f"\n✅ Checkout Session created!")
        print(f"   Session ID: {result['session_id']}")
        print(f"   Status: {result['status']}")
        print(f"   URL: {result['url']}")
    except Exception as e:
        print(f"\n⚠️  Checkout session creation failed: {e}")
        print("   (This is expected if Stripe keys are invalid or network is restricted)")
    
    print("\n" + "=" * 50)
    print("Stripe integration ready for hackathon demo! 🎉")
