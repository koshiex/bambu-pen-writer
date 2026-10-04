"""Per-page motion preview of a job on the P1S bed (data for the «Предпросмотр движения» tab).

A merged job is cut into pages at the `;===== PAGE NN =====` markers (print order, pause
templates repeat the next marker — consecutive duplicates belong to the same page). Each page is
replayed on the planner model (plot_time_sim.timeline) from the park position where the previous
pause left the head. Coordinates are sent in the pen frame = bed coordinates, so the browser
draws the plate, the paper and the L-stop jig in the same millimetres as the ink.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import svg_to_gcode as plotter  # noqa: E402
from holder_config import (PARK_NOZZLE_X_MM, PARK_NOZZLE_Y_MM, select_profile,  # noqa: E402
                           z_travel_clearance_for_profile)
from l_stop_model import JigParams, jig_outline  # noqa: E402
from page_order import SPREAD_ORDER_24  # noqa: E402
from plot_time_sim import timeline  # noqa: E402
from printer_sim import SimConfig  # noqa: E402

from .printer_link import JobMeta, pause_guidance  # noqa: E402

PAGE_RE = re.compile(r"^;=+ PAGE (\d+) =+")
ACCEL_RE = re.compile(r"^M204\s+S(\d+(?:\.\d+)?)", re.I)
ERROR_RE = re.compile(r"^- `(\w+)` line (\d+): (.*)$")
PEN_COMPRESSION_MM = SimConfig.pen_down_compression_mm   # same contact model as printer_sim
HOLDER_RADIUS_MM = 12.0
BED_MM = (256, 256)
NOZZLE_RANGE = (0.0, 0.0, 265.0, 265.0)


@dataclass(frozen=True)
class Section:
    index: int        # 1-based print order
    page: int         # PDF page number
    start: int        # first line (0-based) of the page
    end: int          # one past the last line


@dataclass
class _Job:
    lines: list[str]
    sections: list[Section]
    accel: float


_cache: dict[tuple[str, float], _Job] = {}
_lock = threading.Lock()


def _load(path: Path) -> _Job:
    key = (str(path), path.stat().st_mtime)
    with _lock:
        if key in _cache:
            return _cache[key]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sections: list[Section] = []
    for i, line in enumerate(lines):
        m = PAGE_RE.match(line)
        if m and (not sections or sections[-1].page != int(m.group(1))):
            if sections:
                prev = sections[-1]
                sections[-1] = Section(prev.index, prev.page, prev.start, i)
            sections.append(Section(len(sections) + 1, int(m.group(1)), i, len(lines)))
    accel = next((float(m.group(1)) for m in map(ACCEL_RE.match, lines) if m), 5000.0)
    job = _Job(lines, sections, accel)
    with _lock:
        _cache.clear()                       # one job at a time keeps memory bounded
        _cache[key] = job
    return job


def geometry(holder: str) -> dict:
    profile = select_profile(soft_holder=holder == "soft")
    x0, y0 = plotter.PAPER_LEFT, plotter.PAPER_FRONT          # fixed for every holder
    return {
        "bed": list(BED_MM), "paper": [x0, y0, x0 + plotter.PAPER_W, y0 + plotter.PAPER_H],
        "jig": [[round(x, 2), round(y, 2)] for x, y in jig_outline(JigParams()).exterior.coords],
        "offset": [profile.pen_offset_x, profile.pen_offset_y],
        "holder_radius": HOLDER_RADIUS_MM, "z_pen": profile.z_pen_down,
        "z_contact": profile.z_pen_down + PEN_COMPRESSION_MM,
        "z_clear": z_travel_clearance_for_profile(profile), "nozzle_range": list(NOZZLE_RANGE),
        "park": [PARK_NOZZLE_X_MM + profile.pen_offset_x, PARK_NOZZLE_Y_MM + profile.pen_offset_y],
    }


def _meta(job: _Job, name: str) -> JobMeta:
    pages = tuple(s.page for s in job.sections)
    order = "spread" if pages == tuple(SPREAD_ORDER_24) else "sequential"
    return JobMeta(name, Path(name).stem + ".3mf", pages, order, 0.0)


def _section_moves(job: _Job, sec: Section, holder: str):
    profile = select_profile(soft_holder=holder == "soft")
    start = (PARK_NOZZLE_X_MM, PARK_NOZZLE_Y_MM, z_travel_clearance_for_profile(profile))
    head = [f"M204 S{job.accel:g}"]
    return timeline(head + job.lines[sec.start:sec.end], start), len(head)


def job_overview(path: Path, holder: str) -> dict:
    job = _load(path)
    pages = []
    for sec in job.sections:
        moves, _ = _section_moves(job, sec, holder)
        pages.append({"index": sec.index, "page": sec.page,
                      "seconds": round(moves[-1].t1 if moves else 0.0, 1)})
    return {"file": path.name, "pages": pages, "geometry": geometry(holder)}


def _report_issues(path: Path) -> list[tuple[str, int, str]]:
    report = path.with_name(path.stem + ".sim.md")
    if not report.is_file():
        return []
    out = []
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ERROR_RE.match(line)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3)))
    return out


def page_motion(path: Path, index: int, holder: str) -> dict:
    job = _load(path)
    if not 1 <= index <= len(job.sections):
        raise ValueError(f"страница {index} вне 1…{len(job.sections)}")
    sec = job.sections[index - 1]
    geo = geometry(holder)
    ox, oy = geo["offset"]
    z_contact = geo["z_contact"]
    moves, head = _section_moves(job, sec, holder)
    x, y, z, t, kinds = _arrays(moves, ox, oy, z_contact)
    stats = _stats(moves, z_contact, ox, oy)
    by_line = {m.line_no - head + sec.start: m for m in moves}      # 0-based file line → move
    pauses = []
    for i in range(sec.start, sec.end):
        if job.lines[i].startswith("M400 U1"):
            before = [m for ln, m in by_line.items() if ln < i]
            at = max((m.t1 for m in before), default=0.0)
            state = {"gcode_state": "PAUSE", "stg_cur": 5, "layer_num": sec.index}
            pauses.append({"t": round(at, 3), "label": pause_guidance(state, _meta(job, path.name), 0)})
    issues = []
    for code, line_no, msg in _report_issues(path):
        if sec.start <= line_no - 1 < sec.end:
            m = by_line.get(line_no - 1)
            issues.append({"code": code, "message": msg, "t": round(m.t1, 3) if m else None,
                           "x": round(m.end[0] + ox, 2) if m else None,
                           "y": round(m.end[1] + oy, 2) if m else None})
    return {"file": path.name, "index": sec.index, "page": sec.page, "count": len(job.sections),
            "x": x, "y": y, "z": z, "t": t, "k": kinds, "pauses": pauses, "issues": issues,
            "stats": stats, "geometry": geo}


def _arrays(moves, ox: float, oy: float, z_contact: float):
    first = moves[0].start if moves else (0.0, 0.0, 0.0)
    x, y, z, t = [round(first[0] + ox, 2)], [round(first[1] + oy, 2)], [round(first[2], 2)], [0.0]
    kinds = []
    for m in moves:
        x.append(round(m.end[0] + ox, 2))
        y.append(round(m.end[1] + oy, 2))
        z.append(round(m.end[2], 2))
        t.append(round(m.t1, 3))
        if m.kind == "z":
            kinds.append("z")
        else:
            kinds.append("d" if min(m.start[2], m.end[2]) < z_contact else "t")
    return x, y, z, t, "".join(kinds)


def _stats(moves, z_contact: float, ox: float, oy: float) -> dict:
    ink = travel = 0.0
    pen_downs = 0
    down = False
    for m in moves:
        xy = ((m.end[0] - m.start[0]) ** 2 + (m.end[1] - m.start[1]) ** 2) ** 0.5
        contact = min(m.start[2], m.end[2]) < z_contact
        if m.kind != "z" and contact:
            ink += xy
            if not down:
                pen_downs += 1
            down = True
        elif m.kind != "z":
            travel += xy
            down = False
        elif not contact:
            down = False
    seconds = moves[-1].t1 if moves else 0.0
    return {"seconds": round(seconds, 1), "pen_downs": pen_downs, "ink_mm": round(ink, 1),
            "travel_mm": round(travel, 1), "moves": len(moves)}
