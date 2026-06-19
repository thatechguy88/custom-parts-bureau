# Full Customer Flow — Implementation Plan

## Goal

Build a working end-to-end customer flow: **Upload STL → Analyze → Quote → Pay → Track** — powered by the existing pipeline modules, backed by Flask + SQLite, with a polished dark terminal UI matching the dashboard.

**Deadline:** June 30 (10 days)  
**Scope:** Hackathon MVP — functional demo, not production deployment

## Decisions

- **Nemotron in live flow:** YES — keep it, add 3-stage loading animation to cover the 10-30s latency. Fallback to built-in reasoning if API times out (60s).
- **Admin page:** DEFERRED — no real users yet, demo only.
- **Dashboard seed data:** Keep 14 mock jobs as JS fallback when API returns <5 jobs. Real uploads populate alongside.

---

## File Structure

```
custom-parts-bureau/
├── app.py                          [NEW]  Flask app — routes + pipeline orchestration
├── models.py                       [NEW]  SQLite database layer (Job CRUD)
├── adapters.py                     [NEW]  Schema bridges (quote→stripe, job→dashboard)
├── templates/
│   ├── base.html                   [NEW]  Shared dark theme shell
│   ├── landing.html                [NEW]  Upload page (drag/drop + email)
│   ├── quote.html                  [NEW]  Quote display + pay button
│   └── status.html                 [NEW]  Order tracking timeline
├── dashboard.html                  [MODIFY] Fetch from /api/dashboard-data instead of results.json
├── stl_analyzer.py                         (untouched)
├── cost_estimator.py                       (untouched)
├── quote_generator.py                      (untouched)
├── nemotron_reasoning.py                   (untouched)
├── stripe_integration.py                   (untouched — adapter handles schema mismatch)
├── requirements.txt                [MODIFY] Add flask, requests, stripe
├── .gitignore                      [MODIFY] Add uploads/, *.db
└── uploads/                        [NEW]  STL file storage (gitignored)
```

**Zero modifications to the 5 core pipeline modules.** All schema mismatches handled by `adapters.py`.

---

## Database (SQLite)

```sql
CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    email           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'uploaded',
                    -- uploaded → analyzing → quoted → paying → paid → printing → completed | rejected
    decision        TEXT,
    confidence      REAL,
    volume_cm3      REAL,
    surface_area_cm2 REAL,
    triangle_count  INTEGER,
    bounding_box    TEXT,
    overhang_pct    REAL,
    min_wall_mm     REAL,
    material_usd    REAL,
    machine_usd     REAL,
    support_usd     REAL,
    margin_usd      REAL,
    margin_pct      REAL,
    total_usd       REAL,
    reasoning_text  TEXT,
    nemotron_explanation TEXT,
    line_items_json TEXT,
    stripe_session_id TEXT,
    stripe_payment_status TEXT,
    stl_path        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

---

## Routes

### Pages
| Route | Method | Serves |
|---|---|---|
| `/` | GET | Landing / upload page |
| `/quote/<job_id>` | GET | Quote display page |
| `/status/<job_id>` | GET | Order tracking page |
| `/dashboard` | GET | P&L dashboard |

### API
| Route | Method | What it does |
|---|---|---|
| `/api/upload` | POST | Receive STL + email → save → create job → background pipeline → return `{job_id}` |
| `/api/quote/<job_id>` | GET | Return job data (polled by quote page until status changes from `analyzing`) |
| `/api/checkout/<job_id>` | POST | Create Stripe Checkout Session → return `{checkout_url}` |
| `/api/webhook` | POST | Stripe webhook → verify signature → update job to `paid` |
| `/api/status/<job_id>` | GET | Return job status + details |
| `/api/dashboard-data` | GET | Return `{jobs, notifications}` in dashboard format |

---

## Pipeline Flow (background thread)

```python
def run_pipeline(job_id, stl_path):
    update_job(job_id, status="analyzing")
    analysis = analyze_stl(stl_path)
    estimate = estimate_cost(analysis)
    quote = generate_quote(analysis, estimate)
    quote_dict = quote.to_dict()
    inject_machine_hours(quote_dict, estimate)
    nemotron_result = generate_reasoning(quote_dict)  # 10-30s
    final_status = "rejected" if quote.decision == Decision.REJECT else "quoted"
    update_job(job_id, status=final_status, ...)
```

---

## Schema Adapters (adapters.py)

### quote_to_stripe_dict
| Quote key | Stripe key |
|---|---|
| `filename` | `model_name` |
| `total_usd` | `total_cost` |
| `reasoning_text` | `reasoning` |
| `line_items[].label` | `line_items[].name` |
| `line_items[].cost_usd` | `line_items[].amount` |

### inject_machine_hours
Patches `quote_dict["analysis_summary"]["machine_hours"]` with `cost_estimate.context.estimated_print_time_hours`.

### job_to_dashboard_format
Maps SQLite row → dashboard JS object shape (matching the 14 mock jobs).

---

## Customer Pages

All pages: dark theme, JetBrains Mono, #0a0a0a bg, #00ff41/#00d4ff accents, scanline overlay.

### Landing (`/`)
- "THE CUSTOM PARTS BUREAU" header
- Drag/drop zone with glow-on-hover
- Email input
- "Analyze & Quote" button
- 3-step "How It Works" cards

### Quote (`/quote/<job_id>`)
- **Loading state**: 3-stage animation (Analyzing geometry → Estimating costs → AI reasoning)
- **Result state**: Decision badge, cost breakdown table, Nemotron reasoning box, Pay button
- **Rejected state**: No pay button, "Upload a revised file" CTA

### Status (`/status/<job_id>`)
- Timeline: Uploaded → Analyzed → Quoted → Paid → Printing → Completed
- Current stage pulsing, completed stages solid green
- Job details panel

---

## Dashboard Modifications

1. Change `fetch('results.json')` → `fetch('/api/dashboard-data')`
2. Fix `printingJob.id` bug → use `selectedJobId`
3. Change hardcoded reference time to `new Date()`
4. Keep mock data as fallback when API returns <5 jobs

---

## Verification

```bash
python3 app.py
curl -X POST -F "file=@test_stl/cube.stl" -F "email=test@example.com" http://localhost:5000/api/upload
curl http://localhost:5000/api/quote/<job_id>
curl -X POST http://localhost:5000/api/checkout/<job_id>
curl http://localhost:5000/api/dashboard-data
```

Manual: upload all 4 test STLs, verify ACCEPT/CONDITIONAL/REJECT paths, pay with 4242 test card, check status page, verify dashboard shows real jobs.
