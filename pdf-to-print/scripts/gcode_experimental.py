#!/usr/bin/env python3
"""Experimental post-process for vpype page G-code (variable Z/F, jitter + strikethrough).

All features default off; enable via env or CLI flags in svg_to_gcode.py.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline

from gcode_stroke_parse import (
    EXPERIMENTAL_STROKES_MARKER,
    StrokeBlock,
    _extract_xy,
    emit_gcode,
    make_stroke_block_from_polyline,
    parse_stroke_blocks,
)

# Defaults (plan)
DEFAULT_PRESSURE_Z_RANGE_MM = 0.25
DEFAULT_FEEDRATE_JITTER = 0.2
DEFAULT_STRIKE_PROBABILITY = 5.0
DEFAULT_WORD_GAP_MM = 1.2
DEFAULT_WORD_MIN_WIDTH_MM = 8.0
DEFAULT_WORD_MIN_STROKES = 3
DEFAULT_JITTER_MM = 0.12
DEFAULT_STRIKE_X_OVERHANG_MM = 1.0
DEFAULT_STRIKE_Y_HEIGHT_RATIO = 0.4
DEFAULT_STRIKE_POINTS_PER_CHAR = 12
DEFAULT_STRIKE_CHAR_WIDTH_MM = 2.5

FEEDRATE_CLAMP_MIN_RATIO = 0.5
FEEDRATE_CLAMP_MAX_RATIO = 1.8


@dataclass
class ExperimentalFlags:
    variable_pressure: bool = False
    variable_feedrate: bool = False
    strikethrough: bool = False


@dataclass
class ExperimentalParams:
    pressure_z_range_mm: float = DEFAULT_PRESSURE_Z_RANGE_MM
    feedrate_jitter: float = DEFAULT_FEEDRATE_JITTER
    strike_probability: float = DEFAULT_STRIKE_PROBABILITY
    word_gap_mm: float = DEFAULT_WORD_GAP_MM
    word_min_width_mm: float = DEFAULT_WORD_MIN_WIDTH_MM
    word_min_strokes: int = DEFAULT_WORD_MIN_STROKES
    jitter_mm: float = DEFAULT_JITTER_MM
    strike_x_overhang_mm: float = DEFAULT_STRIKE_X_OVERHANG_MM
    strike_y_height_ratio: float = DEFAULT_STRIKE_Y_HEIGHT_RATIO
    strike_points_per_char: int = DEFAULT_STRIKE_POINTS_PER_CHAR
    strike_char_width_mm: float = DEFAULT_STRIKE_CHAR_WIDTH_MM
    rng_seed: int | None = None


def _env_bool(key: str) -> bool:
    v = os.environ.get(key, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def resolve_experimental_flags(
    *,
    cli_pressure: bool = False,
    cli_feedrate: bool = False,
    cli_strikethrough: bool = False,
) -> ExperimentalFlags:
    return ExperimentalFlags(
        variable_pressure=cli_pressure or _env_bool("PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE"),
        variable_feedrate=cli_feedrate or _env_bool("PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_FEEDRATE"),
        strikethrough=cli_strikethrough or _env_bool("PDF_TO_PRINT_EXPERIMENTAL_STRIKETHROUGH"),
    )


def any_experimental_enabled(flags: ExperimentalFlags) -> bool:
    return flags.variable_pressure or flags.variable_feedrate or flags.strikethrough


def experimental_enabled_from_env() -> bool:
    """True if any experimental env flag is set (for build.sh Phase 2b skip)."""
    f = resolve_experimental_flags()
    return f.variable_pressure or f.variable_feedrate or f.strikethrough


def resolve_experimental_params() -> ExperimentalParams:
    seed_env = os.environ.get("PDF_TO_PRINT_EXPERIMENTAL_RNG_SEED", "").strip()
    seed = int(seed_env) if seed_env else None
    return ExperimentalParams(
        pressure_z_range_mm=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_PRESSURE_Z_RANGE_MM", DEFAULT_PRESSURE_Z_RANGE_MM
        ),
        feedrate_jitter=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_FEEDRATE_JITTER", DEFAULT_FEEDRATE_JITTER
        ),
        strike_probability=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_STRIKE_PROBABILITY", DEFAULT_STRIKE_PROBABILITY
        ),
        word_gap_mm=_env_float("PDF_TO_PRINT_EXPERIMENTAL_WORD_GAP_MM", DEFAULT_WORD_GAP_MM),
        word_min_width_mm=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_WORD_MIN_WIDTH_MM", DEFAULT_WORD_MIN_WIDTH_MM
        ),
        word_min_strokes=_env_int(
            "PDF_TO_PRINT_EXPERIMENTAL_WORD_MIN_STROKES", DEFAULT_WORD_MIN_STROKES
        ),
        jitter_mm=_env_float("PDF_TO_PRINT_EXPERIMENTAL_JITTER_MM", DEFAULT_JITTER_MM),
        strike_x_overhang_mm=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_STRIKE_X_OVERHANG_MM", DEFAULT_STRIKE_X_OVERHANG_MM
        ),
        strike_y_height_ratio=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_STRIKE_Y_HEIGHT_RATIO", DEFAULT_STRIKE_Y_HEIGHT_RATIO
        ),
        strike_points_per_char=_env_int(
            "PDF_TO_PRINT_EXPERIMENTAL_STRIKE_POINTS_PER_CHAR", DEFAULT_STRIKE_POINTS_PER_CHAR
        ),
        strike_char_width_mm=_env_float(
            "PDF_TO_PRINT_EXPERIMENTAL_STRIKE_CHAR_WIDTH_MM", DEFAULT_STRIKE_CHAR_WIDTH_MM
        ),
        rng_seed=seed,
    )


def interpolate_random(length: int, spacing: int, rng: random.Random) -> list[float]:
    """Smooth random curve in [-1, 1] (ported from p1s_plotter)."""
    if length < 1:
        return []
    spacing = max(1, spacing)
    extra = spacing - (length % spacing)
    if extra == spacing:
        extra = 0
    n = length + extra
    x = np.arange(1, n + 2, spacing)
    y = np.array([rng.uniform(-1, 1) for _ in range(len(x))])
    x_new = np.arange(1, n + 1)
    cs = CubicSpline(x, y)
    return [round(float(v), 4) for v in cs(x_new)[:length]]


def _arc_length_params(poly: np.ndarray) -> np.ndarray:
    """Normalized cumulative arc length t in [0, 1] per vertex."""
    n = len(poly)
    if n <= 1:
        return np.zeros(n)
    d = np.abs(np.diff(poly))
    seg = np.hypot(d.real, d.imag)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = cum[-1]
    if total <= 1e-9:
        return np.linspace(0.0, 1.0, n)
    return cum / total


def _force_profile(t: float, noise: float) -> float:
    """Higher in stroke middle; noise in [-1,1] scaled."""
    base = 0.35 + 0.65 * math.sin(math.pi * t)
    return max(0.0, min(1.5, base + 0.25 * noise))


def apply_variable_pressure(
    block: StrokeBlock,
    *,
    z_pen: float,
    z_range_mm: float,
    rng: random.Random,
) -> None:
    poly = block.polyline_xy()
    n = len(poly)
    if n == 0:
        return
    t_arr = _arc_length_params(poly)
    noises = interpolate_random(n, max(2, n // 8), rng)
    for i in range(n):
        force = _force_profile(float(t_arr[i]), noises[i] if i < len(noises) else 0.0)
        z = z_pen - force * z_range_mm
        z = max(z_pen - z_range_mm, min(z_pen, z))
        if i == 0 and block.travel_index is not None:
            continue
        block.set_drawing_z(i, z)


def apply_variable_feedrate(
    block: StrokeBlock,
    *,
    z_pen: float,
    base_feed_mm_min: float,
    base_speed_mm_s: float,
    jitter_frac: float,
    rng: random.Random,
) -> None:
    poly = block.polyline_xy()
    n = len(poly)
    if n < 2:
        if n == 1 and len(block.drawing_indices) >= 1:
            block.set_drawing_f(0, base_feed_mm_min)
        return
    t_arr = _arc_length_params(poly)
    noises = interpolate_random(n - 1, max(2, (n - 1) // 6), rng)
    f_min = base_feed_mm_min * FEEDRATE_CLAMP_MIN_RATIO
    f_max = base_feed_mm_min * FEEDRATE_CLAMP_MAX_RATIO

    for i in range(n - 1):
        p0, p1 = poly[i], poly[i + 1]
        dist = abs(p1 - p0)
        t_mid = 0.5 * (float(t_arr[i]) + float(t_arr[i + 1]))
        noise = noises[i] if i < len(noises) else 0.0
        speed_factor = 1.0 + jitter_frac * noise
        speed_factor = max(0.35, speed_factor)
        speed_mm_s = base_speed_mm_s * speed_factor
        dt = dist / speed_mm_s if speed_mm_s > 1e-6 else 0.001
        f_val = (dist / dt) * 60.0 if dt > 1e-9 else base_feed_mm_min
        f_val = max(f_min, min(f_max, f_val))
        block.set_drawing_f(i + 1, f_val)


@dataclass
class WordCluster:
    stroke_indices: list[int]

    def bbox(self, blocks: list[StrokeBlock]) -> tuple[float, float, float, float]:
        """Axis-aligned bounds from pen-down G1 XY only (excludes G0 travel)."""
        xs: list[float] = []
        ys: list[float] = []
        for si in self.stroke_indices:
            block = blocks[si]
            for idx in block.drawing_indices:
                xy = _extract_xy(block.lines[idx])
                if xy is not None:
                    xs.append(xy[0])
                    ys.append(xy[1])
        if not xs:
            return 0.0, 0.0, 0.0, 0.0
        return min(xs), min(ys), max(xs), max(ys)


def cluster_word_strokes(
    blocks: list[StrokeBlock],
    *,
    row_gap_mm: float,
    force_axis: str | None,
    invert_y: bool,
    word_gap_mm: float,
    word_min_width_mm: float,
    word_min_strokes: int,
) -> list[WordCluster]:
    from svg_to_gcode import reading_row_metadata_from_lines

    polys = [b.polyline_xy() for b in blocks]
    _, row_id, min_x, mean_x, mean_y_c, min_y_c = reading_row_metadata_from_lines(
        polys,
        invert_y=invert_y,
        row_gap_mm=row_gap_mm,
        force_axis=force_axis,
        coords_are_mm=True,
    )
    n = len(blocks)
    rows: dict[int, list[int]] = {}
    for i in range(n):
        rows.setdefault(int(row_id[i]), []).append(i)

    clusters: list[WordCluster] = []
    for indices in rows.values():
        if not indices:
            continue
        # Within row: sort by min_y (left-right for axis=x layout)
        indices.sort(key=lambda i: (min_y_c[i], mean_y_c[i], min_x[i]))
        cluster_idx = [indices[0]]
        for i in indices[1:]:
            prev = cluster_idx[-1]
            gap = min_y_c[i] - min_y_c[prev]
            if gap > word_gap_mm:
                clusters.append(WordCluster(stroke_indices=cluster_idx))
                cluster_idx = [i]
            else:
                cluster_idx.append(i)
        clusters.append(WordCluster(stroke_indices=cluster_idx))

    out: list[WordCluster] = []
    for c in clusters:
        if len(c.stroke_indices) < word_min_strokes:
            continue
        x0, _, x1, _ = c.bbox(blocks)
        if (x1 - x0) < word_min_width_mm:
            continue
        out.append(c)
    return out


def apply_jitter_to_cluster(
    blocks: list[StrokeBlock],
    cluster: WordCluster,
    *,
    jitter_mm: float,
    rng: random.Random,
) -> None:
    total_pts = sum(len(blocks[si].polyline_xy()) for si in cluster.stroke_indices)
    if total_pts < 1:
        return
    x_off = interpolate_random(total_pts, max(2, total_pts // 6), rng)
    y_off = interpolate_random(total_pts, max(2, total_pts // 6), rng)
    k = 0
    for si in cluster.stroke_indices:
        poly = blocks[si].polyline_xy()
        for vi in range(len(poly)):
            dx = jitter_mm * (x_off[k] if k < len(x_off) else 0.0)
            dy = jitter_mm * (y_off[k] if k < len(y_off) else 0.0)
            blocks[si].set_vertex_xy(vi, float(poly[vi].real) + dx, float(poly[vi].imag) + dy)
            k += 1


def build_strikethrough_block(
    cluster: WordCluster,
    blocks: list[StrokeBlock],
    *,
    z_pen: float,
    z_up: float,
    z_range_mm: float,
    variable_pressure: bool,
    base_feed_mm_min: float,
    x_overhang: float,
    y_height_ratio: float,
    points_per_char: int,
    char_width_mm: float,
    rng: random.Random,
) -> StrokeBlock:
    x0, y0, x1, y1 = cluster.bbox(blocks)
    width = max(x1 - x0, 1e-3)
    y_center = 0.5 * (y0 + y1)
    row_h = max(y1 - y0, 2.0)
    y_line = y_center + y_height_ratio * 0.15 * row_h * rng.uniform(-0.3, 0.3)
    n_chars = max(1, int(round(width / char_width_mm)))
    n_pts = max(3, n_chars * points_per_char)
    xs = np.linspace(x0 - x_overhang, x1 + x_overhang, n_pts)
    y_noise = interpolate_random(n_pts, max(2, n_pts // 5), rng)
    points: list[tuple[float, float, float | None, float | None]] = []
    for i, x in enumerate(xs):
        y = y_line + 0.08 * row_h * (y_noise[i] if i < len(y_noise) else 0.0)
        z: float | None = None
        if variable_pressure:
            t = i / max(1, n_pts - 1)
            force = _force_profile(t, y_noise[i] if i < len(y_noise) else 0.0)
            z = z_pen - force * z_range_mm
            z = max(z_pen - z_range_mm, min(z_pen, z))
        points.append((float(x), float(y), z, base_feed_mm_min))
    return make_stroke_block_from_polyline(points, z_pen=z_pen, z_up=z_up)


def apply_experimental_postprocess(
    path: Path,
    *,
    z_pen: float,
    z_up: float,
    base_feed_mm_min: float,
    base_speed_mm_s: float,
    flags: ExperimentalFlags,
    params: ExperimentalParams,
    reading_row_gap_mm: float,
    reading_force_axis: str | None,
    invert_reading_sort: bool,
) -> None:
    if not any_experimental_enabled(flags):
        return

    rng = random.Random(params.rng_seed)

    blocks, trailing = parse_stroke_blocks(path, z_pen, z_up, stop_at_experimental_marker=True)
    # Drop prior experimental section if re-run
    trailing = [ln for ln in trailing if EXPERIMENTAL_STROKES_MARKER not in ln]

    if flags.variable_pressure:
        for block in blocks:
            apply_variable_pressure(
                block, z_pen=z_pen, z_range_mm=params.pressure_z_range_mm, rng=rng
            )

    if flags.variable_feedrate:
        for block in blocks:
            apply_variable_feedrate(
                block,
                z_pen=z_pen,
                base_feed_mm_min=base_feed_mm_min,
                base_speed_mm_s=base_speed_mm_s,
                jitter_frac=params.feedrate_jitter,
                rng=rng,
            )

    extra_blocks: list[StrokeBlock] = []
    if flags.strikethrough:
        candidates = cluster_word_strokes(
            blocks,
            row_gap_mm=reading_row_gap_mm,
            force_axis=reading_force_axis,
            invert_y=invert_reading_sort,
            word_gap_mm=params.word_gap_mm,
            word_min_width_mm=params.word_min_width_mm,
            word_min_strokes=params.word_min_strokes,
        )
        for cluster in candidates:
            if rng.randint(1, 100) > params.strike_probability:
                continue
            apply_jitter_to_cluster(blocks, cluster, jitter_mm=params.jitter_mm, rng=rng)
            extra_blocks.append(
                build_strikethrough_block(
                    cluster,
                    blocks,
                    z_pen=z_pen,
                    z_up=z_up,
                    z_range_mm=params.pressure_z_range_mm,
                    variable_pressure=flags.variable_pressure,
                    base_feed_mm_min=base_feed_mm_min,
                    x_overhang=params.strike_x_overhang_mm,
                    y_height_ratio=params.strike_y_height_ratio,
                    points_per_char=params.strike_points_per_char,
                    char_width_mm=params.strike_char_width_mm,
                    rng=rng,
                )
            )

    if extra_blocks:
        trailing = [EXPERIMENTAL_STROKES_MARKER, ""] + trailing

    path.write_text(emit_gcode(blocks + extra_blocks, trailing), encoding="utf-8")
