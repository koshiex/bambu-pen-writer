#!/usr/bin/env python3
"""Smoke test: synthetic SVG -> svg_to_gcode -> validate_reading_order_gcode --strict.

Covers:
  - force_axis=x (portrait→landscape geometry: rows along gcode_X, left-right along gcode_Y)
  - Multiple strokes per text row with intentional DOM order != left-to-right (tie-break keys)
  - Two rows + single full-width line (vertical progression)
  - Rows output top-first (y_svg=14 first, y_svg=74 last)

Uses default reading-order settings (same as production, axis=x).
Exit 0 only if validation passes. No printer required.

Usage:
  python3 scripts/e2e_reading_order_pipeline.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Same y per “handwriting row”; DOM order shuffled so stable-sort-on-min-X-only would fail.
# Linemerge cannot join disjoint shorts at 0.05 mm tolerance.
SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="90" viewBox="0 0 220 90">
  <!-- Row A y=14: DOM order right -> left -> mid (must emit min-X ascending after sort) -->
  <line x1="175" y1="14" x2="205" y2="14" stroke="black" stroke-width="0.5"/>
  <line x1="8" y1="14" x2="38" y2="14" stroke="black" stroke-width="0.5"/>
  <line x1="115" y1="14" x2="145" y2="14" stroke="black" stroke-width="0.5"/>
  <line x1="88" y1="14" x2="108" y2="14" stroke="black" stroke-width="0.5"/>
  <!-- Row B y=44 -->
  <line x1="160" y1="44" x2="190" y2="44" stroke="black" stroke-width="0.5"/>
  <line x1="20" y1="44" x2="50" y2="44" stroke="black" stroke-width="0.5"/>
  <line x1="95" y1="44" x2="125" y2="44" stroke="black" stroke-width="0.5"/>
  <!-- Row C single sweep -->
  <line x1="10" y1="74" x2="200" y2="74" stroke="black" stroke-width="0.5"/>
</svg>
"""


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        svg_dir = t / "svg"
        gdir = t / "gcode"
        svg_dir.mkdir()
        gdir.mkdir()
        (svg_dir / "page_01.svg").write_text(SVG, encoding="utf-8")

        conv = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "svg_to_gcode.py"),
                "--skip-linemerge",
                "--reading-force-axis", "x",
                "--svg-dir",
                str(svg_dir),
                "--out-dir",
                str(gdir),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if conv.returncode != 0:
            print(conv.stdout, file=sys.stderr)
            print(conv.stderr, file=sys.stderr)
            sys.exit(conv.returncode)

        g = gdir / "page_01.gcode"
        if not g.exists():
            print("e2e FAIL: page_01.gcode missing", file=sys.stderr)
            sys.exit(1)

        val = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "validate_reading_order_gcode.py"),
                "--strict",
                "--quiet",
                "--reading-force-axis", "x",
                str(g),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if val.returncode != 0:
            print(val.stdout, file=sys.stderr)
            print(val.stderr, file=sys.stderr)
            sys.exit(val.returncode)

    print(
        "e2e OK: shuffled multi-stroke rows + strict validate (top-down rows, left-to-right ties)"
    )


if __name__ == "__main__":
    main()
