#!/usr/bin/env python3
"""Tests for printer_sim: whole-job G-code dry run against machine, pen and paper model.

Run: python3 scripts/test_printer_sim.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from printer_sim import SimConfig, StopBox, envelope_of, simulate_job  # noqa: E402

ZD, ZU, ZC = 74.2, 80.2, 93.5          # soft holder pen-down, hop 6, clearance
OX, OY = -38.34, -21.13                # soft holder pen offset (pen = nozzle + offset)


def nz(x_pen: float, y_pen: float) -> tuple[float, float]:
    return x_pen - OX, y_pen - OY


def stroke(pts_pen: list[tuple[float, float]], z_up: float = ZU) -> list[str]:
    x0, y0 = nz(*pts_pen[0])
    out = [f"G0 X{x0:.3f} Y{y0:.3f} F30000", f"G1 Z{ZD:.3f} F1800"]
    out += [f"G1 X{nz(x, y)[0]:.3f} Y{nz(x, y)[1]:.3f} F6000" for x, y in pts_pen[1:]]
    out.append(f"G1 Z{z_up:.3f} F1800")
    return out


def pause() -> list[str]:
    return [f"G1 Z{ZC} F1800", "G0 X128 Y128 F30000", "M400", "M400 U1", "M109 S180"]


def job(pages: list[list[str]], start: list[str] | None = None) -> list[str]:
    lines = start if start is not None else ["G90", "M83", "G28", f"G1 Z{ZC} F1800",
                                             "M104 S180", "M109 S180",
                                             "G0 X128 Y128 F30000", "M400", "M400 U1"]
    for i, page in enumerate(pages, start=1):
        lines = lines + [f";===== PAGE {i:02d} ====="] + page
        if i < len(pages):
            lines = lines + pause()
    return lines + pause() + ["M104 S0", "M84"]


PAGE_A = stroke([(60, 100), (80, 100), (80, 120)]) + stroke([(100, 100), (110, 105)])
PAGE_B = stroke([(150, 180), (160, 190)])
CFG = SimConfig(holder="soft")


def codes(report) -> set[str]:
    return {i.code for i in report.errors}


def test_clean_job_passes_and_counts_pages() -> None:
    r = simulate_job(job([PAGE_A, PAGE_B]), CFG)
    assert not r.errors, r.errors
    assert [p.number for p in r.pages] == [1, 2]
    assert r.pages[0].pen_downs == 2 and r.pages[1].pen_downs == 1
    assert abs(r.pages[0].ink_mm - (20 + 20 + 11.18)) < 0.1, r.pages[0].ink_mm
    assert r.pauses == 3


def test_travel_with_pen_down_is_a_drag() -> None:
    bad = PAGE_A[:4] + [f"G0 X{nz(120, 140)[0]:.3f} Y{nz(120, 140)[1]:.3f} F30000"] + PAGE_A[4:]
    r = simulate_job(job([bad]), CFG)
    assert "DRAG" in codes(r), r.errors


def test_hop_smaller_than_spring_compression_drags() -> None:
    low = stroke([(60, 100), (70, 100)], z_up=ZD + 0.5) + stroke([(90, 100), (95, 100)])
    r = simulate_job(job([low]), SimConfig(holder="soft", pen_down_compression_mm=1.0))
    assert "DRAG" in codes(r), r.errors
    assert r.min_travel_clearance_mm < 0


def test_ink_outside_paper_is_reported() -> None:
    r = simulate_job(job([stroke([(26, 100), (20, 100)])]), CFG)
    assert "INK_OFF_PAPER" in codes(r), r.errors


def test_homing_with_pen_mounted_is_an_error() -> None:
    r = simulate_job(job([PAGE_A + ["G28"]]), CFG)
    assert "HOME_WITH_PEN" in codes(r), r.errors


def test_pause_at_low_z_is_an_error() -> None:
    lines = job([PAGE_A, PAGE_B])
    i = lines.index(f"G1 Z{ZC} F1800", lines.index(";===== PAGE 01 ====="))
    lines = lines[:i] + lines[i + 1:]           # park without lifting to clearance
    r = simulate_job(lines, CFG)
    assert {"PAUSE_LOW_Z"} & codes(r), r.errors


def test_pen_crush_below_spring_travel() -> None:
    deep = [f"G0 X{nz(60, 100)[0]:.3f} Y{nz(60, 100)[1]:.3f}", f"G1 Z{ZD - 6:.3f} F1800",
            f"G1 X{nz(70, 100)[0]:.3f} Y{nz(70, 100)[1]:.3f} F6000", f"G1 Z{ZU:.3f}"]
    r = simulate_job(job([deep]), SimConfig(holder="soft", spring_travel_mm=4.0))
    assert "PEN_CRUSH" in codes(r), r.errors


def test_tall_stop_near_text_collides_low_stop_does_not() -> None:
    page = stroke([(60, 52), (120, 52)])       # text 2 mm from the paper front edge
    tall = StopBox(x0=20, y0=40, x1=235, y1=50, height_mm=20.0)
    low = StopBox(x0=20, y0=40, x1=235, y1=50, height_mm=2.0)
    cfg = dict(holder="soft", paper_thickness_mm=3.0, pen_protrusion_mm=10.0, holder_radius_mm=12.0)
    assert "STOP_COLLISION" in codes(simulate_job(job([page]), SimConfig(**cfg, stops=(tall,))))
    assert not simulate_job(job([page]), SimConfig(**cfg, stops=(low,))).errors


def test_moves_outside_proven_envelope_are_reported() -> None:
    reference = job([PAGE_A])
    wider = job([PAGE_A + stroke([(200, 200), (228, 210)])])
    env = envelope_of(reference)
    assert not simulate_job(reference, CFG, reference_envelope=env).errors
    assert "OUT_OF_ENVELOPE" in codes(simulate_job(wider, CFG, reference_envelope=env))


def test_pause_count_must_match_pages() -> None:
    lines = job([PAGE_A, PAGE_B])
    lines.remove("M400 U1")                     # drop the install pause
    r = simulate_job(lines, CFG)
    assert "PAUSE_COUNT" in codes(r) or "HOME_WITH_PEN" in codes(r) or "INK_BEFORE_INSTALL" in codes(r)


def test_unknown_command_is_a_warning() -> None:
    r = simulate_job(job([PAGE_A + ["M999 S1"]]), CFG)
    assert not r.errors
    assert any(w.code == "UNKNOWN_COMMAND" for w in r.warnings), r.warnings


def test_relative_mode_is_tracked() -> None:
    rel = [f"G0 X{nz(60, 100)[0]:.3f} Y{nz(60, 100)[1]:.3f} F30000", f"G1 Z{ZD:.3f}",
           "G91", "G1 X10 Y0 F6000", "G90", f"G1 Z{ZU:.3f}"]
    r = simulate_job(job([rel]), CFG)
    assert not r.errors, r.errors
    assert abs(r.pages[0].ink_mm - 10.0) < 1e-6


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_printer_sim: {len(tests)} passed")


if __name__ == "__main__":
    main()
