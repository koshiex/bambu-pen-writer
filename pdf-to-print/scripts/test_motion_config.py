#!/usr/bin/env python3
"""Smoke tests for motion settings (Z-hop / draw speed / draw acceleration env overrides).

Run: python3 scripts/test_motion_config.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import holder_config as hc  # noqa: E402

ENV_KEYS = ("PDF_TO_PRINT_Z_HOP_MM", "PDF_TO_PRINT_DRAW_SPEED_MM_S", "PDF_TO_PRINT_DRAW_ACCEL_MM_S2")


@contextmanager
def env(**values: str):
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    try:
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(values)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def raises_value_error(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def test_defaults() -> None:
    with env():
        assert hc.z_hop_for(hc.SOFT_HOLDER) == hc.SOFT_HOLDER.z_hop == 2.5   # hop-ladder 2026-10-04
        assert hc.z_hop_for(hc.UMTS) == 12.0
        assert hc.draw_speed_mm_s() == 100.0
        assert hc.draw_accel_mm_s2() == 5000.0


def test_env_overrides() -> None:
    with env(PDF_TO_PRINT_Z_HOP_MM="2.5", PDF_TO_PRINT_DRAW_SPEED_MM_S="150",
             PDF_TO_PRINT_DRAW_ACCEL_MM_S2="10000"):
        assert hc.z_hop_for(hc.SOFT_HOLDER) == 2.5
        assert hc.z_hop_for(hc.UMTS) == 2.5
        assert hc.draw_speed_mm_s() == 150.0
        assert hc.draw_accel_mm_s2() == 10000.0


def test_invalid_values_fail_fast() -> None:
    with env(PDF_TO_PRINT_Z_HOP_MM="abc"):
        assert raises_value_error(lambda: hc.z_hop_for(hc.SOFT_HOLDER))
    with env(PDF_TO_PRINT_Z_HOP_MM="0.1"):
        assert raises_value_error(lambda: hc.z_hop_for(hc.SOFT_HOLDER))
    with env(PDF_TO_PRINT_DRAW_SPEED_MM_S="900"):
        assert raises_value_error(hc.draw_speed_mm_s)
    with env(PDF_TO_PRINT_DRAW_ACCEL_MM_S2="50"):
        assert raises_value_error(hc.draw_accel_mm_s2)


def test_svg_to_gcode_uses_hop_and_speed() -> None:
    import svg_to_gcode as plotter

    with env(PDF_TO_PRINT_Z_HOP_MM="3", PDF_TO_PRINT_DRAW_SPEED_MM_S="120"):
        plotter.apply_holder_profile(soft_holder=True)
        assert plotter.Z_HOP == 3.0
        assert plotter.DRAW_SPEED_MM_S == 120.0
        assert plotter.DRAW_FEED_MM_MIN == 7200
    with env():
        plotter.apply_holder_profile(soft_holder=True)
        assert plotter.Z_HOP == 2.5


def test_merge_emits_draw_acceleration() -> None:
    import merge_pages

    with env(PDF_TO_PRINT_DRAW_ACCEL_MM_S2="7000"):
        block = merge_pages.motion_limits()
    assert "M204 S7000" in block, block
    assert block.index("M204 P20000") < block.index("M204 S7000")


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_motion_config: {len(tests)} passed")


if __name__ == "__main__":
    main()
