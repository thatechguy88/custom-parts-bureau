"""
Quote Generator — Structured quote output for The Custom Parts Bureau.

Generates machine-readable (JSON) and human-readable quotes from geometry
analysis and cost estimation. Includes reasoning text optimised for
Nemotron 3 Ultra to expand into natural language.

Part of "The Custom Parts Bureau" — Hermes Agent hackathon project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from stl_analyzer import GeometryAnalysis
from cost_estimator import CostEstimate, estimate_cost


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum structural confidence to ACCEPT a job (below = REJECT)
MIN_CONFIDENCE_ACCEPT: float = 30.0
# Maximum structural confidence for automatic accept
AUTO_ACCEPT_CONFIDENCE: float = 70.0
# If below this, the model is fundamentally broken
REJECT_CONFIDENCE: float = 20.0


class Decision(str, Enum):
    ACCEPT = "ACCEPT"
    CONDITIONAL = "CONDITIONAL"
    REJECT = "REJECT"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class QuoteLineItem:
    """A single line in the quote."""
    label: str
    detail: str
    cost_usd: float
    unit: str = ""


@dataclass
class Quote:
    """A complete customer-facing quote."""
    quote_id: str = ""
    timestamp: str = ""
    filename: str = ""
    decision: Decision = Decision.ACCEPT
    decision_reason: str = ""
    line_items: list[QuoteLineItem] = field(default_factory=list)
    total_usd: float = 0.0
    confidence: float = 0.0
    reasoning_text: str = ""
    nemotron_prompt: str = ""
    analysis_summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise to a dict suitable for JSON API integration."""
        return {
            "quote_id": self.quote_id,
            "timestamp": self.timestamp,
            "filename": self.filename,
            "decision": self.decision.value,
            "decision_reason": self.decision_reason,
            "line_items": [
                {"label": li.label, "detail": li.detail, "cost_usd": round(li.cost_usd, 4), "unit": li.unit}
                for li in self.line_items
            ],
            "total_usd": round(self.total_usd, 4),
            "confidence": round(self.confidence, 1),
            "reasoning_text": self.reasoning_text,
            "analysis_summary": self.analysis_summary,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Reasoning text generators
# ---------------------------------------------------------------------------

def _build_reasoning_text(analysis: GeometryAnalysis, estimate: CostEstimate) -> str:
    """Build detailed reasoning text for Nemotron to expand.

    This text is structured as a technical analysis report that an LLM
    can naturally expand into conversational language. Each section
    provides concrete data points and physical reasoning.
    """
    sections = []

    # --- Geometry overview ---
    bbox = analysis.bounding_box
    sections.append(
        f"GEOMETRY ANALYSIS\n"
        f"The model '{analysis.filename}' has a volume of {analysis.volume_cm3:.2f} cm³ "
        f"and a surface area of {analysis.surface_area_cm2:.2f} cm². "
        f"The bounding box is {bbox} "
        f"with {analysis.triangle_count:,} triangles in the mesh."
    )

    # --- Integrity ---
    integrity = analysis.integrity
    integrity_items = []
    if not integrity.is_watertight:
        integrity_items.append("the mesh is NOT watertight (has holes or gaps)")
    if not integrity.is_winding_consistent:
        integrity_items.append("face winding is inconsistent (inverted normals)")
    if integrity.num_degenerate_faces > 0:
        integrity_items.append(f"there are {integrity.num_degenerate_faces} degenerate (zero-area) faces")
    if integrity.num_bodies > 1:
        integrity_items.append(f"the mesh contains {integrity.num_bodies} separate bodies")

    if integrity_items:
        sections.append(
            f"INTEGRITY ISSUES\n"
            f"The mesh has structural integrity problems: {'; '.join(integrity_items)}. "
            f"These issues may cause slicing errors, failed prints, or weak spots."
        )
    else:
        sections.append(
            "INTEGRITY: The mesh passes all integrity checks — watertight, "
            "consistent winding, no degenerate faces, single body."
        )

    # --- Overhang analysis ---
    oh = analysis.overhang
    if oh.overhang_percentage > 20:
        sections.append(
            f"OVERHANG WARNING\n"
            f"Overhang analysis reveals {oh.overhang_percentage:.1f}% of the surface area "
            f"({oh.overhang_area_cm2:.2f} cm²) exceeds the {oh.overhang_angle_threshold_deg}° "
            f"overhang threshold. This means {oh.num_overhang_faces:,} out of "
            f"{oh.total_faces:,} faces are oriented at steep downward angles. "
            f"This will require extensive support material, significantly increasing "
            f"print time, material waste, and post-processing effort."
        )
    elif oh.overhang_percentage > 5:
        sections.append(
            f"OVERHANG NOTE\n"
            f"Overhang analysis shows {oh.overhang_percentage:.1f}% of the surface area "
            f"exceeds the {oh.overhang_angle_threshold_deg}° threshold. "
            f"Some support material will be needed."
        )
    else:
        sections.append(
            f"OVERHANG: Only {oh.overhang_percentage:.1f}% of the surface exceeds "
            f"the {oh.overhang_angle_threshold_deg}° overhang threshold — "
            f"minimal to no support material required."
        )

    # --- Wall thickness ---
    wt = analysis.wall_thickness
    if wt.samples_taken > 0:
        below = wt.samples_below_tolerance
        total = wt.samples_taken
        pct_below = (below / total * 100) if total > 0 else 0

        if below > 0:
            sections.append(
                f"WALL THICKNESS WARNING\n"
                f"Wall thickness estimation (from {total} ray-cast samples) found that "
                f"{below}/{total} samples ({pct_below:.0f}%) fall below the minimum "
                f"tolerance of {wt.tolerance_mm:.1f} mm. "
                f"Minimum measured thickness: {wt.min_thickness_mm:.3f} mm. "
                f"Average thickness: {wt.avg_thickness_mm:.3f} mm. "
                f"Parts with walls thinner than {wt.tolerance_mm:.1f} mm are structurally "
                f"compromised — they may crack, warp, or collapse under normal handling loads."
            )
        else:
            sections.append(
                f"WALL THICKNESS: All {total} sampled locations meet the minimum "
                f"tolerance of {wt.tolerance_mm:.1f} mm. "
                f"Minimum: {wt.min_thickness_mm:.3f} mm, Average: {wt.avg_thickness_mm:.3f} mm."
            )

    # --- Structural confidence ---
    conf = analysis.structural_confidence
    if conf < REJECT_CONFIDENCE:
        confidence_verdict = (
            f"The structural confidence score is {conf}/100 — critically low. "
            f"This model has fundamental geometry problems that make it "
            f"unprintable or non-functional as designed."
        )
    elif conf < MIN_CONFIDENCE_ACCEPT:
        confidence_verdict = (
            f"The structural confidence score is {conf}/100 — below acceptable "
            f"threshold. The model requires significant redesign before printing."
        )
    elif conf < AUTO_ACCEPT_CONFIDENCE:
        confidence_verdict = (
            f"The structural confidence score is {conf}/100 — marginal. "
            f"The model can be printed with caveats regarding structural integrity."
        )
    else:
        confidence_verdict = (
            f"The structural confidence score is {conf}/100 — good. "
            f"The model geometry is suitable for reliable 3D printing."
        )

    sections.append(f"STRUCTURAL CONFIDENCE\n{confidence_verdict}")

    return "\n\n".join(sections)


def _build_nemotron_prompt(
    analysis: GeometryAnalysis,
    estimate: CostEstimate,
    decision: Decision,
    reasoning_text: str,
) -> str:
    """Build a prompt for Nemotron 3 Ultra to expand into natural language.

    This gives Nemotron the technical data and asks it to explain the
    decision in a way a customer would understand.
    """
    bbox = analysis.bounding_box
    oh = analysis.overhang
    wt = analysis.wall_thickness

    decision_instructions = {
        Decision.ACCEPT: (
            "The model has been ACCEPTED for printing. Explain what the model is, "
            "what it costs, and why it's a good candidate for 3D printing. "
            "Be encouraging and professional."
        ),
        Decision.CONDITIONAL: (
            "The model has been CONDITIONALLY accepted. Explain the concerns, "
            "what might go wrong, and what the customer should consider before "
            "proceeding. Be honest about risks but offer solutions if possible."
        ),
        Decision.REJECT: (
            "The model has been REJECTED for printing. Explain clearly and "
            "specifically why it cannot be printed reliably. Reference the "
            "physical measurements (wall thickness, overhangs, integrity). "
            "Suggest concrete improvements the customer could make. "
            "Be firm but helpful — this is a critical moment in the demo."
        ),
    }

    prompt = f"""You are a professional 3D printing engineer at The Custom Parts Bureau.
You have analyzed a customer's STL file and must explain the result.

DECISION: {decision.value}
{decision_instructions[decision]}

TECHNICAL ANALYSIS:
{reasoning_text}

COST ESTIMATE:
- Material: ${estimate.breakdown.material_cost_usd:.2f}
- Machine time: ${estimate.breakdown.machine_cost_usd:.2f} (est. {estimate.context.estimated_print_time_hours:.1f} hours)
- Support material: ${estimate.breakdown.support_cost_usd:.2f}
- Subtotal: ${estimate.breakdown.subtotal_usd:.2f}
- Margin ({estimate.breakdown.margin_pct:.0f}%): ${estimate.breakdown.margin_amount_usd:.2f}
- Total: ${estimate.breakdown.total_usd:.2f}

MODEL METRICS:
- Volume: {analysis.volume_cm3:.2f} cm³ ({estimate.context.material_grams_estimated:.1f}g PLA)
- Bounding box: {bbox}
- Triangles: {analysis.triangle_count:,}
- Overhang: {oh.overhang_percentage:.1f}% of surface
- Min wall thickness: {wt.min_thickness_mm:.3f} mm (tolerance: {wt.tolerance_mm:.1f} mm)
- Structural confidence: {analysis.structural_confidence}/100

Write a clear, professional explanation of this analysis for the customer.
If rejecting, the physical reasoning must be specific and convincing.
Keep it under 200 words.
"""
    return prompt


# ---------------------------------------------------------------------------
# Quote generation
# ---------------------------------------------------------------------------

def generate_quote(
    analysis: GeometryAnalysis,
    material: str = "PLA",
    rush: bool = False,
    estimate: Optional[CostEstimate] = None,
    min_confidence: float = MIN_CONFIDENCE_ACCEPT,
    auto_accept_confidence: float = AUTO_ACCEPT_CONFIDENCE,
    reject_confidence: float = REJECT_CONFIDENCE,
) -> Quote:
    """Generate a complete quote from analysis and cost data.

    Parameters
    ----------
    analysis : GeometryAnalysis
        Geometry analysis output.
    material : str
        Material type (e.g. PLA, PETG, ABS).
    rush : bool
        If True, apply 1.5x rush order surcharge.
    estimate : CostEstimate, optional
        Pre-computed cost estimate. If None, one will be generated.
    min_confidence : float
        Minimum confidence to accept.
    auto_accept_confidence : float
        Confidence above which the job is auto-accepted.
    reject_confidence : float
        Confidence below which the job is hard-rejected.

    Returns
    -------
    Quote
        Complete quote with decision, costs, and reasoning.
    """
    if estimate is None:
        estimate = estimate_cost(analysis, material=material, rush=rush)

    # --- Determine decision ---
    confidence = analysis.structural_confidence

    if confidence < reject_confidence:
        decision = Decision.REJECT
    elif confidence < min_confidence:
        decision = Decision.REJECT
    elif confidence < auto_accept_confidence:
        decision = Decision.CONDITIONAL
    else:
        decision = Decision.ACCEPT

    # --- Decision reason ---
    decision_reasons = {
        Decision.ACCEPT: (
            f"Model meets all structural requirements with a confidence score "
            f"of {confidence}/100. Ready for production printing."
        ),
        Decision.CONDITIONAL: (
            f"Model has a marginal confidence score of {confidence}/100. "
            f"Printing is possible but carries risk of defects. "
            f"Customer accepts responsibility for structural limitations."
        ),
        Decision.REJECT: _build_reject_reason(analysis),
    }

    # --- Line items ---
    bd = estimate.breakdown
    
    # Calculate effective filament price for display
    eff_filament_price = bd.material_cost_usd / analysis.volume_cm3 if analysis.volume_cm3 > 0 else 0.0

    line_items = [
        QuoteLineItem(
            label=f"{estimate.context.material_type} Filament",
            detail=f"{analysis.volume_cm3:.2f} cm³ × ${eff_filament_price:.2f}/cm³",
            cost_usd=bd.material_cost_usd,
            unit=f"{analysis.volume_cm3:.2f} cm³",
        ),
        QuoteLineItem(
            label="Machine Time",
            detail=f"~{estimate.context.estimated_print_time_hours:.1f} hrs × ${DEFAULT_MACHINE_RATE_PER_HOUR}/hr",
            cost_usd=bd.machine_cost_usd,
            unit=f"{estimate.context.estimated_print_time_hours:.1f} hrs",
        ),
        QuoteLineItem(
            label="Support Material",
            detail=f"Overhang support for {analysis.overhang.overhang_percentage:.1f}% overhang area",
            cost_usd=bd.support_cost_usd,
            unit="cm³",
        ),
        QuoteLineItem(
            label="Subtotal",
            detail="Material + Machine + Support",
            cost_usd=bd.subtotal_usd,
        ),
        QuoteLineItem(
            label=f"Margin ({bd.margin_pct:.0f}%)",
            detail=(
                f"{'Confidence-adjusted risk margin' if estimate.context.confidence_adjusted_margin else 'Standard margin'}"
            ),
            cost_usd=bd.margin_amount_usd,
        ),
    ]
    
    if estimate.context.is_rush_order:
        line_items.append(
            QuoteLineItem(
                label="Rush Order Surcharge",
                detail="1.5x expedited pricing",
                cost_usd=bd.rush_surcharge_usd,
            )
        )

    # --- Build quote ---
    now = datetime.now(timezone.utc)
    quote_id = f"Q-{now.strftime('%Y%m%d')}-{analysis.filename[:8].upper()}"

    reasoning_text = _build_reasoning_text(analysis, estimate)
    nemotron_prompt = _build_nemotron_prompt(analysis, estimate, decision, reasoning_text)

    return Quote(
        quote_id=quote_id,
        timestamp=now.isoformat(),
        filename=analysis.filename,
        decision=decision,
        decision_reason=decision_reasons[decision],
        line_items=line_items,
        total_usd=bd.total_usd,
        confidence=confidence,
        reasoning_text=reasoning_text,
        nemotron_prompt=nemotron_prompt,
        analysis_summary=analysis.to_dict(),
    )


def _build_reject_reason(analysis: GeometryAnalysis) -> str:
    """Build a specific rejection reason based on analysis data."""
    reasons = []
    wt = analysis.wall_thickness
    oh = analysis.overhang
    integrity = analysis.integrity

    if wt.min_thickness_mm < wt.tolerance_mm and wt.samples_taken > 0:
        reasons.append(
            f"Critical wall thickness violation: minimum measured wall is "
            f"{wt.min_thickness_mm:.3f} mm, below the {wt.tolerance_mm:.1f} mm "
            f"tolerance threshold"
        )

    if oh.overhang_percentage > 25:
        reasons.append(
            f"Excessive overhang area: {oh.overhang_percentage:.1f}% of the surface "
            f"exceeds {oh.overhang_angle_threshold_deg}° — requires impractical "
            f"amounts of support material"
        )

    if not integrity.is_watertight:
        reasons.append("Mesh is not watertight — cannot be sliced reliably")

    if not integrity.is_winding_consistent:
        reasons.append("Inconsistent face winding — normals are inverted in places")

    if integrity.num_bodies > 1:
        reasons.append(f"Mesh contains {integrity.num_bodies} disconnected bodies")

    if not reasons:
        reasons.append(
            f"Structural confidence score of {analysis.structural_confidence}/100 "
            f"is below the minimum threshold"
        )

    return "REJECTED: " + "; ".join(reasons)


# Keep default accessible for line items
from cost_estimator import (
    DEFAULT_FILAMENT_PRICE_PER_CM3,
    DEFAULT_MACHINE_RATE_PER_HOUR,
)
