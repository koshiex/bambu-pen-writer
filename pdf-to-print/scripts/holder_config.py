"""Pen-holder profiles for P1S plotter pipeline (XY offset + Z calibration + motion settings)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class HolderProfile:
    name: str
    pen_offset_x: float
    pen_offset_y: float
    z_pen_down: float
    z_hop: float


# UMTS spring module (caliper-measured).
UMTS_Z_PEN_DOWN = 40.7
UMTS_Z_HOP = 12.0

UMTS = HolderProfile(
    name="umts",
    pen_offset_x=-26.46,
    pen_offset_y=-37.9,
    z_pen_down=UMTS_Z_PEN_DOWN,
    z_hop=UMTS_Z_HOP,
)

# KEV / MakerWorld soft spring holder (Stabilo fine-liner), caliper ~2026-05.
SOFT_HOLDER_Z_EXTRA_MM = 33.5  # vs UMTS_Z_PEN_DOWN (caliper, soft spring holder)
# Hop-ladder sheet 2026-10-04: streaks at 1.5 mm in part of the block, clean from 2.0 mm;
# +0.5 mm margin for paper waviness (raise it if a notebook bulges near the spine).
SOFT_HOLDER_Z_HOP = 2.5

SOFT_HOLDER = HolderProfile(
    name="soft-holder",
    pen_offset_x=-38.34,
    pen_offset_y=-21.13,
    z_pen_down=UMTS_Z_PEN_DOWN + SOFT_HOLDER_Z_EXTRA_MM,
    z_hop=SOFT_HOLDER_Z_HOP,
)


def paper_origin_x(paper_left: float, pen_offset_x: float) -> str:
    return f"{paper_left - pen_offset_x}mm"


def paper_origin_y(paper_front: float, paper_h: float, pen_offset_y: float) -> str:
    return f"{paper_front + paper_h - pen_offset_y}mm"


def nozzle_x_max_pen(paper_left: float, paper_w: float, pen_offset_x: float) -> float:
    """Max nozzle X when pen draws at paper right edge."""
    return paper_left + paper_w - pen_offset_x


def select_profile(*, soft_holder: bool) -> HolderProfile:
    return SOFT_HOLDER if soft_holder else UMTS


# P1S bed 256×256 — park at center for install / flip / remove pauses (nozzle frame).
PARK_NOZZLE_X_MM = 128.0
PARK_NOZZLE_Y_MM = 128.0

# Pen-up moves (G0 XY, G1 Z hop/lift). Marlin F = mm/min.
# Was 300 / 20 mm/s; raised toward p1s_plotter (500 / 20 nominal, ×1.5 in that repo).
TRAVEL_SPEED_MM_S = 500.0
Z_TRAVEL_SPEED_MM_S = 30.0


def travel_feed_mm_min() -> int:
    return int(TRAVEL_SPEED_MM_S * 60)


def z_travel_feed_mm_min() -> int:
    return int(Z_TRAVEL_SPEED_MM_S * 60)

# Nozzle Z for pauses — margin above pen-down + extra headroom.
# Legacy UMTS: G1 Z50 with Z_PEN_DOWN=40.7 → +9.3 mm; +10 mm extra park lift.
Z_CLEARANCE_ABOVE_PEN_DOWN_MM = 50.0 - UMTS_Z_PEN_DOWN
Z_PARK_EXTRA_MM = 10.0


def z_travel_clearance(z_pen_down: float) -> float:
    """Safe nozzle Z before operator touches module (install / flip / remove)."""
    return round(z_pen_down + Z_CLEARANCE_ABOVE_PEN_DOWN_MM + Z_PARK_EXTRA_MM, 1)


def z_travel_clearance_for_profile(profile: HolderProfile) -> float:
    return z_travel_clearance(profile.z_pen_down)


# --- Pen motion tuning (env overrides; calibrate with scripts/calibration_sheets_gcode.py) ----------
# Pen lifts dominate page time (Z moves the bed at 20 mm/s, 500 mm/s²): keep Z-hop as small as
# the hop-ladder sheet allows. Draw speed/accel barely change page time once strokes are
# smoothed, so defaults favour gel-ink quality.
Z_HOP_ENV = "PDF_TO_PRINT_Z_HOP_MM"
DRAW_SPEED_ENV = "PDF_TO_PRINT_DRAW_SPEED_MM_S"
DRAW_ACCEL_ENV = "PDF_TO_PRINT_DRAW_ACCEL_MM_S2"

DEFAULT_DRAW_SPEED_MM_S = 100.0
DEFAULT_DRAW_ACCEL_MM_S2 = 5000.0
Z_HOP_RANGE_MM = (0.5, 20.0)
DRAW_SPEED_RANGE_MM_S = (5.0, 500.0)      # P1S M203 X/Y max 500
DRAW_ACCEL_RANGE_MM_S2 = (500.0, 20000.0)  # P1S M201 X/Y max 20000


def env_float_in_range(key: str, default: float, bounds: tuple[float, float]) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r} is not a number") from exc
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value:g} outside allowed range [{lo:g}, {hi:g}]")
    return value


def z_hop_for(profile: HolderProfile) -> float:
    """Pen lift above pen-down Z; env PDF_TO_PRINT_Z_HOP_MM overrides the holder default."""
    return env_float_in_range(Z_HOP_ENV, profile.z_hop, Z_HOP_RANGE_MM)


def draw_speed_mm_s() -> float:
    return env_float_in_range(DRAW_SPEED_ENV, DEFAULT_DRAW_SPEED_MM_S, DRAW_SPEED_RANGE_MM_S)


def draw_accel_mm_s2() -> float:
    return env_float_in_range(DRAW_ACCEL_ENV, DEFAULT_DRAW_ACCEL_MM_S2, DRAW_ACCEL_RANGE_MM_S2)
