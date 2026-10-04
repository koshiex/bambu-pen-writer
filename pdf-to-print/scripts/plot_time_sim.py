#!/usr/bin/env python3
"""Estimate P1S run time of pen-plot G-code with a classic-jerk look-ahead planner model.

Why: on this plotter the page time is dominated by pen lifts (Z moves the whole bed: 20 mm/s,
500 mm/s², jerk 3) and by direction changes of short segments (XY jerk 9 mm/s), not by the
nominal draw feed. The model reproduces that, so M73 progress and tuning decisions use real
numbers instead of a flat minutes-per-page guess.

Model (same family as Bambu Studio / PrusaSlicer time estimator):
  * per-axis max speed / acceleration from the P1S machine limits, M204 S|P|T sets the XY cap;
  * junction speed limited so that every axis velocity jump ≤ its jerk;
  * forward/backward passes, trapezoidal profile per move.
It does not model firmware overhead, so real runs are usually somewhat slower; scale with
PDF_TO_PRINT_TIME_FACTOR after timing one real page.

Usage:
  python3 scripts/plot_time_sim.py build/9/gcode/page_*.gcode
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

MAX_V = (500.0, 500.0, 20.0)       # mm/s (M203 X500 Y500 Z20)
MAX_A = (20000.0, 20000.0, 500.0)  # mm/s² (M201 X20000 Y20000 Z500)
JERK = (9.0, 9.0, 3.0)             # mm/s (M205 X9 Y9 Z3)
DEFAULT_ACCEL = 20000.0            # M204 P/T in the Bambu header

_WORD = re.compile(r"([XYZFSPT])\s*(-?\d+(?:\.\d*)?|-?\.\d+)", re.I)


@dataclass
class TimeBreakdown:
    draw: float = 0.0
    travel: float = 0.0
    z: float = 0.0
    draw_mm: float = 0.0
    travel_mm: float = 0.0
    pen_downs: int = 0

    @property
    def total(self) -> float:
        return self.draw + self.travel + self.z

    def __add__(self, o: "TimeBreakdown") -> "TimeBreakdown":
        return TimeBreakdown(self.draw + o.draw, self.travel + o.travel, self.z + o.z,
                             self.draw_mm + o.draw_mm, self.travel_mm + o.travel_mm,
                             self.pen_downs + o.pen_downs)


@dataclass(frozen=True)
class TimedMove:
    kind: str                     # draw | travel | z
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    t0: float
    t1: float
    line_no: int                  # 1-based line of the G-code command


@dataclass
class _Moves:
    start: list[tuple[float, float, float]] = field(default_factory=list)
    end: list[tuple[float, float, float]] = field(default_factory=list)
    line_no: list[int] = field(default_factory=list)
    length: list[float] = field(default_factory=list)
    unit: list[tuple[float, float, float]] = field(default_factory=list)
    v_nom: list[float] = field(default_factory=list)
    accel: list[float] = field(default_factory=list)
    kind: list[str] = field(default_factory=list)


def _limits_for(unit: tuple[float, float, float], feed: float, cap: float) -> tuple[float, float]:
    v, a = feed, cap
    for k in range(3):
        if abs(unit[k]) > 1e-12:
            v = min(v, MAX_V[k] / abs(unit[k]))
            a = min(a, MAX_A[k] / abs(unit[k]))
    return v, a


def _parse(lines: Iterable[str], start: tuple[float, float, float]) -> tuple[_Moves, int]:
    mv = _Moves()
    pos = list(start)
    feed = 100.0
    acc_draw = acc_travel = DEFAULT_ACCEL
    pen_downs = 0
    for line_no, raw in enumerate(lines, start=1):
        line = raw.split(";", 1)[0].strip().upper()
        if line.startswith("M204"):
            w = dict(_WORD.findall(line))
            if "S" in w:
                acc_draw = acc_travel = float(w["S"])
            acc_draw = float(w.get("P", acc_draw))
            acc_travel = float(w.get("T", acc_travel))
            continue
        if not (line.startswith("G0") or line.startswith("G1")) or line.startswith(("G10", "G11")):
            continue
        w = dict(_WORD.findall(line))
        if "F" in w:
            feed = float(w["F"]) / 60.0
        new = [float(w.get(a, pos[i])) for i, a in enumerate("XYZ")]
        d = [n - p for n, p in zip(new, pos)]
        length = math.sqrt(sum(c * c for c in d))
        if length > 1e-9:
            xy = math.hypot(d[0], d[1])
            kind = "z" if xy < 1e-9 else ("travel" if line.startswith("G0") else "draw")
            if kind == "z" and d[2] < 0:
                pen_downs += 1
            unit = (d[0] / length, d[1] / length, d[2] / length)
            v, a = _limits_for(unit, feed, acc_travel if kind == "travel" else acc_draw)
            mv.start.append(tuple(pos))
            mv.end.append(tuple(new))
            mv.line_no.append(line_no)
            mv.length.append(length)
            mv.unit.append(unit)
            mv.v_nom.append(v)
            mv.accel.append(a)
            mv.kind.append(kind)
        pos = new
    return mv, pen_downs


def _standstill_speed(unit: tuple[float, float, float]) -> float:
    """Speed reachable from/to rest without exceeding any axis jerk."""
    return min((JERK[k] / abs(unit[k]) for k in range(3) if abs(unit[k]) > 1e-12), default=0.0)


def _junction_limits(mv: _Moves) -> list[float]:
    n = len(mv.length)
    limit = [0.0] * (n + 1)
    if n:
        limit[n] = min(_standstill_speed(mv.unit[-1]), mv.v_nom[-1])
    for i in range(n):
        u2 = mv.unit[i]
        if i == 0:
            vj = _standstill_speed(u2)
        else:
            u1 = mv.unit[i - 1]
            vj = min(mv.v_nom[i - 1], mv.v_nom[i])
            for k in range(3):
                du = abs(u2[k] - u1[k])
                if du > 1e-12:
                    vj = min(vj, JERK[k] / du)
        limit[i] = min(vj, mv.v_nom[i])
    return limit


def _durations(mv: _Moves) -> list[float]:
    n = len(mv.length)
    v_at = _junction_limits(mv)
    for i in range(n - 1, -1, -1):
        v_at[i] = min(v_at[i], math.sqrt(v_at[i + 1] ** 2 + 2 * mv.accel[i] * mv.length[i]))
    for i in range(n):
        v_at[i + 1] = min(v_at[i + 1], math.sqrt(v_at[i] ** 2 + 2 * mv.accel[i] * mv.length[i]))
    out: list[float] = []
    for i in range(n):
        v0, v1, a, v, s = v_at[i], v_at[i + 1], mv.accel[i], mv.v_nom[i], mv.length[i]
        s_acc = (v * v - v0 * v0) / (2 * a)
        s_dec = (v * v - v1 * v1) / (2 * a)
        if s_acc + s_dec <= s:
            t = (v - v0) / a + (v - v1) / a + (s - s_acc - s_dec) / v
        else:
            vp = math.sqrt(max((2 * a * s + v0 * v0 + v1 * v1) / 2, 0.0))
            t = (vp - v0) / a + (vp - v1) / a
        out.append(t)
    return out


def _simulate(mv: _Moves, pen_downs: int) -> TimeBreakdown:
    out = TimeBreakdown(pen_downs=pen_downs)
    for i, t in enumerate(_durations(mv)):
        s = mv.length[i]
        kind = mv.kind[i]
        setattr(out, kind, getattr(out, kind) + t)
        if kind == "draw":
            out.draw_mm += s
        elif kind == "travel":
            out.travel_mm += s
    return out


def estimate_lines(
    lines: Iterable[str], start: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> TimeBreakdown:
    mv, pen_downs = _parse(lines, start)
    return _simulate(mv, pen_downs)


def timeline(lines: Iterable[str], start: tuple[float, float, float] = (0.0, 0.0, 0.0)
             ) -> list[TimedMove]:
    """Every move with its start/end position and [t0, t1] on the same model as estimate_lines."""
    mv, _ = _parse(lines, start)
    out: list[TimedMove] = []
    t = 0.0
    for i, d in enumerate(_durations(mv)):
        out.append(TimedMove(mv.kind[i], mv.start[i], mv.end[i], t, t + d, mv.line_no[i]))
        t += d
    return out


def _page_start(lines: list[str]) -> tuple[float, float, float]:
    """A page starts after a pause: pen up, above its first stroke (no phantom move from 0,0,0)."""
    from gcode_stroke_parse import _extract_xy, _extract_z

    xy = next((_extract_xy(ln) for ln in lines if ln.upper().startswith("G0")), None)
    zs = [_extract_z(ln) for ln in lines if ln.upper().startswith("G1") and _extract_xy(ln) is None]
    zs = [z for z in zs if z is not None]
    x, y = xy if xy is not None else (0.0, 0.0)
    return x, y, max(zs) if zs else 0.0


def estimate_file(path: Path, accel_mm_s2: float | None = None) -> TimeBreakdown:
    """Page G-code has no M204; pass the accel that merge_pages will emit."""
    lines = Path(path).read_text().splitlines()
    head = [f"M204 S{accel_mm_s2:g}"] if accel_mm_s2 else []
    return estimate_lines(head + lines, start=_page_start(lines))


def format_breakdown(name: str, t: TimeBreakdown) -> str:
    avg = t.draw_mm / t.draw if t.draw > 0 else 0.0
    return (f"{name}: {t.total / 60:5.1f} min | Z {t.z / 60:4.1f} ({t.pen_downs} pen-downs) | "
            f"draw {t.draw / 60:4.1f} ({t.draw_mm / 1000:.1f} m, avg {avg:.0f} mm/s) | "
            f"travel {t.travel / 60:4.1f}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("gcode", nargs="+", type=Path)
    p.add_argument("--accel", type=float, default=None,
                   help="XY acceleration mm/s² for page files without M204 (default: pipeline setting)")
    args = p.parse_args()
    accel = args.accel
    if accel is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from holder_config import draw_accel_mm_s2
        accel = draw_accel_mm_s2()
    total = TimeBreakdown()
    for g in args.gcode:
        t = estimate_file(g, accel)
        total = total + t
        print(format_breakdown(g.name, t))
    if len(args.gcode) > 1:
        print(format_breakdown(f"TOTAL ({len(args.gcode)} files)", total)
              + f" | {total.total / 3600:.2f} h")


if __name__ == "__main__":
    main()
