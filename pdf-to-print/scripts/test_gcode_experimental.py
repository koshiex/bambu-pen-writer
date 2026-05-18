#!/usr/bin/env python3
"""Smoke test for experimental G-code post-process."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from gcode_experimental import (  # noqa: E402
    ExperimentalFlags,
    apply_experimental_postprocess,
    resolve_experimental_params,
)
from gcode_stroke_parse import (  # noqa: E402
    EXPERIMENTAL_STROKES_MARKER,
    parse_stroke_blocks,
)

Z_PEN = 40.7
Z_UP = 52.7
BASE_F = 24000


def _minimal_gcode() -> str:
  return "\n".join(
      [
          "G0 X100.000 Y100.000 F18000",
          "G1 Z40.700 F1200",
          "G1 X110.000 Y100.000 F24000",
          "G1 X120.000 Y100.000 F24000",
          "G1 Z52.700 F1200",
          "G0 X100.000 Y110.000 F18000",
          "G1 Z40.700 F1200",
          "G1 X110.000 Y110.000 F24000",
          "G1 X120.000 Y110.000 F24000",
          "G1 Z52.700 F1200",
          "",
      ]
  )


def _z_values(text: str) -> list[float]:
    return [float(m.group(1)) for m in re.finditer(r"Z\s*([-+]?\d+\.?\d*)", text, re.I)]


def _f_values(text: str) -> list[float]:
    return [float(m.group(1)) for m in re.finditer(r"F\s*([-+]?\d+\.?\d*)", text, re.I)]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.gcode"
        path.write_text(_minimal_gcode(), encoding="utf-8")

        apply_experimental_postprocess(
            path,
            z_pen=Z_PEN,
            z_up=Z_UP,
            base_feed_mm_min=float(BASE_F),
            base_speed_mm_s=400.0,
            flags=ExperimentalFlags(
                variable_pressure=True,
                variable_feedrate=True,
                strikethrough=False,
            ),
            params=resolve_experimental_params(),
            reading_row_gap_mm=2.5,
            reading_force_axis="x",
            invert_reading_sort=False,
        )
        text = path.read_text(encoding="utf-8")
        blocks, trailing = parse_stroke_blocks(path, Z_PEN, Z_UP)
        assert len(blocks) == 2, f"expected 2 strokes, got {len(blocks)}"

        draw_z = []
        for b in blocks:
            for idx in b.drawing_indices:
                ln = b.lines[idx]
                m = re.search(r"Z\s*([-+]?\d+\.?\d*)", ln, re.I)
                if m:
                    draw_z.append(float(m.group(1)))
        assert len(draw_z) >= 2, "expected Z on drawing moves"
        assert max(draw_z) - min(draw_z) > 0.01, "variable pressure should spread Z"

        draw_f = []
        for b in blocks:
            for idx in b.drawing_indices:
                m = re.search(r"F\s*([-+]?\d+\.?\d*)", b.lines[idx], re.I)
                if m:
                    draw_f.append(float(m.group(1)))
        assert len(draw_f) >= 2
        assert max(draw_f) - min(draw_f) > 1.0, "variable feedrate should spread F"

        assert EXPERIMENTAL_STROKES_MARKER not in text

        # Strikethrough: 4 strokes, one text row (axis=x: shared gcode X, nearby gcode Y)
        strike_gcode = "\n".join(
            [
                "G0 X200.000 Y100.000 F18000",
                "G1 Z40.700 F1200",
                "G1 X104.000 Y100.000 F24000",
                "G1 Z52.700 F1200",
                "G0 X200.000 Y100.150 F18000",
                "G1 Z40.700 F1200",
                "G1 X108.000 Y100.150 F24000",
                "G1 Z52.700 F1200",
                "G0 X200.000 Y100.080 F18000",
                "G1 Z40.700 F1200",
                "G1 X112.000 Y100.080 F24000",
                "G1 Z52.700 F1200",
                "G0 X200.000 Y100.220 F18000",
                "G1 Z40.700 F1200",
                "G1 X116.000 Y100.220 F24000",
                "G1 Z52.700 F1200",
                "",
            ]
        )
        path.write_text(strike_gcode, encoding="utf-8")
        strike_params = resolve_experimental_params()
        strike_params.rng_seed = 0
        strike_params.strike_probability = 100.0
        apply_experimental_postprocess(
            path,
            z_pen=Z_PEN,
            z_up=Z_UP,
            base_feed_mm_min=float(BASE_F),
            base_speed_mm_s=400.0,
            flags=ExperimentalFlags(strikethrough=True),
            params=strike_params,
            reading_row_gap_mm=2.5,
            reading_force_axis="x",
            invert_reading_sort=False,
        )
        assert EXPERIMENTAL_STROKES_MARKER in path.read_text(encoding="utf-8")
        blocks2, _ = parse_stroke_blocks(path, Z_PEN, Z_UP)
        assert len(blocks2) >= 5, "expected base strokes + strikethrough"

    print("test_gcode_experimental: OK")


if __name__ == "__main__":
    main()
