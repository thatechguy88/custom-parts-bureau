# 🔧 The Custom Parts Bureau — STL Analysis & Cost Estimator

> **Hermes Agent Accelerated Business Hackathon** (NVIDIA / Stripe / Nous Research)

An AI-powered 3D printing business that analyzes STL files, estimates costs, and makes accept/reject decisions with physical reasoning. The "refusal moment" — rejecting bad models with specific structural reasoning — is the demo centerpiece.

## Quick Start

```bash
# Install dependencies
pip3 install trimesh numpy rtree scipy networkx

# Run the demo on any STL file
python3 demo.py path/to/model.stl

# Run on test files
python3 demo.py test_stl/cube.stl          # ✅ ACCEPTED (100/100)
python3 demo.py test_stl/overhang_test.stl  # ⚠️ CONDITIONAL (67/100)
python3 demo.py test_stl/thin_walls.stl     # ⚠️ CONDITIONAL (41/100)
python3 demo.py test_stl/bracket.stl        # ✅ ACCEPTED (79/100)
```

## Architecture

```
stl_analyzer.py     → Geometry analysis (volume, overhang, wall thickness, integrity)
cost_estimator.py   → Cost estimation (material, machine, support, margin)
quote_generator.py  → Quote generation with Nemotron reasoning prompts
demo.py             → Full pipeline demo with formatted output
generate_tests.py   → Test STL file generator
test_stl/           → Sample STL files for testing
```

## What It Analyzes

| Metric | Description |
|--------|-------------|
| **Volume** | Model volume in cm³ |
| **Surface Area** | Total surface area in cm² |
| **Bounding Box** | Dimensions in mm |
| **Overhang %** | Percentage of surface exceeding 45° threshold |
| **Wall Thickness** | Min/avg/max via ray-casting (tolerance: 0.8mm) |
| **Mesh Integrity** | Watertight, winding consistency, degenerate faces |
| **Confidence Score** | 0-100 structural confidence rating |

## Cost Model

| Component | Default Rate |
|-----------|-------------|
| PLA Filament | $0.03/cm³ |
| Machine Time | $4.00/hr |
| Support Material | $0.05/cm³ |
| Base Margin | 30% |
| Risk Margin | Up to +50% (confidence-adjusted) |

## Decision Thresholds

| Confidence | Decision |
|-----------|----------|
| ≥ 70 | ✅ ACCEPT |
| 30-70 | ⚠️ CONDITIONAL |
| < 30 | ❌ REJECT |

## JSON API Output

The script outputs structured JSON for API integration:

```json
{
  "quote_id": "Q-20260619-CUBE.STL",
  "decision": "ACCEPT",
  "confidence": 100.0,
  "total_usd": 3.78,
  "analysis_summary": { ... },
  "reasoning_text": "..."
}
```

## Nemotron Integration

The `nemotron_prompt` field in the JSON output contains a structured prompt for Nemotron 3 Ultra to expand into natural language customer explanations.

## Files

| File | Purpose |
|------|---------|
| `stl_analyzer.py` | Core geometry analysis module |
| `cost_estimator.py` | Cost calculation with dynamic pricing |
| `quote_generator.py` | Quote generation with decision logic |
| `demo.py` | Full pipeline demo script |
| `generate_tests.py` | Test STL file generator |
| `requirements.txt` | Python dependencies |
| `test_stl/` | Sample STL files |

## License

Hackathon project — NVIDIA/Stripe/Nous Research Hermes Agent Accelerated Business Hackathon.
