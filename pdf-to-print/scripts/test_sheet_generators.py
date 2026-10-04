#!/usr/bin/env python3
"""Smoke tests for calibration_sheets_gcode (calibration sheets for speed-up tuning).

Run: python3 scripts/test_sheet_generators.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "calibration_sheets_gcode.py"
sys.path.insert(0, str(ROOT / "scripts"))

import svg_to_gcode as plotter  # noqa: E402
from holder_config import SOFT_HOLDER, z_travel_clearance_for_profile  # noqa: E402
from calibration_sheets_gcode import HOP_LADDER_MM, SPEED_GRID  # noqa: E402

Z_WORD = re.compile(r"Z(-?\d+\.?\d*)")
XY_WORD = re.compile(r"X(-?\d+\.?\d*) Y(-?\d+\.?\d*)")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PDF_TO_PRINT_")}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(ROOT),
                          capture_output=True, text=True, check=check, env=env)


def body(text: str) -> list[str]:
    """Executable lines (comments stripped)."""
    code = (ln.split(";")[0].strip() for ln in text.splitlines())
    return [ln for ln in code if ln]


def pauses(text: str) -> int:
    return len(re.findall(r"^M400 U1", text, re.M))


def pen_frame_xy(lines: list[str]) -> list[tuple[float, float]]:
    plotter.apply_holder_profile(soft_holder=True)
    out = []
    for ln in lines:
        m = XY_WORD.search(ln)
        if m and ln.startswith(("G0", "G1")):
            out.append((float(m.group(1)) + plotter.PEN_OFFSET_X,
                        float(m.group(2)) + plotter.PEN_OFFSET_Y))
    return out


def test_holder_is_mandatory() -> None:
    with tempfile.TemporaryDirectory() as td:
        res = run("zbench", "--out", str(Path(td) / "z.gcode"), check=False)
        assert res.returncode != 0


def test_zbench_moves_only_z_at_clearance() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "z.gcode"
        run("zbench", "--holder", "soft", "--hops", "20", "--out", str(out))
        text = out.read_text()
        z_clear = z_travel_clearance_for_profile(SOFT_HOLDER)
        section = text[text.index("ZBENCH A"):text.index("; EXECUTABLE_BLOCK_END")]
        zs = [float(m.group(1)) for m in Z_WORD.finditer(section)]
        assert min(zs) >= z_clear - 1e-6, (min(zs), z_clear)
        assert max(zs) <= z_clear + 6.0 + 1e-6
        z_only = [ln for ln in body(section) if ln.startswith("G1 Z")]
        assert len(z_only) >= 3 * 2 * 20, len(z_only)
        assert pauses(text) == 4  # install + 2 between blocks + final


def test_hop_ladder_uses_each_hop_inside_paper() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "h.gcode"
        run("hop-ladder", "--holder", "soft", "--out", str(out))
        text = out.read_text()
        plotter.apply_holder_profile(soft_holder=True)
        z_down = plotter.Z_PEN_DOWN
        lines = body(text[text.index("HOP LADDER"):text.index("; EXECUTABLE_BLOCK_END")])
        ups = {round(float(Z_WORD.search(b).group(1)) - z_down, 3)
               for a, b in zip(lines, lines[1:])
               if a.startswith("G1 X") and b.startswith("G1 Z") and "X" not in b}
        assert set(HOP_LADDER_MM) <= ups, (sorted(ups), HOP_LADDER_MM)
        drawn = [ln for ln in lines if ln.startswith(("G0 X", "G1 X"))]
        for x, y in pen_frame_xy(drawn):
            assert plotter.PAPER_LEFT + 5 <= x <= plotter.PAPER_LEFT + plotter.PAPER_W - 5, x
            assert plotter.PAPER_FRONT + 5 <= y <= plotter.PAPER_FRONT + plotter.PAPER_H - 5, y


def test_speed_grid_repeats_source_row_per_setting() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        src = t / "page_01.gcode"
        plotter.apply_holder_profile(soft_holder=True)
        zd, zu = plotter.Z_PEN_DOWN, plotter.Z_PEN_DOWN + plotter.Z_HOP
        strokes = []
        for i in range(6):
            y = 230 - i * 4
            strokes += [f"G0 X240.000 Y{y:.3f} F30000", f"G1 Z{zd:.3f} F1800",
                        f"G1 X236.000 Y{y - 2:.3f} F6000", f"G1 Z{zu:.3f} F1800"]
        src.write_text("\n".join(strokes) + "\n")
        out = t / "s.gcode"
        run("speed", "--holder", "soft", "--source", str(src), "--out", str(out))
        text = out.read_text()
        for v, a in SPEED_GRID:
            assert f"M204 S{a}" in text, a
            assert f"F{int(v * 60)}" in text, v
        grid = text[text.index("SPEED GRID"):text.index("; EXECUTABLE_BLOCK_END")]
        downs = [ln for ln in body(grid) if ln == f"G1 Z{zd:.3f} F1800"]
        assert len(downs) >= 6 * len(SPEED_GRID), len(downs)


def test_page_wraps_single_page_with_estimate() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        src = t / "page_05.gcode"
        plotter.apply_holder_profile(soft_holder=True)
        zd, zu = plotter.Z_PEN_DOWN, plotter.Z_PEN_DOWN + plotter.Z_HOP
        src.write_text(f"G0 X150 Y150 F30000\nG1 Z{zd:.3f} F1800\nG1 X160 Y150 F6000\n"
                       f"G1 Z{zu:.3f} F1800\n")
        out = t / "p.gcode"
        run("page", "--holder", "soft", "--gcode", str(src), "--out", str(out))
        text = out.read_text()
        assert "; est page time:" in text
        assert pauses(text) == 2  # install + final


def test_edge_check_traces_text_bounding_box_slowly() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        gdir = t / "gcode"
        gdir.mkdir()
        plotter.apply_holder_profile(soft_holder=True)
        zd, zu = plotter.Z_PEN_DOWN, plotter.Z_PEN_DOWN + plotter.Z_HOP
        (gdir / "page_01.gcode").write_text(
            f"G0 X100 Y120 F30000\nG1 Z{zd:.3f} F1800\nG1 X150 Y130 F6000\nG1 Z{zu:.3f} F1800\n")
        (gdir / "page_02.gcode").write_text(
            f"G0 X90 Y200 F30000\nG1 Z{zd:.3f} F1800\nG1 X95 Y210 F6000\nG1 Z{zu:.3f} F1800\n")
        out = t / "e.gcode"
        run("edge-check", "--holder", "soft", "--gcode-dir", str(gdir), "--out", str(out))
        text = out.read_text()
        sec = body(text[text.index("EDGE CHECK"):text.index("; EXECUTABLE_BLOCK_END")])
        corners = {(float(m.group(1)), float(m.group(2)))
                   for ln in sec if ln.startswith(("G0 X", "G1 X")) for m in [XY_WORD.search(ln)]}
        assert {(90.0, 120.0), (150.0, 120.0), (150.0, 210.0), (90.0, 210.0)} <= corners, corners
        assert all("F1200" in ln for ln in sec if ln.startswith("G1 X")), sec
        assert pauses(text) == 2


def _umts_page(path: Path) -> None:
    path.write_text("G0 X150 Y150 F30000\nG1 Z40.700 F1800\nG1 X160 Y150 F6000\nG1 Z52.700 F1800\n")


def test_page_rejects_pages_built_for_other_holder() -> None:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "page_05.gcode"
        _umts_page(src)
        res = run("page", "--holder", "soft", "--gcode", str(src), "--out", str(Path(td) / "p.gcode"),
                  check=False)
        assert res.returncode != 0 and "holder" in res.stderr.lower(), res.stderr
        assert not (Path(td) / "p.gcode").exists()


def test_edge_check_rejects_pages_built_for_other_holder() -> None:
    with tempfile.TemporaryDirectory() as td:
        gdir = Path(td) / "gcode"
        gdir.mkdir()
        _umts_page(gdir / "page_01.gcode")
        res = run("edge-check", "--holder", "soft", "--gcode-dir", str(gdir),
                  "--out", str(Path(td) / "e.gcode"), check=False)
        assert res.returncode != 0 and "holder" in res.stderr.lower(), res.stderr


def test_speed_reads_pages_built_with_another_hop() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        src = t / "page_01.gcode"
        plotter.apply_holder_profile(soft_holder=True)
        zd = plotter.Z_PEN_DOWN
        lines = []
        for i in range(4):
            y = 230 - i * 4
            lines += [f"G0 X240.000 Y{y:.3f} F30000", f"G1 Z{zd:.3f} F1800",
                      f"G1 X236.000 Y{y - 2:.3f} F6000", f"G1 Z{zd + 2.5:.3f} F1800"]
        src.write_text("\n".join(lines) + "\n")
        res = run("speed", "--holder", "soft", "--source", str(src), "--out", str(t / "s.gcode"),
                  check=False)
        assert res.returncode == 0, res.stderr


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_sheet_generators: {len(tests)} passed")


if __name__ == "__main__":
    main()
