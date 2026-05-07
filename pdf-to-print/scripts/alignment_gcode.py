#!/usr/bin/env python3
"""Generate alignment template gcode.

Draws on a sacrificial big sheet placed on the bed. Result becomes a
permanent positioning template — keep that sheet, place notebook inside
the frame for repeatable alignment.

Pattern:
  - Outer rectangle = paper bounds (205×165 landscape), drawn 3× per side
    with small Y/X offsets for thicker, more visible line
  - Center crosshair (+ shape) at paper center (helps centering)
  - Asymmetric directional key: arrow pointing OUT of frame at rear-right
    corner — marks orientation so you know which way notebook goes

Usage:
  python3 scripts/alignment_gcode.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from svg_to_gcode import (  # noqa: E402
    Z_PEN_DOWN as DEFAULT_Z_DOWN,
    Z_HOP as DEFAULT_Z_HOP,
    PEN_OFFSET_X,
    PEN_OFFSET_Y,
)

# Paper bounds in PEN frame (where ink lands), bed-relative.
# Pipeline applies offset compensation at gcode emission (gcode = nozzle frame).
X_MIN = 24.0    # mm from bed left to paper left
X_MAX = 229.0   # 24 + 205
Y_MIN = 50.0    # mm from bed front to paper front
Y_MAX = 215.0   # 50 + 165


def pen_to_nozzle(x: float, y: float) -> tuple[float, float]:
    """Convert pen-frame (X, Y) to nozzle-frame for gcode emission."""
    return x - PEN_OFFSET_X, y - PEN_OFFSET_Y

# Stroke thickness — multiple passes with small offset = thicker line
STROKE_PASSES = 3
STROKE_OFFSET = 0.3  # mm between passes


def thick_line(x1: float, y1: float, x2: float, y2: float) -> list[str]:
    """Draw a line N times with small perpendicular offset for thicker mark.
    Input coords are in PEN frame; emitted gcode is in NOZZLE frame."""
    out = []
    # direction vector
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return out
    # perpendicular unit vector
    nx, ny = -dy / length, dx / length
    for i in range(STROKE_PASSES):
        # center the passes around the original line
        offset = (i - (STROKE_PASSES - 1) / 2) * STROKE_OFFSET
        ox = nx * offset
        oy = ny * offset
        # alternate direction each pass to avoid retract-travel between passes
        if i % 2 == 0:
            ax, ay, bx, by = x1 + ox, y1 + oy, x2 + ox, y2 + oy
        else:
            ax, ay, bx, by = x2 + ox, y2 + oy, x1 + ox, y1 + oy
        # Convert pen-frame to nozzle-frame for emission
        ax_n, ay_n = pen_to_nozzle(ax, ay)
        bx_n, by_n = pen_to_nozzle(bx, by)
        if i == 0:
            out.append(f"G0 X{ax_n:.3f} Y{ay_n:.3f} F18000")
            out.append(f"G1 Z{DEFAULT_Z_DOWN:.3f} F1200")
        else:
            out.append(f"G1 X{ax_n:.3f} Y{ay_n:.3f} F12000")
        out.append(f"G1 X{bx_n:.3f} Y{by_n:.3f} F12000")
    return out


def stroke_lift() -> list[str]:
    return [f"G1 Z{DEFAULT_Z_DOWN + DEFAULT_Z_HOP:.3f} F1200"]


def build_drawing() -> list[str]:
    body: list[str] = []

    # 1. Outer rectangle = paper bounds (4 sides)
    rect_sides = [
        (X_MIN, Y_MIN, X_MAX, Y_MIN),  # bottom (front edge)
        (X_MAX, Y_MIN, X_MAX, Y_MAX),  # right
        (X_MAX, Y_MAX, X_MIN, Y_MAX),  # top (rear edge)
        (X_MIN, Y_MAX, X_MIN, Y_MIN),  # left
    ]
    for x1, y1, x2, y2 in rect_sides:
        body.extend(thick_line(x1, y1, x2, y2))
        body.extend(stroke_lift())

    # 2. Center crosshair (+ shape, 20 mm arms)
    cx = (X_MIN + X_MAX) / 2
    cy = (Y_MIN + Y_MAX) / 2
    arm = 20.0
    body.extend(thick_line(cx - arm, cy, cx + arm, cy))
    body.extend(stroke_lift())
    body.extend(thick_line(cx, cy - arm, cx, cy + arm))
    body.extend(stroke_lift())

    # 3. Directional key — arrow drawn AT rear-right corner pointing INWARD.
    #    Marks "this is the rear-right corner" — notebook spine to rear,
    #    page-1 first stroke also tends to be near rear of frame.
    #    Asymmetric: only one corner has it, so you can't put notebook upside-down.
    arrow_size = 15.0
    ax, ay = X_MAX - 5, Y_MAX - 5  # arrow tip just inside corner
    # arrow shape: tip at (ax, ay), base 15 mm down-left
    body.extend(thick_line(ax, ay, ax - arrow_size, ay))
    body.extend(stroke_lift())
    body.extend(thick_line(ax, ay, ax, ay - arrow_size))
    body.extend(stroke_lift())
    body.extend(thick_line(ax, ay, ax - arrow_size, ay - arrow_size))
    body.extend(stroke_lift())

    return body


def build_gcode() -> str:
    drawing = "\n".join(build_drawing()) + "\n"

    return f"""; HEADER_BLOCK_START
; BambuStudio 01.06.09.02
; model printing time: 3m 0s; total estimated time: 3m 0s
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
M73 P0 R3
M201 X20000 Y20000 Z500 E5000
M203 X500 Y500 Z20 E30
M204 P20000 R5000 T20000
M205 X9.00 Y9.00 Z3.00 E2.50
M106 S0
M106 P2 S0
M106 P3 S0
; FEATURE: Custom

;===== Notebook alignment template =====
; Draws onto a sacrificial big sheet placed on bed.
; Sheet becomes permanent positioning template.
; Frame: paper bounds X∈[{X_MIN}, {X_MAX}], Y∈[{Y_MIN}, {Y_MAX}]
; Stroke: {STROKE_PASSES} passes × {STROKE_OFFSET}mm offset = thick visible line
; Z_PEN_DOWN={DEFAULT_Z_DOWN}, Z_HOP={DEFAULT_Z_HOP}

M17
G90
M83
M140 S0
M141 S0
M106 S0
M106 P2 S0
M106 P3 S0
M221 X0 Y0 Z0
G28
G1 Z25 F600
M104 S180
M109 S180

;===== install pause =====
G0 X128 Y200 F18000
M400
M400 U1                    ; install UMTS + pen, then Resume

; CHANGE_LAYER
; Z_HEIGHT: {DEFAULT_Z_DOWN:.2f}
; LAYER_HEIGHT: 0.10
M73 L1
{drawing}
;===== final pause =====
G1 Z50 F1200
G0 X128 Y200 F18000
M400
M73 P100 R0
M400 U1                    ; remove UMTS, then Stop on LCD

M104 S0
M84

; EXECUTABLE_BLOCK_END
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="output/alignment.gcode")
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_gcode())
    print(f"✅ {out}")
    print(f"   Frame: paper bounds 205×165 (landscape), {STROKE_PASSES}-pass thick stroke")
    print(f"   Center crosshair + rear-right corner arrow (orientation key)")
    print(f"   Z_PEN_DOWN={DEFAULT_Z_DOWN}, Z_HOP={DEFAULT_Z_HOP}")
    print(f"")
    print(f"   Workflow:")
    print(f"   1. Place big sacrificial sheet on bed (covering at least X=24..229, Y=52..217)")
    print(f"   2. Run output/alignment.gcode (~3 min)")
    print(f"   3. Keep that sheet as permanent template under magnetic layer / on bed")
    print(f"   4. Place notebook inside drawn frame (arrow at rear-right corner)")


if __name__ == "__main__":
    main()
