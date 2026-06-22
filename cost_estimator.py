"""
Cost Estimator — 3D printing cost calculation module.

Estimates material, machine, and support costs based on geometry analysis.
Implements confidence-based dynamic pricing.

Part of "The Custom Parts Bureau" — Hermes Agent hackathon project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from stl_analyzer import GeometryAnalysis


# ---------------------------------------------------------------------------
# Default pricing parameters (USD)
# ---------------------------------------------------------------------------
MATERIAL_PRICING = {
    "PLA": 0.03,
    "ABS": 0.04,
    "PETG": 0.05,
    "SLA resin": 0.08,
}
DEFAULT_FILAMENT_PRICE_PER_CM3: float = MATERIAL_PRICING["PLA"]
DEFAULT_MACHINE_RATE_PER_HOUR: float = 4.00          # $4/hr
DEFAULT_SUPPORT_MATERIAL_PRICE_PER_CM3: float = 0.05 # $0.05/cm³
DEFAULT_BASE_MARGIN: float = 0.30                    # 30%
DEFAULT_ESTIMATED_PRINT_SPEED_CM3_PER_HOUR: float = 12.0  # ~12 cm³/hr typical
SUPPORT_VOLUME_FRACTION: float = 0.15  # assume supports ≈ 15% of overhang volume


def recommend_material(analysis: GeometryAnalysis) -> str:
    """Suggest best material based on geometry."""
    wt = analysis.wall_thickness
    # Fine details (< 1mm features) -> SLA resin
    if wt.min_thickness_mm > 0 and wt.min_thickness_mm < 1.0:
        return "SLA resin"
    
    # High temp resistance needed -> suggest ABS (heuristic based on filename for demo)
    name = analysis.filename.lower()
    if any(kw in name for kw in ["engine", "exhaust", "mount", "bracket", "heat"]):
        return "ABS"
        
    # General purpose -> PLA
    return "PLA"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CostBreakdown:
    """Itemised cost breakdown for a 3D print job."""
    material_cost_usd: float = 0.0
    machine_cost_usd: float = 0.0
    support_cost_usd: float = 0.0
    subtotal_usd: float = 0.0
    margin_pct: float = 0.0
    margin_amount_usd: float = 0.0
    rush_surcharge_usd: float = 0.0
    total_usd: float = 0.0

    @property
    def material_usd(self) -> float:
        return self.material_cost_usd

    def to_dict(self) -> dict:
        return {
            "material_cost_usd": round(self.material_cost_usd, 4),
            "machine_cost_usd": round(self.machine_cost_usd, 4),
            "support_cost_usd": round(self.support_cost_usd, 4),
            "subtotal_usd": round(self.subtotal_usd, 4),
            "margin_pct": round(self.margin_pct, 2),
            "margin_amount_usd": round(self.margin_amount_usd, 4),
            "rush_surcharge_usd": round(self.rush_surcharge_usd, 4),
            "total_usd": round(self.total_usd, 4),
        }


@dataclass
class EstimateContext:
    """Additional context about the estimate for reporting."""
    estimated_print_time_hours: float = 0.0
    material_grams_estimated: float = 0.0  # density varies by material
    confidence_adjusted_margin: bool = False
    base_margin_pct: float = DEFAULT_BASE_MARGIN
    confidence: float = 100.0
    material_type: str = "PLA"
    is_rush_order: bool = False


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

@dataclass
class CostEstimate:
    """Complete cost estimate result."""
    breakdown: CostBreakdown = field(default_factory=CostBreakdown)
    context: EstimateContext = field(default_factory=EstimateContext)

    def to_dict(self) -> dict:
        return {
            "breakdown": self.breakdown.to_dict(),
            "context": {
                "estimated_print_time_hours": round(self.context.estimated_print_time_hours, 2),
                "material_grams_estimated": round(self.context.material_grams_estimated, 2),
                "confidence_adjusted_margin": self.context.confidence_adjusted_margin,
                "base_margin_pct": round(self.context.base_margin_pct, 2),
                "confidence": round(self.context.confidence, 1),
                "material_type": self.context.material_type,
                "is_rush_order": self.context.is_rush_order,
            },
        }


def estimate_cost(
    analysis: GeometryAnalysis,
    material: str = "PLA",
    rush: bool = False,
    machine_rate_hr: float = DEFAULT_MACHINE_RATE_PER_HOUR,
    support_price_cm3: float = DEFAULT_SUPPORT_MATERIAL_PRICE_PER_CM3,
    base_margin: float = DEFAULT_BASE_MARGIN,
    print_speed_cm3_hr: float = DEFAULT_ESTIMATED_PRINT_SPEED_CM3_PER_HOUR,
) -> CostEstimate:
    """Estimate 3D printing cost based on geometry analysis.

    Parameters
    ----------
    analysis : GeometryAnalysis
        Output from ``stl_analyzer.analyze_stl``.
    material : str
        Material type (e.g. PLA, PETG, ABS).
    rush : bool
        If True, apply 1.5x rush order surcharge.
    machine_rate_hr : float
        Machine operating cost per hour in USD.
    support_price_cm3 : float
        Support material cost per cm³ in USD.
    base_margin : float
        Base profit margin as a fraction (0.30 = 30%).
    print_speed_cm3_hr
        Estimated print speed in cm³ per hour.

    Returns
    -------
    CostEstimate
        Full cost breakdown with context.
    """
    volume_cm3 = analysis.volume_cm3
    overhang_pct = analysis.overhang.overhang_percentage / 100.0

    # --- Material cost ---
    filament_price_cm3 = MATERIAL_PRICING.get(material, MATERIAL_PRICING["PLA"])
    
    # Overhang support cost: for every 10% overhang beyond 45° (which overhang_percentage measures), add 5% to material cost
    overhang_material_surcharge = int(analysis.overhang.overhang_percentage // 10) * 0.05
    effective_filament_price_cm3 = filament_price_cm3 * (1.0 + overhang_material_surcharge)

    material_cost = volume_cm3 * effective_filament_price_cm3

    # --- Machine time cost ---
    # Estimate print time from volume and print speed
    print_time_hours = volume_cm3 / print_speed_cm3_hr if print_speed_cm3_hr > 0 else 1.0
    # Adjust for complexity: more overhangs = more retractions = slower
    complexity_multiplier = 1.0 + (overhang_pct * 0.5)  # up to 1.5x for extreme overhang
    print_time_hours *= complexity_multiplier
    machine_cost = print_time_hours * machine_rate_hr

    # --- Support material cost ---
    # Supports are needed for overhangs; estimate support volume
    support_volume_cm3 = volume_cm3 * overhang_pct * SUPPORT_VOLUME_FRACTION
    support_cost = support_volume_cm3 * support_price_cm3

    # --- Subtotal ---
    subtotal = material_cost + machine_cost + support_cost

    # --- Confidence-based margin adjustment ---
    confidence = analysis.structural_confidence
    confidence_adjusted = False
    adjusted_margin = base_margin

    if confidence < 50.0:
        # Low confidence → significantly higher margin to cover risk of failure
        risk_surcharge = (50.0 - confidence) / 50.0 * 0.50  # up to 50% extra
        adjusted_margin = base_margin + risk_surcharge
        confidence_adjusted = True
    elif confidence < 70.0:
        # Moderate confidence → slight increase
        risk_surcharge = (70.0 - confidence) / 20.0 * 0.20  # up to 20% extra
        adjusted_margin = base_margin + risk_surcharge
        confidence_adjusted = True

    adjusted_margin = min(adjusted_margin, 0.80)  # cap at 80%

    margin_amount = subtotal * adjusted_margin
    pre_rush_total = subtotal + margin_amount
    
    # --- Rush Order Surcharge ---
    rush_surcharge = 0.0
    if rush:
        rush_surcharge = pre_rush_total * 0.5  # 1.5x total
        
    total = pre_rush_total + rush_surcharge

    # --- Density estimate ---
    density_map = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "SLA resin": 1.15}
    density_g_cm3 = density_map.get(material, 1.24)
    material_grams = volume_cm3 * density_g_cm3

    breakdown = CostBreakdown(
        material_cost_usd=material_cost,
        machine_cost_usd=machine_cost,
        support_cost_usd=support_cost,
        subtotal_usd=subtotal,
        margin_pct=adjusted_margin * 100.0,
        margin_amount_usd=margin_amount,
        rush_surcharge_usd=rush_surcharge,
        total_usd=total,
    )

    context = EstimateContext(
        estimated_print_time_hours=print_time_hours,
        material_grams_estimated=material_grams,
        confidence_adjusted_margin=confidence_adjusted,
        base_margin_pct=base_margin * 100.0,
        confidence=confidence,
        material_type=material,
        is_rush_order=rush,
    )

    return CostEstimate(breakdown=breakdown, context=context)
