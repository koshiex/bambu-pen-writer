#!/usr/bin/env python3
"""Skeleton pixels → pen strokes with the fewest pen lifts.

The legacy tracer (png_to_skeleton_svg.extract_polylines) cuts a stroke at every pixel whose
degree is not 2. Two effects make that expensive on a plotter whose pen lift is a slow Z move:
  * 8-connected skeletons contain "corner triangles" (two pixels joined both directly and via a
    shared 4-neighbour) that look like junctions, so strokes are split where the ink is continuous;
  * real junctions (T, X, loops with tails) split one connected letter into 3–4 separate strokes.

Here the skeleton becomes a stroke graph (redundant diagonals dropped, junction pixels clustered,
short end spurs pruned). Each connected component is then drawn as Euler trails: odd-degree
vertices close enough are paired by retracing ≤ ``retrace_px`` of existing ink, the rest are paired
by pen lifts, so a component needs max(1, odd/2) lifts instead of one per junction arm. At a
junction the walk continues along the straightest unused edge, which keeps crossings natural.

Trails are finally smoothed (Gaussian along the path, endpoints and cusps pinned) and simplified
(Ramer–Douglas–Peucker): the pixel staircase otherwise forces the motion planner to crawl.

Coordinates: input is a bool image (row, col); output polylines are float (x=col, y=row) in px.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import networkx as nx
import numpy as np

Pixel = tuple[int, int]

DIRECTION_LOOKAHEAD_PX = 3
VIRTUAL_EDGE_PENALTY = 10.0
MAX_SPUR_PASSES = 5


@dataclass(frozen=True)
class TraceParams:
    spur_px: float = 4.0            # prune end branches shorter than this (skeleton artefacts)
    retrace_px: float = 12.0        # max ink path retraced to avoid one pen lift (0 = never)
    smooth_sigma_px: float = 0.8    # Gaussian smoothing along the trail (0 = off)
    simplify_px: float = 0.18       # RDP tolerance after smoothing (0 = off)
    min_points: int = 2             # drop trails with fewer raw pixels
    cusp_angle_deg: float = 100.0   # turning angle kept sharp (pinned) during smoothing


@dataclass
class Edge:
    u: object
    v: object
    pts: np.ndarray                 # (N, 2) float x, y from u to v
    virtual: bool = False

    @property
    def length(self) -> float:
        if self.virtual or len(self.pts) < 2:
            return 0.0
        return float(np.sum(np.hypot(*np.diff(self.pts, axis=0).T)))


# ---------------------------------------------------------------- pixel graph → stroke graph

def pixel_graph(skel: np.ndarray) -> nx.Graph:
    """8-connected pixel graph without diagonals that duplicate a 4-connected detour."""
    ys, xs = np.nonzero(skel)
    pixels = set(zip(ys.tolist(), xs.tolist()))
    g = nx.Graph()
    g.add_nodes_from(pixels)
    for y, x in pixels:
        for dy, dx in ((0, 1), (1, 0)):
            if (y + dy, x + dx) in pixels:
                g.add_edge((y, x), (y + dy, x + dx))
        for dy, dx in ((1, 1), (1, -1)):
            nb = (y + dy, x + dx)
            if nb in pixels and (y + dy, x) not in pixels and (y, x + dx) not in pixels:
                g.add_edge((y, x), nb)
    return g


def _xy(p: Pixel) -> tuple[float, float]:
    return float(p[1]), float(p[0])


def _junction_clusters(g: nx.Graph) -> dict[Pixel, tuple[str, int, float, float]]:
    junctions = [p for p in g if g.degree[p] >= 3]
    sub = g.subgraph(junctions)
    out: dict[Pixel, tuple[str, int, float, float]] = {}
    for k, comp in enumerate(nx.connected_components(sub)):
        cx = float(np.mean([p[1] for p in comp]))
        cy = float(np.mean([p[0] for p in comp]))
        for p in comp:
            out[p] = ("J", k, cx, cy)
    return out


def _walk_chain(g: nx.Graph, start: Pixel, nb: Pixel, is_key, seen: set) -> list[Pixel]:
    chain = [start, nb]
    seen.add(frozenset((start, nb)))
    prev, cur = start, nb
    while not is_key(cur):
        nxt = [q for q in g.neighbors(cur) if q != prev]
        if len(nxt) != 1 or frozenset((cur, nxt[0])) in seen:
            break
        seen.add(frozenset((cur, nxt[0])))
        chain.append(nxt[0])
        prev, cur = cur, nxt[0]
    return chain


def stroke_graph(skel: np.ndarray) -> list[Edge]:
    """Chains between endpoints / junction clusters; pure cycles become self-loops."""
    g = pixel_graph(skel)
    clusters = _junction_clusters(g)

    def is_key(p: Pixel) -> bool:
        return g.degree[p] != 2

    def node_of(p: Pixel):
        return clusters[p][:2] if p in clusters else ("E", p)

    def coord(p: Pixel) -> tuple[float, float]:
        return (clusters[p][2], clusters[p][3]) if p in clusters else _xy(p)

    edges: list[Edge] = []
    seen: set = set()
    for p in g:
        if not is_key(p) or g.degree[p] == 0:
            continue
        for nb in g.neighbors(p):
            if frozenset((p, nb)) in seen:
                continue
            if p in clusters and nb in clusters and clusters[p][:2] == clusters[nb][:2]:
                seen.add(frozenset((p, nb)))
                continue
            chain = _walk_chain(g, p, nb, is_key, seen)
            pts = [coord(chain[0])] + [_xy(q) for q in chain[1:-1]] + [coord(chain[-1])]
            edges.append(Edge(node_of(chain[0]), node_of(chain[-1]), np.asarray(pts, float)))
    for comp in nx.connected_components(g):
        if any(is_key(p) for p in comp) or len(comp) < 3:
            continue
        start = min(comp)
        nb = next(iter(g.neighbors(start)))
        chain = _walk_chain(g, start, nb, lambda q: q == start, seen)
        node = ("C", start)
        edges.append(Edge(node, node, np.asarray([_xy(q) for q in chain], float)))
    return edges


def prune_spurs(edges: list[Edge], spur_px: float) -> list[Edge]:
    """Remove short end branches at junctions; never below degree 2 at the junction."""
    if spur_px <= 0:
        return edges
    edges = list(edges)
    for _ in range(MAX_SPUR_PASSES):
        deg: dict[object, int] = {}
        for e in edges:
            deg[e.u] = deg.get(e.u, 0) + 1
            deg[e.v] = deg.get(e.v, 0) + 1
        removed = False
        for e in sorted(edges, key=lambda e: e.length):
            if e.u == e.v or e.length >= spur_px:
                continue
            for tip, hub in ((e.u, e.v), (e.v, e.u)):
                if deg[tip] == 1 and deg[hub] >= 3:
                    edges.remove(e)
                    deg[tip] -= 1
                    deg[hub] -= 1
                    removed = True
                    break
        if not removed:
            break
    return edges


# ---------------------------------------------------------------- Euler trails

def _components(edges: list[Edge]) -> list[list[Edge]]:
    g = nx.MultiGraph()
    for i, e in enumerate(edges):
        g.add_edge(e.u, e.v, idx=i)
    comps = []
    for nodes in nx.connected_components(g):
        idx = {d["idx"] for _, _, d in g.subgraph(nodes).edges(data=True)}
        comps.append([edges[i] for i in sorted(idx)])
    return comps


def _degrees(edges: list[Edge]) -> dict[object, int]:
    deg: dict[object, int] = {}
    for e in edges:
        deg[e.u] = deg.get(e.u, 0) + 1
        deg[e.v] = deg.get(e.v, 0) + 1
    return deg


def _retrace_pairs(edges: list[Edge], odd: list, retrace_px: float) -> list[Edge]:
    """Duplicate short ink paths between odd vertices (min-weight pairing) to save lifts."""
    if retrace_px <= 0 or len(odd) < 2:
        return []
    g = nx.Graph()
    best: dict[frozenset, Edge] = {}
    for e in edges:
        if e.u == e.v:
            continue
        key = frozenset((e.u, e.v))
        if key not in best or e.length < best[key].length:
            best[key] = e
            g.add_edge(e.u, e.v, w=e.length)
    pair_graph = nx.Graph()
    paths: dict[frozenset, list] = {}
    for i, a in enumerate(odd):
        dist, path = nx.single_source_dijkstra(g, a, cutoff=retrace_px, weight="w")
        for b in odd[i + 1:]:
            if b in dist:
                pair_graph.add_edge(a, b, weight=2 * retrace_px - dist[b])
                paths[frozenset((a, b))] = path[b]
    # A component with odd vertices needs one lift anyway, so one odd pair may stay unpaired
    # as the trail's two ends for free: two dummy nodes can each take one odd vertex with
    # half the weight of a zero-length retrace, and the matching picks which pair to free.
    # Single-edge components (dots, commas, short dashes) keep the out-and-back pass on
    # purpose: their 2–3 px skeleton is too short for min_points / the 0.3 mm length filter,
    # and a second pass gives a dot enough ink.
    if len(edges) > 1:
        for dummy in (("FREE", 0), ("FREE", 1)):
            for a in odd:
                pair_graph.add_edge(dummy, a, weight=retrace_px)
    extra: list[Edge] = []
    for a, b in nx.max_weight_matching(pair_graph):
        if a[0] == "FREE" or b[0] == "FREE":
            continue
        route = paths[frozenset((a, b))]
        for n0, n1 in zip(route[:-1], route[1:]):
            e = best[frozenset((n0, n1))]
            extra.append(Edge(e.u, e.v, e.pts))
    return extra


def _direction(e: Edge, at, leaving: bool) -> np.ndarray | None:
    if e.virtual or len(e.pts) < 2:
        return None
    pts = e.pts if (e.u == at) == leaving else e.pts[::-1]
    m = min(DIRECTION_LOOKAHEAD_PX, len(pts) - 1)
    d = pts[m] - pts[0] if leaving else pts[-1] - pts[-1 - m]
    n = float(np.hypot(*d))
    return d / n if n > 1e-9 else None


def _pick_edge(cands: list[int], edges: list[Edge], at, incoming: np.ndarray | None) -> int:
    def cost(i: int) -> float:
        e = edges[i]
        if e.virtual:
            return VIRTUAL_EDGE_PENALTY
        out = _direction(e, at, leaving=True)
        if incoming is None or out is None:
            return 1.0
        return 1.0 - float(np.dot(incoming, out))
    return min(cands, key=cost)


def _euler_circuit(edges: list[Edge], start) -> list[tuple[int, object, object]]:
    """Hierholzer walk preferring the straightest continuation; returns (edge, from, to)."""
    adj: dict[object, list[int]] = {}
    for i, e in enumerate(edges):
        adj.setdefault(e.u, []).append(i)
        if e.v != e.u:
            adj.setdefault(e.v, []).append(i)
    used = [False] * len(edges)
    stack: list[tuple[object, tuple[int, object, object] | None]] = [(start, None)]
    circuit: list[tuple[int, object, object]] = []
    while stack:
        v, arrived = stack[-1]
        cands = [i for i in adj.get(v, []) if not used[i]]
        if cands:
            incoming = None
            if arrived is not None:
                incoming = _direction(edges[arrived[0]], v, leaving=False)
            i = _pick_edge(cands, edges, v, incoming)
            used[i] = True
            e = edges[i]
            w = e.v if e.u == v else e.u
            stack.append((w, (i, v, w)))
        else:
            stack.pop()
            if arrived is not None:
                circuit.append(arrived)
    circuit.reverse()
    return circuit


def _trail_points(steps: list[tuple[int, object, object]], edges: list[Edge]) -> np.ndarray:
    parts: list[np.ndarray] = []
    for i, frm, _to in steps:
        e = edges[i]
        pts = e.pts if e.u == frm else e.pts[::-1]
        parts.append(pts if not parts else pts[1:])
    return np.concatenate(parts) if parts else np.zeros((0, 2))


def euler_trails(edges: list[Edge], retrace_px: float) -> list[np.ndarray]:
    trails: list[np.ndarray] = []
    for k, comp in enumerate(_components(edges)):
        deg = _degrees(comp)
        odd = sorted((n for n, d in deg.items() if d % 2), key=str)
        comp = comp + _retrace_pairs(comp, odd, retrace_px)
        deg = _degrees(comp)
        odd = [n for n, d in deg.items() if d % 2]
        start = comp[0].u
        if odd:
            hub = ("V", k)
            comp = comp + [Edge(hub, n, np.zeros((0, 2)), virtual=True) for n in odd]
            start = hub
        steps = _euler_circuit(comp, start)
        run: list[tuple[int, object, object]] = []
        for step in steps + [(-1, None, None)]:
            if step[0] == -1 or comp[step[0]].virtual:
                if run:
                    trails.append(_trail_points(run, comp))
                run = []
            else:
                run.append(step)
    return trails


# ---------------------------------------------------------------- smoothing / simplification

def _cusp_pins(pts: np.ndarray, cusp_angle_deg: float) -> list[int]:
    n = len(pts)
    pins = [0, n - 1]
    lim = math.cos(math.radians(cusp_angle_deg))
    k = 2
    for i in range(k, n - k):
        a = pts[i] - pts[i - k]
        b = pts[i + k] - pts[i]
        na, nb = float(np.hypot(*a)), float(np.hypot(*b))
        if na > 1e-9 and nb > 1e-9 and float(np.dot(a, b)) / (na * nb) < lim:
            pins.append(i)
    return sorted(set(pins))


def _smooth_open(seg: np.ndarray, sigma: float) -> np.ndarray:
    n = len(seg)
    r = min(int(math.ceil(3 * sigma)), n - 2)
    if r < 1:
        return seg
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = seg.copy()
    for d in range(2):
        col = seg[:, d]
        head = 2 * col[0] - col[r:0:-1]
        tail = 2 * col[-1] - col[-2:-r - 2:-1]
        out[:, d] = np.convolve(np.concatenate([head, col, tail]), k, mode="valid")
    out[0], out[-1] = seg[0], seg[-1]
    return out


def _smooth_closed(pts: np.ndarray, sigma: float) -> np.ndarray:
    ring = pts[:-1]
    n = len(ring)
    r = min(int(math.ceil(3 * sigma)), n // 2)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = np.empty_like(ring)
    for d in range(2):
        col = np.concatenate([ring[-r:, d], ring[:, d], ring[:r, d]])
        out[:, d] = np.convolve(col, k, mode="valid")
    return np.vstack([out, out[:1]])


def smooth_trail(pts: np.ndarray, sigma: float, cusp_angle_deg: float) -> np.ndarray:
    if sigma <= 0 or len(pts) < 4:
        return pts
    closed = bool(np.allclose(pts[0], pts[-1]))
    pins = _cusp_pins(pts, cusp_angle_deg)
    if closed and pins == [0, len(pts) - 1]:
        return _smooth_closed(pts, sigma)
    out = pts.copy()
    for a, b in zip(pins[:-1], pins[1:]):
        if b - a >= 3:
            out[a:b + 1] = _smooth_open(pts[a:b + 1], sigma)
    return out


def rdp(pts: np.ndarray, eps: float) -> np.ndarray:
    if eps <= 0 or len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, seg = pts[i], pts[j] - pts[i]
        rel = pts[i + 1:j] - a
        length = float(np.hypot(*seg))
        if length > 1e-12:
            dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / length
        else:
            dist = np.hypot(rel[:, 0], rel[:, 1])
        m = int(np.argmax(dist))
        if dist[m] > eps:
            keep[i + 1 + m] = True
            stack += [(i, i + 1 + m), (i + 1 + m, j)]
    return pts[keep]


# ---------------------------------------------------------------- entry point

def trace_strokes(skel: np.ndarray, params: TraceParams = TraceParams()) -> list[np.ndarray]:
    """Skeleton bool image → list of (N, 2) float polylines (x=col, y=row), fewest pen lifts."""
    edges = prune_spurs(stroke_graph(skel), params.spur_px)
    out: list[np.ndarray] = []
    for trail in euler_trails(edges, params.retrace_px):
        if len(trail) < max(2, params.min_points):
            continue
        trail = smooth_trail(trail, params.smooth_sigma_px, params.cusp_angle_deg)
        out.append(rdp(trail, params.simplify_px))
    return out
