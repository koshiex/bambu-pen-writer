#!/usr/bin/env python3
"""Calibration sheets for the speed-up tuning (see docs/operator-manual.md §2.5).

Subcommands (all wrapped like notebook.gcode: Bambu header/config, start with install pause,
production pause template between blocks, end with final pause):

  zbench      No pen/paper needed. Z-only hop blocks at safe clearance, a short beep at the start
              and a double beep at the end of each block — time them with a stopwatch.
              A: 3 mm hops, B: 6 mm hops (current mode, expect Standard), C: 3 mm hops after you
              switch the LCD speed mode to Ludicrous. Shows whether speed modes speed up Z and how
              the planner model compares with reality.
  hop-ladder  Sacrificial sheet where the notebook goes. 7 blocks, block k (k+1 tally marks under
              it) crosses freshly drawn lines with pen lift HOP_LADDER_MM[k]. Pick the smallest hop
              without drag marks, add 0.5 mm margin → PDF_TO_PRINT_Z_HOP_MM.
  speed       Sacrificial sheet. First text row of a real page repeated with SPEED_GRID
              (speed mm/s, accel mm/s²) settings, copy k has k+1 tally marks after it.
              Pick the fastest copy that looks identical → PDF_TO_PRINT_DRAW_SPEED_MM_S / _ACCEL_.
  page        One page G-code wrapped alone — time it to calibrate PDF_TO_PRINT_TIME_FACTOR.
  edge-check  Notebook and L-stop in place, holder mounted WITHOUT the pen (its body sits exactly
              at writing height). Traces the bounding box of all pages at pen-down Z, 20 mm/s:
              if the holder clears the stop here, it clears it during the whole notebook.

--holder is mandatory: the two holders differ by 33.5 mm in pen-down Z.

Usage:
  python3 scripts/calibration_sheets_gcode.py zbench --holder soft
  python3 scripts/calibration_sheets_gcode.py hop-ladder --holder soft
  python3 scripts/calibration_sheets_gcode.py speed --holder soft --source build/9/gcode/page_01.gcode
  python3 scripts/calibration_sheets_gcode.py page --holder soft --gcode build/9/gcode/page_05.gcode
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_to_gcode as plotter  # noqa: E402
from gcode_stroke_parse import parse_stroke_polylines, require_pen_down  # noqa: E402
from holder_config import (  # noqa: E402
    draw_accel_mm_s2,
    select_profile,
    travel_feed_mm_min,
    z_travel_clearance_for_profile,
    z_travel_feed_mm_min,
)
from merge_pages import (  # noqa: E402
    job_templates,
    layer_marker,
    motion_limits,
    patch_header,
    pause_beep_enabled,
    read,
)
from plot_time_sim import estimate_file, estimate_lines  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"

HOP_LADDER_MM = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0)
SPEED_GRID = ((50, 3000), (100, 5000), (150, 8000), (250, 15000), (500, 20000))
ZBENCH_BLOCKS = (
    ("A", 3.0, "current LCD speed mode (expect Standard)"),
    ("B", 6.0, "same mode"),
    ("C", 3.0, "switch LCD speed mode to Ludicrous before Resume"),
)
SAFE_HOP_MM = 6.0            # known-good lift between test blocks
SPEED_ROW_PITCH_MM = 14.0    # distance between speed-grid copies (across rows)
SPEED_ROW_LENGTH_MM = 60.0   # length of the text row sample
PAPER_MARGIN_MM = 5.0
EDGE_CHECK_FEED = 1200       # 20 mm/s: slow enough to stop the job before a real collision

BEEP_START = "M400\nM1006 S1\nM1006 A72 B10 L150 C0 D0 M150 E0 F0 N150\nM1006 W\n"
BEEP_END = ("M400\nM1006 S1\nM1006 A72 B10 L150 C0 D0 M150 E0 F0 N150\n"
            "M1006 A0 B10 L100 C0 D0 M100 E0 F0 N100\n"
            "M1006 A72 B10 L150 C0 D0 M150 E0 F0 N150\nM1006 W\n")


@dataclass
class Section:
    title: str
    gcode: str
    resume_hint: str = ""   # shown in the pause that follows this section


class PenWriter:
    """Emit strokes given in PEN frame (where ink lands); G-code targets the nozzle."""

    def __init__(self, draw_feed: int) -> None:
        self.z_down = plotter.Z_PEN_DOWN
        self.draw_feed = draw_feed
        self.lines: list[str] = []

    def nozzle(self, x: float, y: float) -> tuple[float, float]:
        return x - plotter.PEN_OFFSET_X, y - plotter.PEN_OFFSET_Y

    def stroke(self, pts_pen: list[tuple[float, float]], hop: float) -> None:
        x0, y0 = self.nozzle(*pts_pen[0])
        self.lines.append(f"G0 X{x0:.3f} Y{y0:.3f} F{travel_feed_mm_min()}")
        self.lines.append(f"G1 Z{self.z_down:.3f} F{z_travel_feed_mm_min()}")
        for x, y in pts_pen[1:]:
            nx, ny = self.nozzle(x, y)
            self.lines.append(f"G1 X{nx:.3f} Y{ny:.3f} F{self.draw_feed}")
        self.lift(hop)

    def lift(self, hop: float) -> None:
        self.lines.append(f"G1 Z{self.z_down + hop:.3f} F{z_travel_feed_mm_min()}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------- job wrapper

def pause_text(pause_tpl: str, step: int, hint: str) -> str:
    tpl = pause_tpl.replace("{NEXT_PAGE}", f"{step:02d}").replace("{PROGRESS}", "0")
    tpl = tpl.replace("{REMAINING}", "0")
    return f"; ===== TEST SHEET PAUSE → next: {hint} =====\n" + tpl


def job_text(sections: list[Section], soft: bool) -> str:
    profile = select_profile(soft_holder=soft)
    z_clear = z_travel_clearance_for_profile(profile)
    start, pause_tpl, end = job_templates(TEMPLATES, z_clear, pause_beep_enabled())
    minutes = sum(estimate_lines(["M204 S%g" % draw_accel_mm_s2()] + s.gcode.splitlines(),
                                 start=(0.0, 0.0, z_clear)).total for s in sections) / 60.0
    total = max(1, int(math.ceil(minutes)))
    parts = [patch_header(read(TEMPLATES / "bambu_header_block.gcode"), len(sections), total),
             "\n", read(TEMPLATES / "bambu_config_block.gcode"), "\n",
             "; EXECUTABLE_BLOCK_START\n", f"M73 P0 R{total}\n", motion_limits(), start, "\n"]
    for i, sec in enumerate(sections, start=1):
        # page marker: printer_sim counts sections like notebook pages (pauses = sections + 1)
        parts += [f";===== PAGE {i:02d} =====\n", layer_marker(i),
                  f"; ===== {sec.title} =====\n", sec.gcode]
        if i < len(sections):
            parts.append(pause_text(pause_tpl, i + 1,
                                    sec.resume_hint or sections[i].title))
    parts += ["\nM73 P100 R0\n", end, "\n; EXECUTABLE_BLOCK_END\n"]
    return "".join(parts)


# ---------------------------------------------------------------- sheets

def zbench_sections(hops: int, soft: bool) -> list[Section]:
    z_clear = z_travel_clearance_for_profile(select_profile(soft_holder=soft))
    zf = z_travel_feed_mm_min()
    out: list[Section] = []
    for name, hop, mode in ZBENCH_BLOCKS:
        cycle = f"G1 Z{z_clear + hop:.3f} F{zf}\nG1 Z{z_clear:.3f} F{zf}\n"
        body = (f"G1 Z{z_clear:.3f} F{zf}\n" + BEEP_START + cycle * hops + BEEP_END)
        sim = estimate_lines(body.splitlines(), start=(0.0, 0.0, z_clear)).total
        title = f"ZBENCH {name}: {hops} hops × {hop:g} mm, {mode}; model {sim:.1f} s"
        out.append(Section(title, f"; {title}\n" + body, resume_hint=""))
    for i, (sec, nxt) in enumerate(zip(out, out[1:])):
        sec.resume_hint = nxt.title
    return out


def hop_ladder_sections() -> list[Section]:
    w = PenWriter(int(plotter.DRAW_SPEED_MM_S * 60))
    w.lines.append("; HOP LADDER: block k has k+1 tally marks; hops " +
                   ", ".join(f"{k + 1}→{h:g} mm" for k, h in enumerate(HOP_LADDER_MM)))
    for k, hop in enumerate(HOP_LADDER_MM):
        x0 = 40.0 + 25.0 * k
        w.lines.append(f"; --- block {k + 1}: hop {hop:g} mm ---")
        for dx in (8.0, 10.0, 12.0):
            w.stroke([(x0 + dx, 80.0), (x0 + dx, 150.0)], SAFE_HOP_MM)
        for j in range(10):
            y = 85.0 + 6.0 * j
            w.stroke([(x0 + 2.0, y), (x0 + 4.0, y)], hop)
            w.stroke([(x0 + 16.0, y), (x0 + 18.0, y)], hop)
        w.lift(SAFE_HOP_MM)
        for i in range(k + 1):
            w.stroke([(x0 + 2.0 + 2.0 * i, 160.0), (x0 + 2.0 + 2.0 * i, 164.0)], SAFE_HOP_MM)
    return [Section("HOP LADDER (sacrificial sheet where the notebook goes)", w.text())]


def first_row_sample(source: Path, holder: str) -> list[np.ndarray]:
    """First text row of a page, parsed with the page's own pen levels (any Z-hop)."""
    zd, zu = require_pen_down(source, plotter.Z_PEN_DOWN, holder)
    strokes = [s for s in parse_stroke_polylines(source, zd, zu) if len(s) >= 2]
    if not strokes:
        raise ValueError(f"{source}: no pen strokes to sample")
    _, row_id, *_ = plotter.reading_row_metadata_from_lines(
        strokes, invert_y=False, row_gap_mm=plotter.READING_ROW_GAP_BREAK_MM,
        force_axis="x", coords_are_mm=True)
    row = [s for s, r in zip(strokes, row_id) if r == 0]
    y_left = max(float(s.imag.max()) for s in row)
    return [s for s in row if float(s.imag.mean()) >= y_left - SPEED_ROW_LENGTH_MM]


def speed_sections(source: Path, holder: str) -> list[Section]:
    sample = first_row_sample(source, holder)
    x_mid = float(np.mean([s.real.mean() for s in sample]))
    y_end = min(float(s.imag.min()) for s in sample)
    zd, hop = plotter.Z_PEN_DOWN, plotter.Z_HOP
    lines = ["; SPEED GRID: copy k (k+1 tally marks) = " +
             ", ".join(f"{k + 1}→{v} mm/s @ {a} mm/s²" for k, (v, a) in enumerate(SPEED_GRID))]
    for k, (v, a) in enumerate(SPEED_GRID):
        dx = -SPEED_ROW_PITCH_MM * k
        lines += [f"; --- copy {k + 1}: {v} mm/s, accel {a} ---", f"M204 S{a}"]
        for s in sample:
            pts = [(float(p.real) + dx, float(p.imag)) for p in s]
            lines += [f"G0 X{pts[0][0]:.3f} Y{pts[0][1]:.3f} F{travel_feed_mm_min()}",
                      f"G1 Z{zd:.3f} F{z_travel_feed_mm_min()}"]
            lines += [f"G1 X{x:.3f} Y{y:.3f} F{int(v * 60)}" for x, y in pts[1:]]
            lines.append(f"G1 Z{zd + hop:.3f} F{z_travel_feed_mm_min()}")
        for i in range(k + 1):
            y = y_end - 4.0 - 1.5 * i
            lines += [f"G0 X{x_mid + dx + 1.5:.3f} Y{y:.3f} F{travel_feed_mm_min()}",
                      f"G1 Z{zd:.3f} F{z_travel_feed_mm_min()}",
                      f"G1 X{x_mid + dx - 1.5:.3f} Y{y:.3f} F6000",
                      f"G1 Z{zd + hop:.3f} F{z_travel_feed_mm_min()}"]
    lines.append(f"M204 S{draw_accel_mm_s2():g}")
    check_pen_bounds(lines)
    return [Section(f"SPEED GRID from {source.name}", "\n".join(lines) + "\n")]


def check_pen_bounds(lines: list[str]) -> None:
    lo_x, hi_x = plotter.PAPER_LEFT + PAPER_MARGIN_MM, plotter.PAPER_LEFT + plotter.PAPER_W - PAPER_MARGIN_MM
    lo_y = plotter.PAPER_FRONT + PAPER_MARGIN_MM
    hi_y = plotter.PAPER_FRONT + plotter.PAPER_H - PAPER_MARGIN_MM
    for ln in lines:
        if not ln.startswith(("G0 X", "G1 X")):
            continue
        words = {w[0]: float(w[1:]) for w in ln.split()[1:3]}
        px, py = words["X"] + plotter.PEN_OFFSET_X, words["Y"] + plotter.PEN_OFFSET_Y
        if not (lo_x <= px <= hi_x and lo_y <= py <= hi_y):
            raise ValueError(f"pen point ({px:.1f}, {py:.1f}) outside paper minus margin: {ln}")


def edge_check_sections(gcode_dir: Path, holder: str) -> list[Section]:
    """Trace the union bounding box of all pages' pen moves at pen-down Z, slowly."""
    xs: list[float] = []
    ys: list[float] = []
    pages = sorted(gcode_dir.glob("page_*.gcode"))
    if not pages:
        raise ValueError(f"no page_*.gcode in {gcode_dir}")
    for page in pages:
        require_pen_down(page, plotter.Z_PEN_DOWN, holder)
        for ln in page.open():
            if ln.startswith(("G0 X", "G1 X")):
                w = {t[0]: float(t[1:]) for t in ln.split(";")[0].split()[1:3]}
                xs.append(w["X"])
                ys.append(w["Y"])
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    zd, hop = plotter.Z_PEN_DOWN, plotter.Z_HOP
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    lines = [f"; EDGE CHECK: text bounding box of {len(pages)} pages, nozzle X {x0:.1f}..{x1:.1f} "
             f"Y {y0:.1f}..{y1:.1f}; pen margins to paper edge: "
             f"left {x0 + plotter.PEN_OFFSET_X - plotter.PAPER_LEFT:.1f}, "
             f"right {plotter.PAPER_LEFT + plotter.PAPER_W - x1 - plotter.PEN_OFFSET_X:.1f}, "
             f"front {y0 + plotter.PEN_OFFSET_Y - plotter.PAPER_FRONT:.1f}, "
             f"back {plotter.PAPER_FRONT + plotter.PAPER_H - y1 - plotter.PEN_OFFSET_Y:.1f} mm",
             f"G0 X{x0:.3f} Y{y0:.3f} F{travel_feed_mm_min()}",
             f"G1 Z{zd:.3f} F{z_travel_feed_mm_min()}"]
    lines += [f"G1 X{x:.3f} Y{y:.3f} F{EDGE_CHECK_FEED}" for x, y in corners[1:]]
    lines.append(f"G1 Z{zd + hop:.3f} F{z_travel_feed_mm_min()}")
    return [Section("EDGE CHECK (notebook + L-stop in place, holder WITHOUT pen, watch the stop)",
                    "\n".join(lines) + "\n")]


def page_sections(gcode: Path, holder: str) -> list[Section]:
    require_pen_down(gcode, plotter.Z_PEN_DOWN, holder)
    t = estimate_file(gcode, draw_accel_mm_s2())
    note = (f"{t.total / 60:.1f} min (model: Z {t.z / 60:.1f}, draw {t.draw / 60:.1f}, "
            f"travel {t.travel / 60:.1f}; {t.pen_downs} pen-downs)")
    return [Section(f"PAGE {gcode.stem}", f"; est page time: {note}\n" + gcode.read_text())]


# ---------------------------------------------------------------- CLI

def build_sections(args: argparse.Namespace, soft: bool) -> list[Section]:
    holder = "soft-holder" if soft else "umts"
    if args.cmd == "zbench":
        return zbench_sections(args.hops, soft)
    if args.cmd == "hop-ladder":
        return hop_ladder_sections()
    if args.cmd == "speed":
        return speed_sections(args.source, holder)
    if args.cmd == "edge-check":
        return edge_check_sections(args.gcode_dir, holder)
    return page_sections(args.gcode, holder)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("zbench", "hop-ladder", "speed", "page", "edge-check"):
        sp = sub.add_parser(name)
        sp.add_argument("--holder", choices=("soft", "umts"), required=True)
        sp.add_argument("--out", type=Path, default=None)
        if name == "zbench":
            sp.add_argument("--hops", type=int, default=100)
        if name == "speed":
            sp.add_argument("--source", type=Path, required=True, help="page G-code of this pipeline")
        if name == "page":
            sp.add_argument("--gcode", type=Path, required=True)
        if name == "edge-check":
            sp.add_argument("--gcode-dir", type=Path, required=True, help="build/<pdf>/gcode")
    args = p.parse_args()

    soft = args.holder == "soft"
    try:
        plotter.apply_holder_profile(soft_holder=soft)
        sections = build_sections(args, soft)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    default_name = f"test_{args.cmd.replace('-', '_')}"
    if args.cmd == "page":
        default_name = f"test_{args.gcode.stem}"
    out = args.out or ROOT / "output" / f"{default_name}.gcode"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(job_text(sections, soft))
    print(f"✅ {out}  holder={args.holder} Z_down={plotter.Z_PEN_DOWN} hop={plotter.Z_HOP} "
          f"draw={plotter.DRAW_SPEED_MM_S:g} mm/s accel={draw_accel_mm_s2():g}")
    for s in sections:
        print(f"   {s.title}")


if __name__ == "__main__":
    main()
