#!/usr/bin/env python3
"""Tests for the motion preview backend (per-page timeline of a job on the P1S bed).

Run: python3 scripts/test_webui_motion.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from plot_time_sim import estimate_lines, timeline  # noqa: E402
from webui.motion import job_overview, page_motion  # noqa: E402

OX, OY = -38.34, -21.13   # soft holder: pen = nozzle + offset


def job_text() -> str:
    lines = ["; HEADER", "M204 S5000", "G28", "G1 Z93.5 F1800", "G0 X128 Y128 F30000", "M400 U1"]
    for i, page in enumerate((1, 2), start=1):
        lines += [f";===== PAGE {page:02d} =====", f"M73 L{i}",
                  "G0 X100 Y100 F30000", "G1 Z74.200 F1800",
                  "G1 X110 Y100 F6000", "G1 X110 Y105 F6000", "G1 Z80.200 F1800",
                  "G0 X120 Y100 F30000", "G1 Z74.200 F1800", "G1 X125 Y100 F6000", "G1 Z80.200 F1800",
                  "G1 Z93.5 F1800", "G0 X128 Y128 F30000", "M400", "M400 U1",
                  f";===== PAGE {page + 1:02d} =====" if page == 1 else ""]
    lines += ["M104 S0"]
    return "\n".join(lines) + "\n"


def test_timeline_matches_total_and_is_monotonic() -> None:
    lines = job_text().splitlines()
    moves = timeline(lines, start=(128.0, 128.0, 93.5))
    total = estimate_lines(lines, start=(128.0, 128.0, 93.5)).total
    assert abs(moves[-1].t1 - total) < 1e-6, (moves[-1].t1, total)
    assert all(a.t1 <= b.t0 + 1e-9 for a, b in zip(moves, moves[1:]))
    assert {m.kind for m in moves} == {"draw", "travel", "z"}


def test_overview_lists_pages_in_print_order_with_durations() -> None:
    with tempfile.TemporaryDirectory() as td:
        g = Path(td) / "nb.gcode"
        g.write_text(job_text())
        ov = job_overview(g, holder="soft")
        assert [p["page"] for p in ov["pages"]] == [1, 2], ov
        assert all(p["seconds"] > 0 for p in ov["pages"])
        assert ov["geometry"]["bed"] == [256, 256]


def test_page_motion_in_pen_frame_with_kinds_and_pause() -> None:
    with tempfile.TemporaryDirectory() as td:
        g = Path(td) / "nb.gcode"
        g.write_text(job_text())
        m = page_motion(g, 1, holder="soft")
        n = len(m["k"])
        assert len(m["x"]) == len(m["y"]) == len(m["z"]) == len(m["t"]) == n + 1
        assert set(m["k"]) <= {"d", "t", "z"} and "d" in m["k"] and "t" in m["k"]
        first_draw = m["k"].index("d")
        assert abs(m["x"][first_draw] - (100 + OX)) < 0.01 and abs(m["y"][first_draw] - (100 + OY)) < 0.01
        assert all(a <= b for a, b in zip(m["t"], m["t"][1:]))
        assert m["pauses"] and "страницу 02" in m["pauses"][-1]["label"], m["pauses"]
        assert m["stats"]["pen_downs"] == 2 and abs(m["stats"]["ink_mm"] - 20.0) < 0.01


def test_page_motion_maps_report_errors_to_pages() -> None:
    with tempfile.TemporaryDirectory() as td:
        g = Path(td) / "nb.gcode"
        g.write_text(job_text())
        line_in_page2 = job_text().splitlines().index(";===== PAGE 02 =====", 20) + 4
        g.with_name("nb.sim.md").write_text(
            "# Printer simulation: nb.gcode — FAIL\n\n## Errors (1)\n"
            f"- `DRAG` line {line_in_page2}: travel with pen on paper (3.00 mm)\n")
        assert page_motion(g, 1, holder="soft")["issues"] == []
        issues = page_motion(g, 2, holder="soft")["issues"]
        assert issues and issues[0]["code"] == "DRAG", issues


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_webui_motion: {len(tests)} passed")


if __name__ == "__main__":
    main()
