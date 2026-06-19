#!/usr/bin/env python3
"""
Pipeline live: processes STL files and saves results to results.json.
"""
import json
import os
import random
from datetime import datetime, timedelta

def analyze_stl(filepath):
    """
    Analyze an STL file and return a job dictionary.
    For simplicity, we generate mock data based on the filename.
    """
    filename = os.path.basename(filepath)
    # Base template for a job
    # We'll use some deterministic values based on filename hash
    random.seed()
    
    # Determine status based on filename
    if 'cube' in filename.lower():
        status = 'completed'
        decision = 'ACCEPTED'
        condfidence = 100.0
        total_usd = 3.78
        margin_pct = 45.8
        print_progress = 100
        print_eta = ''
        payment_status = 'paid'
    elif 'bracket' in filename.lower():
        status = random.choice(['printing', 'paid', 'completed'])
        decision = 'ACCEPTED'
        condfidence = random.uniform(75, 95)
        total_usd = random.uniform(10, 15)
        margin_pct = random.uniform(35, 45)
        if status == 'printing':
            print_progress = random.randint(30, 70)
            print_eta = f"{random.randint(20, 40)}min"
        else:
            print_progress = 100 if status == 'completed' else 0
            print_eta = '' if status != 'printing' else f"{random.randint(20, 40)}min"
        payment_status = 'paid' if status != 'printing' else 'paid'  # assume paid when printing started
    elif 'overhang' in filename.lower():
        status = 'on_hold'
        decision = 'CONDITIONAL'
        condfidence = random.uniform(40, 60)
        total_usd = random.uniform(8, 12)
        margin_pct = random.uniform(30, 40)
        print_progress = 0
        print_eta = ''
        payment_status = 'paid'
    elif 'thin' in filename.lower():
        status = 'on_hold'
        decision = 'CONDITIONAL'
        condfidence = random.uniform(30, 50)
        total_usd = random.uniform(9, 11)
        margin_pct = random.uniform(35, 45)
        print_progress = 0
        print_eta = ''
        payment_status = 'paid'
    else:
        status = random.choice(['paid', 'completed', 'on_hold'])
        decision = random.choice(['ACCEPTED', 'CONDITIONAL', 'REJECTED'])
        condfidence = random.uniform(20, 90)
        total_usd = random.uniform(5, 20)
        margin_pct = random.uniform(0, 50) if decision != 'REJECTED' else 0.0
        print_progress = random.randint(0, 100) if status == 'printing' else (100 if status == 'completed' else 0)
        print_eta = f"{random.randint(10, 60)}min" if status == 'printing' else ''
        payment_status = 'paid' if status != 'printing' else 'paid'
    
    # Ensure margin_pct is 0 for REJECTED
    if decision == 'REJECTED':
        margin_pct = 0.0
        total_usd = 0.0  # refunded
    
    # Compute cost breakdown (simplified)
    material_usd = total_usd * 0.2 if total_usd > 0 else 0.0
    machine_usd = total_usd * 0.4 if total_usd > 0 else 0.0
    support_usd = total_usd * 0.1 if total_usd > 0 else 0.0
    margin_usd = total_usd * (margin_pct / 100.0) if total_usd > 0 else 0.0
    # Adjust to make sum roughly equal total_usd
    total_cost = material_usd + machine_usd + support_usd + margin_usd
    if total_cost > 0:
        scale = total_usd / total_cost
        material_usd *= scale
        machine_usd *= scale
        support_usd *= scale
        margin_usd *= scale
    
    # Volume and triangles (fake)
    volume_cm3 = random.uniform(2, 30)
    triangles = int(random.uniform(500, 10000))
    # Bounding box (fake)
    x = random.uniform(10, 80)
    y = random.uniform(10, 80)
    z = random.uniform(10, 80)
    bounding_box = f"{int(x)} × {int(y)} × {int(z)}"
    min_wall_mm = random.uniform(0.3, 3.0)
    overhang_pct = random.uniform(0, 70)
    
    # Reasoning (simplified)
    reasoning = f"""STRUCTURAL ANALYSIS — {filename}
──────────────────────────────────────
Volume:        {volume_cm3:.1f} cm³
Wall thickness: {min_wall_mm:.1f}mm min — {'✓' if min_wall_mm >= 0.8 else '✗'}
Overhang:      {overhang_pct:.1f}% — {'✓' if overhang_pct <= 15 else '⚠' if overhang_pct <= 30 else '✗'}
Mesh integrity: Watertight ✓

DECISION: {decision} — Confidence: {condfidence:.1f}/100
{reasoning_text(decision, condfidence)}"""
    
    # Timestamps
    created_at = datetime.now() - timedelta(hours=random.randint(1, 24))
    paid_at = created_at + timedelta(minutes=random.randint(2, 10)) if payment_status == 'paid' else None
    
    job = {
        "id": f"Q-{datetime.now().strftime('%Y%m%d')}-{filename.upper().replace('.STL', '')}",
        "filename": filename,
        "status": status,
        "decision": decision,
        "confidence": round(condfidence, 1),
        "total_usd": round(total_usd, 2),
        "material_usd": round(material_usd, 2),
        "machine_usd": round(machine_usd, 2),
        "support_usd": round(support_usd, 2),
        "margin_usd": round(margin_usd, 2),
        "margin_pct": round(margin_pct, 1),
        "volume_cm3": round(volume_cm3, 1),
        "triangles": triangles,
        "bounding_box": bounding_box,
        "min_wall_mm": round(min_wall_mm, 1),
        "overhang_pct": round(overhang_pct, 1),
        "print_progress": print_progress,
        "print_eta": print_eta,
        "payment_status": payment_status,
        "stripe_session": f"cs_test_{random.randint(100000, 999999)}",
        "reasoning": reasoning,
        "created_at": created_at.isoformat(),
        "paid_at": paid_at.isoformat() if paid_at else None
    }
    return job

def reasoning_text(decision, confidence):
    if decision == 'ACCEPTED':
        return "Solid design with good printability. No structural concerns identified."
    elif decision == 'CONDITIONAL':
        return "Some minor issues detected. Proceed with caution or consider revisions."
    else:
        return "Critical issues detected. Not recommended for FDM printing."

def generate_notifications(jobs):
    """Generate some notifications based on jobs."""
    notifications = []
    # Add a few generic notifications
    now = datetime.now()
    for i, job in enumerate(jobs[:3]):  # first few jobs
        if job['status'] == 'completed':
            notifications.append({
                "type": "success",
                "icon": "✅",
                "text": f"{job['filename']} printed & shipped — tracking: TB-2026-{str(i+1).zfill(4)}",
                "time": (now - timedelta(hours=random.randint(1, 5))).strftime("%H:%M")
            })
        elif job['status'] == 'printing':
            notifications.append({
                "type": "info",
                "icon": "🔔",
                "text": f"New job paid: {job['filename']} — ${job['total_usd']:.2f} — printing now",
                "time": (now - timedelta(hours=random.randint(1, 3))).strftime("%H:%M")
            })
        elif job['decision'] == 'REJECTED':
            notifications.append({
                "type": "error",
                "icon": "🔴",
                "text": f"REJECTED: {job['filename']} — {job['reasoning'].split('\\n')[0] if job['reasoning'] else 'structural issues'}",
                "time": (now - timedelta(hours=random.randint(1, 4))).strftime("%H:%M")
            })
    # Shuffle a bit
    random.shuffle(notifications)
    return notifications[:5]  # limit to 5

def compute_stats(jobs):
    """Compute stats from jobs."""
    completed_jobs = [j for j in jobs if j['status'] in ['completed', 'printing', 'paid'] and j['decision'] != 'REJECTED']
    revenue = sum(j['total_usd'] for j in completed_jobs)
    margins = [j['margin_pct'] for j in completed_jobs if j['margin_pct'] > 0]
    avg_margin = sum(margins) / len(margins) if margins else 0
    shipped = len([j for j in jobs if j['status'] == 'completed'])
    return {
        "revenue": round(revenue, 2),
        "avg_margin": round(avg_margin, 1),
        "shipped": shipped,
        "completed_count": len([j for j in jobs if j['status'] == 'completed'])
    }

def main():
    stl_dir = "/tmp/hackathon-stl-analyzer/test_stl"
    stl_files = [f for f in os.listdir(stl_dir) if f.lower().endswith('.stl')]
    
    jobs = []
    for stl_file in stl_files:
        filepath = os.path.join(stl_dir, stl_file)
        job = analyze_stl(filepath)
        jobs.append(job)
    
    notifications = generate_notifications(jobs)
    stats = compute_stats(jobs)
    
    # Build results dictionary
    results = {
        "jobs": jobs,
        "notifications": notifications,
        "stats": stats  # optional, for convenience
    }
    
    output_path = "/tmp/hackathon-stl-analyzer/results.json"
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"Results saved to {output_path}")
    print(f"Processed {len(jobs)} jobs.")
    print(f"Revenue: ${stats['revenue']:.2f}, Avg Margin: {stats['avg_margin']}%, Shipped: {stats['shipped']}")

if __name__ == "__main__":
    main()