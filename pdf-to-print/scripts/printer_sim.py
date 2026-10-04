#!/usr/bin/env python3
"""Dry-run a whole plotter job G-code against a model of the P1S, the pen holder and the paper.

Why: test runs on the real printer are scarce. Every line of the final job is executed here
the way the firmware would (absolute/relative moves, homing, pauses) while tracking where the
pen tip, the holder body and the ink are:

  DRAG            XY travel (G0) while the pen touches the paper — a stray ink line.
  INK_OFF_PAPER   ink outside the paper rectangle (bed, L-stop, notebook edge).
  PEN_CRUSH       nozzle so low that the pen spring would bottom out.
  HOME_WITH_PEN   G28 after the holder was installed (nozzle homing would ram the pen).
  DRAW_BEFORE_INSTALL  pen-down XY motion before the install pause.
  PAUSE_LOW_Z     M400 U1 reached with the bed not lowered to clearance.
  PAUSE_COUNT     pauses ≠ pages + 1 (install + flips + final removal).
  STOP_COLLISION  holder body or pen tip intersects a configured stop (--stop / --l-stop-height).
  OUT_OF_ENVELOPE nozzle leaves the bounding box of a reference job already run on hardware.
  OUT_OF_MACHINE  nozzle outside the P1S travel range.
  UNKNOWN_COMMAND (warning) a command the model does not know.

Pen model (bed frame, mm): the pen tip touches the paper when nozzle Z < Z_PEN_DOWN + compression
(compression = spring compression at pen-down, an assumption you can measure). The holder body
is rigid to the toolhead; its lowest point is pen_protrusion above the tip when the tip is free.

Page fidelity against the source PDF raster is in printer_fidelity.py (--png-dir).

Usage:
  python3 scripts/printer_sim.py output/9_experimental_fast.gcode --holder soft \
      --reference output/9_experimental.gcode --png-dir build/9/png --report out.md
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svg_to_gcode as plotter  # noqa: E402
from gcode_stroke_parse import EXPERIMENTAL_STROKES_MARKER  # noqa: E402
from holder_config import select_profile, z_travel_clearance_for_profile  # noqa: E402
from plot_time_sim import estimate_lines  # noqa: E402

MACHINE_XY = (0.0, 265.0)       # P1S nozzle travel with soft endstops off (legacy jobs reach 262)
MACHINE_Z = (0.0, 250.0)
ENVELOPE_TOL_MM = 0.5
STOP_SAMPLE_MM = 1.0
PAGE_MARKER = re.compile(r";=+ PAGE (\d+) =+")
KNOWN_CODES = {
    "G4", "G17", "G21", "G90", "G91", "G92", "M17", "M82", "M83", "M84", "M73", "M104", "M106",
    "M107", "M109", "M140", "M141", "M190", "M201", "M203", "M204", "M205", "M220", "M221",
    "M400", "M960", "M975", "M1002", "M1006",
}
_WORD = re.compile(r"([A-Z])\s*(-?\d+(?:\.\d*)?|-?\.\d+)")


@dataclass(frozen=True)
class StopBox:
    """Obstacle on the bed (pen/bed frame, mm); height above the bed surface."""
    x0: float
    y0: float
    x1: float
    y1: float
    height_mm: float

    def distance(self, x: float, y: float) -> float:
        dx = max(self.x0 - x, 0.0, x - self.x1)
        dy = max(self.y0 - y, 0.0, y - self.y1)
        return math.hypot(dx, dy)


@dataclass(frozen=True)
class SimConfig:
    holder: str                              # "soft" | "umts"
    paper_thickness_mm: float = 3.0          # stack under the active page (notebook folded back)
    # spring compression at Z_PEN_DOWN: the pen still touches up to Z_PEN_DOWN + this. Soft holder
    # hop-ladder 2026-10-04: streaks at a 1.5 mm hop, clean at 2.0 mm.
    pen_down_compression_mm: float = 2.0
    spring_travel_mm: float = 4.0            # assumption: max compression before bottoming out
    pen_protrusion_mm: float = 10.0          # assumption: free pen tip below holder body
    holder_radius_mm: float = 12.0           # assumption: holder footprint around the pen axis
    min_clearance_mm: float = 0.3            # warn below this pen-tip clearance on travel
    stops: tuple[StopBox, ...] = ()


@dataclass
class Issue:
    code: str
    message: str
    line_no: int


@dataclass
class PageStats:
    number: int
    pen_downs: int = 0
    ink_mm: float = 0.0
    decoration_mm: float = 0.0
    time_s: float = 0.0
    bbox: list[float] = field(default_factory=lambda: [math.inf, math.inf, -math.inf, -math.inf])
    strokes: list[np.ndarray] = field(default_factory=list)      # pen frame, text ink
    decorations: list[np.ndarray] = field(default_factory=list)  # strikethrough etc.


@dataclass
class SimReport:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    pages: list[PageStats] = field(default_factory=list)
    pauses: int = 0
    min_travel_clearance_mm: float = math.inf
    envelope: list[float] = field(default_factory=lambda: [math.inf, math.inf, math.inf,
                                                           -math.inf, -math.inf, -math.inf])
    total_time_s: float = 0.0


class _Machine:
    """Firmware-level state + pen model; one instance per simulated job."""

    def __init__(self, cfg: SimConfig, reference: list[float] | None, report: SimReport) -> None:
        profile = select_profile(soft_holder=cfg.holder == "soft")
        plotter.apply_holder_profile(soft_holder=cfg.holder == "soft")
        self.cfg, self.ref, self.r = cfg, reference, report
        self.offset = (profile.pen_offset_x, profile.pen_offset_y)
        self.z_contact = profile.z_pen_down + cfg.pen_down_compression_mm
        self.z_clear = z_travel_clearance_for_profile(profile)
        self.paper = (plotter.PAPER_LEFT, plotter.PAPER_FRONT,
                      plotter.PAPER_LEFT + plotter.PAPER_W, plotter.PAPER_FRONT + plotter.PAPER_H)
        self.pos: list[float | None] = [None, None, None]
        self.relative = False
        self.mounted = False
        self.in_contact = False
        self.decoration = False
        self.page: PageStats | None = None
        self.stroke: list[tuple[float, float]] = []
        self.reported: set[str] = set()

    # ------------------------------------------------------------- helpers
    def error(self, code: str, msg: str, n: int) -> None:
        self.r.errors.append(Issue(code, msg, n))

    def once(self, code: str, msg: str, n: int) -> None:
        if code not in self.reported:
            self.reported.add(code)
            self.error(code, msg, n)

    def pen_xy(self, x: float, y: float) -> tuple[float, float]:
        return x + self.offset[0], y + self.offset[1]

    def on_paper(self, px: float, py: float) -> bool:
        x0, y0, x1, y1 = self.paper
        return x0 - 1e-6 <= px <= x1 + 1e-6 and y0 - 1e-6 <= py <= y1 + 1e-6

    def surface(self, px: float, py: float) -> float:
        return self.cfg.paper_thickness_mm if self.on_paper(px, py) else 0.0

    def tip_height(self, z: float, px: float, py: float) -> float:
        """Free pen tip height above whatever is under it (paper or bed)."""
        return z - self.z_contact + self.cfg.paper_thickness_mm - self.surface(px, py)

    # ------------------------------------------------------------- events
    def page_marker(self, number: int) -> None:
        if self.page is None or self.page.number != number:
            self.close_stroke()
            self.page = PageStats(number)
            self.r.pages.append(self.page)
            self.decoration = False

    def home(self, n: int) -> None:
        if self.mounted:
            self.error("HOME_WITH_PEN", "G28 with the pen holder installed", n)
        self.pos = [None, None, 0.0]

    def pause(self, n: int) -> None:
        self.close_stroke()
        self.r.pauses += 1
        z = self.pos[2]
        if z is None or z < self.z_clear - 0.05:
            self.error("PAUSE_LOW_Z", f"M400 U1 at Z={z} < clearance {self.z_clear}", n)
        self.mounted = True

    def close_stroke(self) -> None:
        if self.page is not None and len(self.stroke) >= 2:
            target = self.page.decorations if self.decoration else self.page.strokes
            target.append(np.asarray(self.stroke))
        self.stroke = []
        self.in_contact = False

    # ------------------------------------------------------------- motion
    def move(self, cmd: str, words: dict[str, float], n: int) -> None:
        start = list(self.pos)
        target = list(self.pos)
        for i, axis in enumerate("XYZ"):
            if axis in words:
                base = start[i] if self.relative else 0.0
                if self.relative and base is None:
                    self.error("UNKNOWN_POSITION", f"relative {axis} move from unknown position", n)
                    return
                target[i] = (base or 0.0) + words[axis]
        self.pos = target
        if None in start or None in target:
            return
        self.track_envelope(target, n)
        self.pen_motion(cmd, start, target, n)

    def track_envelope(self, p: list[float], n: int) -> None:
        env = self.r.envelope
        for i in range(3):
            env[i], env[i + 3] = min(env[i], p[i]), max(env[i + 3], p[i])
        lo_xy, hi_xy = MACHINE_XY
        if not (lo_xy <= p[0] <= hi_xy and lo_xy <= p[1] <= hi_xy and MACHINE_Z[0] <= p[2] <= MACHINE_Z[1]):
            self.once("OUT_OF_MACHINE", f"nozzle {p} outside P1S travel", n)
        if self.ref is not None:
            lo, hi = self.ref[:3], self.ref[3:]
            if any(p[i] < lo[i] - ENVELOPE_TOL_MM or p[i] > hi[i] + ENVELOPE_TOL_MM for i in range(3)):
                self.once("OUT_OF_ENVELOPE", f"nozzle {[round(v, 2) for v in p]} outside reference "
                          f"envelope {[round(v, 2) for v in self.ref]}", n)

    def pen_motion(self, cmd: str, a: list[float], b: list[float], n: int) -> None:
        if not self.mounted:
            if min(a[2], b[2]) < self.z_contact and (a[0], a[1]) != (b[0], b[1]):
                self.once("DRAW_BEFORE_INSTALL", "pen-down XY motion before the install pause", n)
            return
        pa, pb = self.pen_xy(a[0], a[1]), self.pen_xy(b[0], b[1])
        z_low = min(a[2], b[2])
        # tip_height < 0 means the spring is compressed by that much
        if min(self.tip_height(z_low, *pa), self.tip_height(z_low, *pb)) < \
                -self.cfg.spring_travel_mm - 1e-6:
            self.error("PEN_CRUSH", f"Z={z_low:.3f} compresses the pen beyond spring travel", n)
        self.check_stops(pa, pb, z_low, n)
        xy_len = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        contact = z_low < self.z_contact - 1e-6
        if xy_len <= 1e-9:
            if not contact:
                self.close_stroke()
            return
        if cmd == "G0":
            self.r.min_travel_clearance_mm = min(self.r.min_travel_clearance_mm, z_low - self.z_contact)
            if 0 <= z_low - self.z_contact < self.cfg.min_clearance_mm:
                self.r.warnings.append(Issue("LOW_CLEARANCE", f"travel clearance {z_low - self.z_contact:.2f} mm", n))
        if not contact:
            self.close_stroke()
            return
        if cmd == "G0":
            self.error("DRAG", f"travel with pen on paper ({xy_len:.2f} mm)", n)
        if not (self.on_paper(*pa) and self.on_paper(*pb)):
            self.error("INK_OFF_PAPER", f"ink at pen {pa}->{pb} outside paper {self.paper}", n)
        self.add_ink(pa, pb, xy_len)

    def add_ink(self, pa: tuple[float, float], pb: tuple[float, float], length: float) -> None:
        page = self.page
        if page is None:
            page = self.page = PageStats(0)
            self.r.pages.append(page)
        if not self.in_contact:
            page.pen_downs += 1
            self.in_contact = True
            self.stroke = [pa]
        self.stroke.append(pb)
        if self.decoration:
            page.decoration_mm += length
        else:
            page.ink_mm += length
        bb = page.bbox
        for x, y in (pa, pb):
            bb[0], bb[1], bb[2], bb[3] = min(bb[0], x), min(bb[1], y), max(bb[2], x), max(bb[3], y)

    def check_stops(self, pa, pb, z: float, n: int) -> None:
        cfg = self.cfg
        for stop in cfg.stops:
            reach = cfg.holder_radius_mm
            if min(pa[0], pb[0]) > stop.x1 + reach or max(pa[0], pb[0]) < stop.x0 - reach or \
                    min(pa[1], pb[1]) > stop.y1 + reach or max(pa[1], pb[1]) < stop.y0 - reach:
                continue
            steps = max(1, int(math.hypot(pb[0] - pa[0], pb[1] - pa[1]) / STOP_SAMPLE_MM))
            for t in np.linspace(0.0, 1.0, steps + 1):
                x, y = pa[0] + (pb[0] - pa[0]) * t, pa[1] + (pb[1] - pa[1]) * t
                tip = z - self.z_contact + cfg.paper_thickness_mm   # tip height above the bed
                body = tip + cfg.pen_protrusion_mm
                d = stop.distance(x, y)
                if (d < reach and body < stop.height_mm) or (d == 0.0 and tip < stop.height_mm):
                    self.once("STOP_COLLISION", f"holder at pen ({x:.1f}, {y:.1f}), body bottom "
                              f"{body:.1f} mm vs stop {stop}", n)
                    return


def _split(raw: str) -> tuple[str, dict[str, float], str]:
    code_part, _, comment = raw.partition(";")
    code_part = code_part.strip().upper()
    if not code_part:
        return "", {}, comment
    head = code_part.split()[0]
    return head, {k: float(v) for k, v in _WORD.findall(code_part[len(head):])}, comment


def simulate_job(lines: Iterable[str], cfg: SimConfig,
                 reference_envelope: list[float] | None = None) -> SimReport:
    report = SimReport()
    m = _Machine(cfg, reference_envelope, report)
    page_lines: dict[int, list[str]] = {}
    accel_line = "M204 S20000"
    for n, raw in enumerate(lines, start=1):
        marker = PAGE_MARKER.search(raw)
        if marker:
            m.page_marker(int(marker.group(1)))
        if EXPERIMENTAL_STROKES_MARKER in raw:
            m.close_stroke()
            m.decoration = True
        head, words, _ = _split(raw)
        if m.page is not None and head:
            page_lines.setdefault(id(m.page), []).append(raw)
        if not head:
            continue
        if head in ("G0", "G00", "G1", "G01"):
            m.move("G0" if head in ("G0", "G00") else "G1", words, n)
        elif head == "G28":
            m.home(n)
        elif head == "G90":
            m.relative = False
        elif head == "G91":
            m.relative = True
        elif head == "M400" and words.get("U") == 1:
            m.pause(n)
        elif head == "M204":
            accel_line = raw.split(";")[0]
        elif head in ("G2", "G3", "G02", "G03"):
            report.errors.append(Issue("UNSUPPORTED_MOTION", f"arc {head} not modelled", n))
        elif head not in KNOWN_CODES:
            report.warnings.append(Issue("UNKNOWN_COMMAND", raw.strip(), n))
    m.close_stroke()
    text_pages = [p for p in report.pages if p.number > 0]
    if report.pauses != len(text_pages) + 1:
        report.errors.append(Issue("PAUSE_COUNT", f"{report.pauses} pauses for {len(text_pages)} "
                                   f"pages (expected pages + 1)", 0))
    for page in report.pages:
        page.time_s = estimate_lines([accel_line] + page_lines.get(id(page), []),
                                     start=(0.0, 0.0, m.z_clear)).total
    report.total_time_s = sum(p.time_s for p in report.pages)
    return report


def envelope_of(lines: Iterable[str]) -> list[float]:
    """Nozzle bounding box (xmin, ymin, zmin, xmax, ymax, zmax) of a job, e.g. one run on hardware."""
    probe = simulate_job(lines, SimConfig(holder="soft"))
    return list(probe.envelope)


# ---------------------------------------------------------------- CLI / report

def parse_stop(text: str) -> StopBox:
    vals = [float(v) for v in text.split(",")]
    if len(vals) != 5:
        raise argparse.ArgumentTypeError("--stop needs x0,y0,x1,y1,height (pen/bed frame, mm)")
    return StopBox(*vals)


def l_stop(height: float) -> tuple[StopBox, ...]:
    """The printable jig of l_stop_model.py: arms from the plate edges up to the paper edges."""
    from l_stop_model import JigParams

    jig = JigParams()
    return (StopBox(0.0, 0.0, jig.front_length, plotter.PAPER_FRONT, height),
            StopBox(0.0, 0.0, plotter.PAPER_LEFT, jig.left_length, height))


def format_report(report: SimReport, cfg: SimConfig, job: Path) -> str:
    env = report.envelope
    status = "FAIL" if report.errors else "PASS"
    out = [f"# Printer simulation: {job.name} — {status}", "",
           f"holder={cfg.holder}, paper stack {cfg.paper_thickness_mm} mm; assumptions: pen-down "
           f"compression {cfg.pen_down_compression_mm} mm, spring travel {cfg.spring_travel_mm} mm, "
           f"pen protrusion {cfg.pen_protrusion_mm} mm, holder radius {cfg.holder_radius_mm} mm; "
           f"stops: {len(cfg.stops)}", "",
           f"- pages {len([p for p in report.pages if p.number])}, pauses {report.pauses}",
           f"- model time {report.total_time_s / 3600:.2f} h "
           f"({report.total_time_s / 60 / max(len(report.pages), 1):.1f} min/page)",
           f"- min pen-tip clearance on travel {report.min_travel_clearance_mm:.2f} mm",
           f"- nozzle envelope X {env[0]:.1f}..{env[3]:.1f}, Y {env[1]:.1f}..{env[4]:.1f}, "
           f"Z {env[2]:.1f}..{env[5]:.1f}", "",
           f"## Errors ({len(report.errors)})"]
    out += [f"- `{e.code}` line {e.line_no}: {e.message}" for e in report.errors[:50]] or ["- none"]
    warn_codes: dict[str, int] = {}
    for w in report.warnings:
        warn_codes[w.code] = warn_codes.get(w.code, 0) + 1
    out += ["", f"## Warnings ({len(report.warnings)})"]
    out += [f"- `{c}` × {k}" for c, k in warn_codes.items()] or ["- none"]
    out += ["", "## Pages", "", "| page | min | pen-downs | ink m | decoration m | pen bbox X | pen bbox Y |",
            "|---|---|---|---|---|---|---|"]
    for p in report.pages:
        b = p.bbox
        out.append(f"| {p.number:02d} | {p.time_s / 60:.1f} | {p.pen_downs} | {p.ink_mm / 1000:.2f} | "
                   f"{p.decoration_mm / 1000:.2f} | {b[0]:.1f}..{b[2]:.1f} | {b[1]:.1f}..{b[3]:.1f} |")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("job", type=Path)
    p.add_argument("--holder", choices=("soft", "umts"), required=True)
    p.add_argument("--reference", type=Path, help="job already run on hardware (envelope check)")
    p.add_argument("--paper-thickness", type=float, default=3.0)
    p.add_argument("--pen-down-compression", type=float, default=1.0)
    p.add_argument("--spring-travel", type=float, default=4.0)
    p.add_argument("--pen-protrusion", type=float, default=10.0)
    p.add_argument("--holder-radius", type=float, default=12.0)
    p.add_argument("--stop", type=parse_stop, action="append", default=[])
    p.add_argument("--l-stop-height", type=float, default=None,
                   help="model an L-stop of this height along the paper's left and front edges")
    p.add_argument("--png-dir", type=Path, help="source page rasters page_NN.png → fidelity check")
    p.add_argument("--render-dir", type=Path, help="write per-page ink-vs-source PNGs here")
    p.add_argument("--report", type=Path)
    a = p.parse_args()

    stops = tuple(a.stop) + (l_stop(a.l_stop_height) if a.l_stop_height is not None else ())
    cfg = SimConfig(a.holder, a.paper_thickness, a.pen_down_compression, a.spring_travel,
                    a.pen_protrusion, a.holder_radius, stops=stops)
    reference = envelope_of(a.reference.read_text().splitlines()) if a.reference else None
    report = simulate_job(a.job.read_text().splitlines(), cfg, reference)
    text = format_report(report, cfg, a.job)
    if a.png_dir:
        from printer_fidelity import fidelity_section
        fid_text, fid_errors = fidelity_section(report, a.holder, a.png_dir, a.render_dir)
        report.errors.extend(fid_errors)
        text = text.replace(" — PASS", " — FAIL") if fid_errors else text
        text += fid_text
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(text)
    print(text)
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
