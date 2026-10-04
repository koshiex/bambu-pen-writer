#!/usr/bin/env python3
"""Smoke test: merge_pages derives M73 remaining time from the planner simulation.

Run: python3 scripts/test_merge_pages.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def stroke_block(x: float, n: int) -> str:
    lines = []
    for i in range(n):
        y = 100 + i * 2.0
        lines += [f"G0 X{x:.3f} Y{y:.3f} F30000", "G1 Z74.200 F1800",
                  f"G1 X{x + 5:.3f} Y{y:.3f} F6000", "G1 Z80.200 F1800"]
    return "\n".join(lines) + "\n"


def clean_env() -> dict[str, str]:
    """Operator tuning (PDF_TO_PRINT_*) must not leak into the expected numbers."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PDF_TO_PRINT_")}


def run_merge(gdir: Path, out: Path, *extra: str) -> str:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "merge_pages.py"), "--gcode-dir", str(gdir),
         "--templates-dir", str(ROOT / "templates"), "--out", str(out), "--soft-holder", *extra],
        check=True, capture_output=True, text=True, cwd=str(ROOT), env=clean_env(),
    )
    return out.read_text()


def test_m73_remaining_follows_simulated_page_times() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        gdir = t / "gcode"
        gdir.mkdir()
        (gdir / "page_01.gcode").write_text(stroke_block(60, 400))
        (gdir / "page_02.gcode").write_text(stroke_block(80, 100))
        text = run_merge(gdir, t / "out.gcode")
        header_min = int(re.search(r"total estimated time: (\d+)m", text).group(1))
        # 500 strokes × (2 × 6 mm Z hop ≈ 0.66 s + ~0.1 s draw/travel) ≈ 6.3 min
        assert 6 <= header_min <= 8, header_min
        r_values = [int(v) for v in re.findall(r"^M73 P\d+ R(\d+)", text, re.M)]
        assert r_values[0] == header_min, r_values
        assert r_values == sorted(r_values, reverse=True), r_values
        assert "; est page time:" in text
        assert len(re.findall(r"^M400 U1", text, re.M)) == 3  # install + 1 flip + final


def test_flat_minutes_per_page_still_supported() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        gdir = t / "gcode"
        gdir.mkdir()
        (gdir / "page_01.gcode").write_text(stroke_block(60, 10))
        text = run_merge(gdir, t / "out.gcode", "--minutes-per-page", "15")
        assert "total estimated time: 15m" in text


def test_merge_rejects_pages_built_for_other_holder() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        gdir = t / "gcode"
        gdir.mkdir()
        (gdir / "page_01.gcode").write_text(stroke_block(60, 3).replace("Z74.200", "Z40.700"))
        res = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "merge_pages.py"), "--gcode-dir", str(gdir),
             "--templates-dir", str(ROOT / "templates"), "--out", str(t / "out.gcode"),
             "--soft-holder"],
            capture_output=True, text=True, cwd=str(ROOT), env=clean_env(),
        )
        assert res.returncode != 0 and "holder" in res.stderr.lower(), res.stderr
        assert not (t / "out.gcode").exists()


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_merge_pages: {len(tests)} passed")


if __name__ == "__main__":
    main()
