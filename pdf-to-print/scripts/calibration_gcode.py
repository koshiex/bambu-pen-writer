#!/usr/bin/env python3
"""Generate a short Z-calibration G-code for live pen-depth tuning (Test 3).

Output: output/calibration.gcode

Pattern: 5 horizontal lines at different Y, ~50mm long each.
Total runtime ~30 seconds per iteration.

Wrapped in same Bambu format as notebook.gcode (HEADER + CONFIG + EXECUTABLE)
so it runs from SD card.

Usage:
  python3 scripts/calibration_gcode.py [--z-down 18.0] [--z-hop 3.0]

After each run:
  - lines clean and pen lifts on travel → DONE, lock pen at current depth
  - faint / no mark → push pen DOWN 1-2 mm in module spring → re-run
  - heavy / smudge → pull pen UP 1-2 mm → re-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Inherit constants from svg_to_gcode.py for consistency
sys.path.insert(0, str(Path(__file__).parent))
from svg_to_gcode import (  # noqa: E402
    Z_PEN_DOWN as DEFAULT_Z_DOWN,
    Z_HOP as DEFAULT_Z_HOP,
    PEN_OFFSET_X,
    PEN_OFFSET_Y,
)

# Safe pen-frame bounds — paper bounds for landscape (bed=256).
# LINES below are in PEN frame (where ink lands). Pipeline applies offset
# compensation when emitting gcode (gcode = nozzle frame).
SAFE_X_MIN = 24.0    # paper left edge (bed-relative)
SAFE_X_MAX = 229.0   # paper right edge (24 + 205)
SAFE_Y_MIN = 50.0    # paper front edge (bed-relative)
SAFE_Y_MAX = 215.0   # paper rear edge (50 + 165)

# Calibration pattern in PEN frame: 5 horizontal lines, 50 mm long, spaced 8 mm in Y.
# Coordinates centered well inside SAFE bounds.
LINES = [
    (80, 100, 130, 100),
    (80, 108, 130, 108),
    (80, 116, 130, 116),
    (80, 124, 130, 124),
    (80, 132, 130, 132),
]


def validate_bounds() -> None:
    """Fail loud if any calibration line exits the safe pen-frame area."""
    for x1, y1, x2, y2 in LINES:
        for x, y in ((x1, y1), (x2, y2)):
            if not (SAFE_X_MIN <= x <= SAFE_X_MAX):
                sys.exit(f"ERROR: calibration X={x} out of safe [{SAFE_X_MIN}, {SAFE_X_MAX}]")
            if not (SAFE_Y_MIN <= y <= SAFE_Y_MAX):
                sys.exit(f"ERROR: calibration Y={y} out of safe [{SAFE_Y_MIN}, {SAFE_Y_MAX}]")


def pen_to_nozzle(x: float, y: float) -> tuple[float, float]:
    """Convert pen-frame (X, Y) to nozzle-frame for gcode emission.
    Pen is at nozzle + PEN_OFFSET, so nozzle = pen - PEN_OFFSET."""
    return x - PEN_OFFSET_X, y - PEN_OFFSET_Y


def build_gcode(z_down: float, z_hop: float) -> str:
    z_up = z_down + z_hop
    body = []
    for x1_pen, y1_pen, x2_pen, y2_pen in LINES:
        x1, y1 = pen_to_nozzle(x1_pen, y1_pen)
        x2, y2 = pen_to_nozzle(x2_pen, y2_pen)
        body.append(f"G0 X{x1:.3f} Y{y1:.3f} F18000")
        body.append(f"G1 Z{z_down:.3f} F1200")
        body.append(f"G1 X{x2:.3f} Y{y2:.3f} F12000")
        body.append(f"G1 Z{z_up:.3f} F1200")
    drawing = "\n".join(body) + "\n"

    return f"""; HEADER_BLOCK_START
; BambuStudio 01.06.09.02
; model printing time: 1m 0s; total estimated time: 1m 0s
; total layer number: 1
; model label id: 117
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; printer_model = Bambu Lab P1S
; nozzle_diameter = 0.4
; layer_height = 0.10
; max_print_height = 250
; CONFIG_BLOCK_END

; EXECUTABLE_BLOCK_START
M73 P0 R1
M201 X20000 Y20000 Z500 E5000
M203 X500 Y500 Z20 E30
M204 P20000 R5000 T20000
M205 X9.00 Y9.00 Z3.00 E2.50
M106 S0
M106 P2 S0
M106 P3 S0
; FEATURE: Custom

;===== UMTS calibration (Test 3) — pen Z tuning =====
; Z_PEN_DOWN = {z_down}, Z_HOP = {z_hop}
; pen down at Z={z_down}, pen up (Z-hop) at Z={z_up}

M17                       ; enable steppers
G90
M83
M140 S0                   ; bed cold
M141 S0
M106 S0
M106 P2 S0
M106 P3 S0
M221 X0 Y0 Z0             ; soft endstops off
G28                       ; home
G1 Z25 F600               ; safe Z for module install
M104 S180                 ; nozzle 180°C (cold-extrusion guard)
M109 S180

;===== install pause =====
G0 X128 Y200 F18000
M400
M400 U1                   ; install UMTS + pen, then Resume

;===== draw 5 calibration lines =====
; CHANGE_LAYER
; Z_HEIGHT: {z_down:.2f}
; LAYER_HEIGHT: 0.10
M73 L1
{drawing}
;===== final pause =====
G1 Z50 F1200
G0 X128 Y200 F18000
M400
M73 P100 R0
M400 U1                   ; remove UMTS, then Stop on LCD

M104 S0
M84

; EXECUTABLE_BLOCK_END
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--z-down", type=float, default=DEFAULT_Z_DOWN,
                        help=f"pen-down nozzle Z (default {DEFAULT_Z_DOWN})")
    parser.add_argument("--z-hop", type=float, default=DEFAULT_Z_HOP,
                        help=f"Z-hop above pen-down (default {DEFAULT_Z_HOP})")
    parser.add_argument("--out", default="output/calibration.gcode")
    args = parser.parse_args()

    validate_bounds()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_gcode(args.z_down, args.z_hop))
    print(f"✅ {out}  (Z_DOWN={args.z_down}, Z_HOP={args.z_hop})")
    print(f"   Pattern: {len(LINES)} lines, X∈[{min(l[0] for l in LINES)}, "
          f"{max(l[2] for l in LINES)}], Y∈[{min(l[1] for l in LINES)}, "
          f"{max(l[3] for l in LINES)}] (within safe paper bounds)")
    print(f"   Est ~30 sec drawing per iteration")
    print(f"   Copy to SD, run, observe ink quality, adjust pen depth in module, repeat.")


if __name__ == "__main__":
    main()
