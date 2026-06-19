#!/usr/bin/env python3
"""
Demo Script — Full analysis flow for The Custom Parts Bureau.

Accepts an STL file path as a command-line argument and runs the complete
pipeline: geometry analysis → cost estimation → quote generation → formatted
output showing all results including accept/reject decision with reasoning.

Usage:
    python3 demo.py <path/to/model.stl>

Part of "The Custom Parts Bureau" — Hermes Agent hackathon project.
"""

from __future__ import annotations

import sys
import json
from pathlib import Path

# Add project directory to path for imports
PROJECT_DIR = Path(__file__).parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from stl_analyzer import analyze_stl, GeometryAnalysis
from cost_estimator import estimate_cost, CostEstimate
from quote_generator import generate_quote, Quote, Decision


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

DIVIDER = "═" * 72
THIN_DIVIDER = "─" * 72
SECTION_WIDTH = 30


def _fmt_header(title: str) -> str:
    return f"\n{DIVIDER}\n  {title}\n{DIVIDER}"


def _fmt_section(title: str) -> str:
    return f"\n  {title}\n{THIN_DIVIDER}"


def _fmt_row(label: str, value: str, indent: int = 4) -> str:
    return f"{' ' * indent}{label:<{SECTION_WIDTH}} {value}"


def format_analysis(analysis: GeometryAnalysis) -> str:
    """Format geometry analysis results for terminal display."""
    lines = []
    lines.append(_fmt_header("📐 GEOMETRY ANALYSIS"))
    lines.append(_fmt_section("File Information"))
    lines.append(_fmt_row("Filename:", analysis.filename))
    lines.append(_fmt_row("Triangle count:", f"{analysis.triangle_count:,}"))

    lines.append(_fmt_section("Dimensions"))
    bbox = analysis.bounding_box
    lines.append(_fmt_row("Bounding box:", str(bbox)))
    lines.append(_fmt_row("Volume:", f"{analysis.volume_cm3:.2f} cm³"))
    lines.append(_fmt_row("Surface area:", f"{analysis.surface_area_cm2:.2f} cm²"))

    lines.append(_fmt_section("Mesh Integrity"))
    integ = analysis.integrity
    lines.append(_fmt_row("Watertight:", "✅ Yes" if integ.is_watertight else "❌ No"))
    lines.append(_fmt_row("Winding consistent:", "✅ Yes" if integ.is_winding_consistent else "❌ No"))
    lines.append(_fmt_row("Degenerate faces:", str(integ.num_degenerate_faces)))
    lines.append(_fmt_row("Separate bodies:", str(integ.num_bodies)))

    lines.append(_fmt_section("Overhang Analysis"))
    oh = analysis.overhang
    overhang_icon = "🔴" if oh.overhang_percentage > 20 else ("🟡" if oh.overhang_percentage > 5 else "🟢")
    lines.append(_fmt_row("Overhang threshold:", f"{oh.overhang_angle_threshold_deg}°"))
    lines.append(_fmt_row("Overhang area:", f"{oh.overhang_area_cm2:.2f} cm²"))
    lines.append(_fmt_row("Overhang %:", f"{overhang_icon} {oh.overhang_percentage:.1f}%"))
    lines.append(_fmt_row("Overhang faces:", f"{oh.num_overhang_faces:,} / {oh.total_faces:,}"))

    lines.append(_fmt_section("Wall Thickness"))
    wt = analysis.wall_thickness
    if wt.samples_taken > 0:
        lines.append(_fmt_row("Samples taken:", str(wt.samples_taken)))
        lines.append(_fmt_row("Minimum:", f"{wt.min_thickness_mm:.3f} mm"))
        lines.append(_fmt_row("Average:", f"{wt.avg_thickness_mm:.3f} mm"))
        lines.append(_fmt_row("Maximum:", f"{wt.max_thickness_mm:.3f} mm"))
        below_icon = "🔴" if wt.samples_below_tolerance > 0 else "🟢"
        lines.append(_fmt_row("Below tolerance:", f"{below_icon} {wt.samples_below_tolerance}/{wt.samples_taken} samples"))
        lines.append(_fmt_row("Tolerance:", f"{wt.tolerance_mm:.1f} mm"))
    else:
        lines.append(_fmt_row("Samples:", "0 (could not cast rays)"))

    lines.append(_fmt_section("Structural Confidence"))
    conf = analysis.structural_confidence
    if conf >= 70:
        conf_bar = "🟢"
    elif conf >= 30:
        conf_bar = "🟡"
    else:
        conf_bar = "🔴"
    lines.append(_fmt_row("Score:", f"{conf_bar} {conf} / 100"))

    return "\n".join(lines)


def format_cost_estimate(estimate: CostEstimate) -> str:
    """Format cost breakdown for terminal display."""
    lines = []
    lines.append(_fmt_header("💰 COST ESTIMATE"))

    bd = estimate.breakdown
    ctx = estimate.context

    lines.append(_fmt_row("Material (PLA):", f"${bd.material_cost_usd:.2f}"))
    lines.append(_fmt_row("Machine time:", f"${bd.machine_cost_usd:.2f} (~{ctx.estimated_print_time_hours:.1f} hrs)"))
    lines.append(_fmt_row("Support material:", f"${bd.support_cost_usd:.2f}"))
    lines.append(f"{'─' * (SECTION_WIDTH + 20)}")
    lines.append(_fmt_row("Subtotal:", f"${bd.subtotal_usd:.2f}"))

    margin_label = f"Margin ({bd.margin_pct:.0f}%):"
    margin_detail = "confidence-adjusted" if ctx.confidence_adjusted_margin else "standard"
    lines.append(_fmt_row(margin_label, f"${bd.margin_amount_usd:.2f} ({margin_detail})"))
    lines.append(f"{'═' * (SECTION_WIDTH + 20)}")
    lines.append(_fmt_row("TOTAL:", f"${bd.total_usd:.2f}"))
    lines.append(_fmt_row("Estimated material weight:", f"{ctx.material_grams_estimated:.1f}g PLA"))

    return "\n".join(lines)


def format_decision(quote: Quote) -> str:
    """Format the accept/reject decision for terminal display."""
    lines = []
    lines.append(_fmt_header("📋 QUOTE DECISION"))

    decision_icons = {
        Decision.ACCEPT: "✅ ACCEPTED",
        Decision.CONDITIONAL: "⚠️  CONDITIONALLY ACCEPTED",
        Decision.REJECT: "❌ REJECTED",
    }
    lines.append(f"\n  {decision_icons[quote.decision]}")
    lines.append(f"  Quote ID: {quote.quote_id}")
    lines.append(f"\n  Reason: {quote.decision_reason}")

    return "\n".join(lines)


def format_reasoning(quote: Quote) -> str:
    """Format the reasoning text for terminal display."""
    lines = []
    lines.append(_fmt_header("🧠 PHYSICAL REASONING"))
    lines.append("")
    # Indent each paragraph
    for paragraph in quote.reasoning_text.split("\n\n"):
        for line in paragraph.split("\n"):
            lines.append(f"  {line}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(stl_path: str) -> None:
    """Run the full analysis pipeline on an STL file."""
    print(f"\n{'🔬 THE CUSTOM PARTS BUREAU — STL Analysis Pipeline':^72}")
    print(f"{'Hermes Agent | NVIDIA/Stripe/Nous Research Hackathon':^72}\n")

    # --- Step 1: Geometry Analysis ---
    print("  ⏳ Analyzing geometry...")
    try:
        analysis = analyze_stl(stl_path)
    except FileNotFoundError as e:
        print(f"\n  ❌ Error: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"\n  ❌ Error: {e}")
        sys.exit(1)

    print(format_analysis(analysis))

    # --- Step 2: Cost Estimation ---
    print("\n  ⏳ Estimating costs...")
    estimate = estimate_cost(analysis)
    print(format_cost_estimate(estimate))

    # --- Step 3: Quote Generation ---
    print("\n  ⏳ Generating quote...")
    quote = generate_quote(analysis, estimate)
    print(format_decision(quote))

    # --- Step 4: Reasoning ---
    print(format_reasoning(quote))

    # --- Step 5: JSON output ---
    print(DIVIDER)
    print("  📦 JSON OUTPUT (for API integration)")
    print(DIVIDER)
    print(quote.to_json())

    # --- Step 6: Nemotron prompt ---
    print(f"\n{DIVIDER}")
    print("  🤖 NEMOTRON 3 ULTRA PROMPT")
    print(DIVIDER)
    print(f"\n{quote.nemotron_prompt}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <path/to/model.stl>")
        print(f"\nExample:")
        print(f"  python3 {sys.argv[0]} test_stl/cube.stl")
        print(f"  python3 {sys.argv[0]} test_stl/overhang_test.stl")
        print(f"  python3 {sys.argv[0]} test_stl/thin_walls.stl")
        sys.exit(1)

    stl_path = sys.argv[1]
    main(stl_path)
