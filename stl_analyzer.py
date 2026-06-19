"""
STL Analyzer — Core geometry analysis module for 3D printing cost estimation.

Uses trimesh to parse STL files and extract geometric properties including
volume, surface area, overhang analysis, wall thickness estimation, and
structural confidence scoring.

Part of "The Custom Parts Bureau" — Hermes Agent hackathon project.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh


# ---------------------------------------------------------------------------
# Default analysis parameters
# ---------------------------------------------------------------------------
DEFAULT_OVERHANG_ANGLE_DEG: float = 45.0
DEFAULT_MIN_WALL_THICKNESS_MM: float = 0.8
DEFAULT_RAY_SAMPLES: int = 500
RAY_DIRECTION = np.array([0.0, 0.0, -1.0])  # cast downward (into the model)


# ---------------------------------------------------------------------------
# Data classes for structured results
# ---------------------------------------------------------------------------

@dataclass
class BoundingBox:
    """Axis-aligned bounding box in millimetres."""
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0

    @property
    def volume_mm3(self) -> float:
        return self.x_mm * self.y_mm * self.z_mm

    def __str__(self) -> str:
        return f"{self.x_mm:.1f} × {self.y_mm:.1f} × {self.z_mm:.1f} mm"


@dataclass
class OverhangResult:
    """Results of the overhang analysis."""
    overhang_area_cm2: float = 0.0
    total_area_cm2: float = 0.0
    overhang_percentage: float = 0.0
    overhang_angle_threshold_deg: float = DEFAULT_OVERHANG_ANGLE_DEG
    num_overhang_faces: int = 0
    total_faces: int = 0


@dataclass
class WallThicknessResult:
    """Results of wall thickness estimation via ray-casting."""
    min_thickness_mm: float = 0.0
    avg_thickness_mm: float = 0.0
    max_thickness_mm: float = 0.0
    samples_taken: int = 0
    samples_below_tolerance: int = 0
    tolerance_mm: float = DEFAULT_MIN_WALL_THICKNESS_MM


@dataclass
class MeshIntegrity:
    """Mesh health check results."""
    is_watertight: bool = False
    is_winding_consistent: bool = False
    num_degenerate_faces: int = 0
    num_faces: int = 0
    num_vertices: int = 0
    num_bodies: int = 0


@dataclass
class GeometryAnalysis:
    """Complete geometry analysis output."""
    filename: str = ""
    volume_cm3: float = 0.0
    surface_area_cm2: float = 0.0
    bounding_box: BoundingBox = field(default_factory=BoundingBox)
    overhang: OverhangResult = field(default_factory=OverhangResult)
    wall_thickness: WallThicknessResult = field(default_factory=WallThicknessResult)
    integrity: MeshIntegrity = field(default_factory=MeshIntegrity)
    triangle_count: int = 0
    structural_confidence: float = 0.0  # 0-100

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON output."""
        return {
            "filename": self.filename,
            "volume_cm3": round(self.volume_cm3, 4),
            "surface_area_cm2": round(self.surface_area_cm2, 4),
            "bounding_box_mm": {
                "x": round(self.bounding_box.x_mm, 2),
                "y": round(self.bounding_box.y_mm, 2),
                "z": round(self.bounding_box.z_mm, 2),
            },
            "triangle_count": self.triangle_count,
            "overhang": {
                "percentage": round(self.overhang.overhang_percentage, 2),
                "area_cm2": round(self.overhang.overhang_area_cm2, 4),
                "threshold_deg": self.overhang.overhang_angle_threshold_deg,
                "num_faces": self.overhang.num_overhang_faces,
                "total_faces": self.overhang.total_faces,
            },
            "wall_thickness": {
                "min_mm": round(self.wall_thickness.min_thickness_mm, 3),
                "avg_mm": round(self.wall_thickness.avg_thickness_mm, 3),
                "max_mm": round(self.wall_thickness.max_thickness_mm, 3),
                "tolerance_mm": self.wall_thickness.tolerance_mm,
                "samples_below_tolerance": self.wall_thickness.samples_below_tolerance,
                "samples_taken": self.wall_thickness.samples_taken,
            },
            "integrity": {
                "watertight": self.integrity.is_watertight,
                "winding_consistent": self.integrity.is_winding_consistent,
                "degenerate_faces": self.integrity.num_degenerate_faces,
                "num_bodies": self.integrity.num_bodies,
            },
            "structural_confidence": round(self.structural_confidence, 1),
        }


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _mm2_to_cm2(val_mm2: float) -> float:
    """Convert mm² to cm²."""
    return val_mm2 / 100.0


def _mm3_to_cm3(val_mm3: float) -> float:
    """Convert mm³ to cm³."""
    return val_mm3 / 1000.0


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def load_mesh(stl_path: str | Path) -> trimesh.Trimesh:
    """Load an STL file and return a trimesh.Trimesh object.

    Raises
    ------
    FileNotFoundError
        If the path does not exist.
    ValueError
        If the file cannot be parsed as a valid triangle mesh.
    """
    path = Path(stl_path)
    if not path.exists():
        raise FileNotFoundError(f"STL file not found: {path}")

    try:
        mesh = trimesh.load(str(path), force="mesh")
    except Exception as exc:
        raise ValueError(f"Failed to parse STL file '{path.name}': {exc}") from exc

    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"File '{path.name}' did not produce a triangle mesh (got {type(mesh).__name__})")

    if len(mesh.faces) == 0:
        raise ValueError(f"STL file '{path.name}' contains no faces.")

    return mesh


def compute_bounding_box(mesh: trimesh.Trimesh) -> BoundingBox:
    """Return the axis-aligned bounding box dimensions in mm."""
    extents = mesh.bounding_box.extents  # [x, y, z] in mesh units (mm)
    return BoundingBox(x_mm=float(extents[0]), y_mm=float(extents[1]), z_mm=float(extents[2]))


def compute_overhang(
    mesh: trimesh.Trimesh,
    threshold_deg: float = DEFAULT_OVERHANG_ANGLE_DEG,
) -> OverhangResult:
    """Analyse face normals to detect overhanging surfaces.

    An overhang is any face whose outward normal has a Z component pointing
    sufficiently downward — specifically, the angle between the face normal
    and the vertical (Z-up) axis exceeds ``threshold_deg`` **and** the face
    is pointing downward (negative Z).

    Parameters
    ----------
    mesh : trimesh.Trimesh
        The loaded triangle mesh.
    threshold_deg : float
        Maximum overhang angle in degrees (default 45°).

    Returns
    -------
    OverhangResult
    """
    face_normals = mesh.face_normals  # (N, 3) unit vectors
    face_areas = mesh.area_faces      # (N,) scalar areas in mm²

    # Angle between each face normal and the upward vector [0, 0, 1]
    # For upward face (nz=1): angle = 0°, downward (nz=-1): angle = 180°
    # Overhang = face pointing downward (nz < 0) AND angle > threshold
    nz = face_normals[:, 2]
    # arccos(nz) gives angle from vertical (0°=up, 180°=down)
    angle_from_vertical_rad = np.arccos(np.clip(nz, -1.0, 1.0))
    angle_from_vertical_deg = np.degrees(angle_from_vertical_rad)

    # Overhang: face pointing downward AND angle exceeds threshold
    # angle > threshold means the face is tilted more than threshold degrees
    # from straight up, which for downward faces means it's an overhang
    is_overhang = (nz < 0) & (angle_from_vertical_deg > threshold_deg)

    overhang_area = float(np.sum(face_areas[is_overhang]))
    total_area = float(np.sum(face_areas))

    return OverhangResult(
        overhang_area_cm2=_mm2_to_cm2(overhang_area),
        total_area_cm2=_mm2_to_cm2(total_area),
        overhang_percentage=(overhang_area / total_area * 100.0) if total_area > 0 else 0.0,
        overhang_angle_threshold_deg=threshold_deg,
        num_overhang_faces=int(np.sum(is_overhang)),
        total_faces=len(face_areas),
    )


def compute_wall_thickness(
    mesh: trimesh.Trimesh,
    num_samples: int = DEFAULT_RAY_SAMPLES,
    tolerance_mm: float = DEFAULT_MIN_WALL_THICKNESS_MM,
) -> WallThicknessResult:
    """Estimate wall thickness via simplified inward ray-casting.

    For each sample point on the mesh surface, a ray is cast inward
    (opposite to the face normal). The distance to the first intersection
    with another part of the mesh surface is the local wall thickness.

    Rays are offset slightly inward from the surface to avoid
    self-intersection (the ray hitting the triangle it originated from).

    Parameters
    ----------
    mesh : trimesh.Trimesh
        The loaded triangle mesh.
    num_samples : int
        Number of random surface points to sample.
    tolerance_mm : float
        Minimum acceptable wall thickness in mm.

    Returns
    -------
    WallThicknessResult
    """
    face_areas = mesh.area_faces
    total_area = float(np.sum(face_areas))

    if total_area <= 0:
        return WallThicknessResult(tolerance_mm=tolerance_mm)

    # Probability of selecting each face proportional to its area
    probs = face_areas / total_area

    # Sample face indices
    rng = np.random.default_rng(42)  # deterministic for reproducibility
    face_indices = rng.choice(len(face_areas), size=num_samples, p=probs)

    # Get random points on sampled faces (barycentric interpolation)
    face_verts = mesh.vertices[mesh.faces[face_indices]]  # (N, 3, 3)
    # Random barycentric coordinates
    r1 = rng.random(num_samples)
    r2 = rng.random(num_samples)
    u = 1.0 - np.sqrt(r1)
    v = np.sqrt(r1) * (1.0 - r2)
    w = np.sqrt(r1) * r2
    sample_points = (
        u[:, None] * face_verts[:, 0]
        + v[:, None] * face_verts[:, 1]
        + w[:, None] * face_verts[:, 2]
    )

    # Outward normals at sample points (use face normals)
    sample_normals = mesh.face_normals[face_indices]  # (N, 3)

    # Cast rays inward: direction = -normal (opposite to outward normal)
    directions = -sample_normals  # (N, 3)

    # Offset origins slightly inward to avoid self-intersection
    # Use a small fixed offset — just enough to skip the originating triangle
    # but small enough not to filter out legitimate thin walls
    offset = 0.02  # 0.02mm offset — well below any real wall thickness
    origins = sample_points + directions * offset  # (N, 3)

    # Use trimesh ray casting with multiple hits to find the closest hit
    locations, index_ray, index_tri = mesh.ray.intersects_location(
        ray_origins=origins,
        ray_directions=directions,
        multiple_hits=True,
    )

    # Calculate thicknesses: for each ray, find the closest hit
    # that is far enough away (more than offset distance, to skip self)
    thicknesses = []
    for i in range(num_samples):
        mask = index_ray == i
        if np.any(mask):
            hit_points = locations[mask]
            # Distance from offset origin to each hit
            dists = np.linalg.norm(hit_points - origins[i], axis=1)
            # Filter out hits that are too close (self-intersection)
            # Use a threshold slightly above the offset
            valid = dists > (offset * 1.5)
            if np.any(valid):
                min_dist = float(np.min(dists[valid]))
                thicknesses.append(min_dist)

    if not thicknesses:
        # Fallback: estimate from bounding box
        bbox = mesh.bounding_box.extents
        min_dim = float(np.min(bbox))
        return WallThicknessResult(
            min_thickness_mm=min_dim,
            avg_thickness_mm=min_dim,
            max_thickness_mm=min_dim,
            samples_taken=0,
            samples_below_tolerance=0,
            tolerance_mm=tolerance_mm,
        )

    thicknesses_arr = np.array(thicknesses)
    below_tolerance = int(np.sum(thicknesses_arr < tolerance_mm))

    return WallThicknessResult(
        min_thickness_mm=float(np.min(thicknesses_arr)),
        avg_thickness_mm=float(np.mean(thicknesses_arr)),
        max_thickness_mm=float(np.max(thicknesses_arr)),
        samples_taken=len(thicknesses),
        samples_below_tolerance=below_tolerance,
        tolerance_mm=tolerance_mm,
    )


def check_mesh_integrity(mesh: trimesh.Trimesh) -> MeshIntegrity:
    """Evaluate mesh health: watertightness, winding, degenerate faces."""
    # Degenerate faces: faces with near-zero area
    face_areas = mesh.area_faces
    degenerate_threshold = 1e-10
    num_degenerate = int(np.sum(face_areas < degenerate_threshold))

    # Count separate bodies
    bodies = mesh.split(only_watertight=False)
    num_bodies = len(bodies) if bodies else 1

    return MeshIntegrity(
        is_watertight=mesh.is_watertight,
        is_winding_consistent=mesh.is_winding_consistent,
        num_degenerate_faces=num_degenerate,
        num_faces=len(mesh.faces),
        num_vertices=len(mesh.vertices),
        num_bodies=num_bodies,
    )


def compute_structural_confidence(
    overhang: OverhangResult,
    wall_thickness: WallThicknessResult,
    integrity: MeshIntegrity,
) -> float:
    """Compute a 0-100 structural confidence score.

    The score is composed of three weighted factors:

    1. **Wall thickness score (40%)** — based on the fraction of samples
       above the minimum tolerance. Perfect if 0% are below tolerance.
    2. **Overhang score (30%)** — penalises large overhang percentages.
       0% overhang = 100 score; ≥30% overhang = 0 score.
    3. **Mesh integrity score (30%)** — watertight + consistent winding
       = 100; each defect reduces the score.

    Returns
    -------
    float
        Confidence value between 0 and 100.
    """
    # --- Wall thickness sub-score (40%) ---
    if wall_thickness.samples_taken > 0:
        thickness_ratio = 1.0 - (wall_thickness.samples_below_tolerance / wall_thickness.samples_taken)
        # If min thickness is well above tolerance, give a bonus
        if wall_thickness.min_thickness_mm >= wall_thickness.tolerance_mm:
            thickness_bonus = min(0.2, (wall_thickness.min_thickness_mm / wall_thickness.tolerance_mm - 1.0) * 0.1)
        else:
            thickness_bonus = 0.0
        thickness_score = min(100.0, max(0.0, (thickness_ratio + thickness_bonus) * 100.0))
    else:
        # No samples taken — assume moderate confidence
        thickness_score = 50.0

    # --- Overhang sub-score (30%) ---
    # 0% overhang → 100, linear drop to 0 at 30% overhang
    overhang_score = max(0.0, 100.0 - (overhang.overhang_percentage / 30.0) * 100.0)

    # --- Integrity sub-score (30%) ---
    integrity_score = 0.0
    if integrity.is_watertight:
        integrity_score += 40.0
    if integrity.is_winding_consistent:
        integrity_score += 30.0
    if integrity.num_degenerate_faces == 0:
        integrity_score += 20.0
    if integrity.num_bodies == 1:
        integrity_score += 10.0

    # --- Weighted combination ---
    confidence = (
        0.40 * thickness_score
        + 0.30 * overhang_score
        + 0.30 * integrity_score
    )
    return round(min(100.0, max(0.0, confidence)), 1)


# ---------------------------------------------------------------------------
# Main analysis entry point
# ---------------------------------------------------------------------------

def analyze_stl(
    stl_path: str | Path,
    overhang_angle_deg: float = DEFAULT_OVERHANG_ANGLE_DEG,
    min_wall_thickness_mm: float = DEFAULT_MIN_WALL_THICKNESS_MM,
    ray_samples: int = DEFAULT_RAY_SAMPLES,
) -> GeometryAnalysis:
    """Perform a complete geometry analysis of an STL file.

    Parameters
    ----------
    stl_path : str or Path
        Path to the STL file.
    overhang_angle_deg : float
        Maximum angle (degrees) from vertical before a face is overhang.
    min_wall_thickness_mm : float
        Minimum acceptable wall thickness in mm.
    ray_samples : int
        Number of random points to sample for wall thickness estimation.

    Returns
    -------
    GeometryAnalysis
        Complete analysis results.

    Raises
    ------
    FileNotFoundError
        If the file doesn't exist.
    ValueError
        If the file can't be parsed or contains no valid geometry.
    """
    mesh = load_mesh(stl_path)

    # Basic geometry
    volume_mm3 = mesh.volume
    surface_area_mm2 = mesh.area
    bbox = compute_bounding_box(mesh)

    # Overhang
    overhang = compute_overhang(mesh, threshold_deg=overhang_angle_deg)

    # Wall thickness
    wall_thickness = compute_wall_thickness(mesh, num_samples=ray_samples, tolerance_mm=min_wall_thickness_mm)

    # Integrity
    integrity = check_mesh_integrity(mesh)

    # Confidence
    confidence = compute_structural_confidence(overhang, wall_thickness, integrity)

    return GeometryAnalysis(
        filename=Path(stl_path).name,
        volume_cm3=_mm3_to_cm3(abs(volume_mm3)),
        surface_area_cm2=_mm2_to_cm2(surface_area_mm2),
        bounding_box=bbox,
        overhang=overhang,
        wall_thickness=wall_thickness,
        integrity=integrity,
        triangle_count=len(mesh.faces),
        structural_confidence=confidence,
    )
