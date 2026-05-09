#!/usr/bin/env python3
"""PNG (page raster) → skeleton → SVG paths — single centerline per stroke.

Potrace traces bitmap *boundaries*; a thick stroke is a ring of pixels → inner + outer
contours. Skeletonization yields one polyline through the middle (pen-friendly).

Usage:
  python3 scripts/png_to_skeleton_svg.py input.png output.svg
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import networkx as nx
import numpy as np
from PIL import Image
from skimage.filters import threshold_otsu
from skimage.morphology import remove_small_holes, remove_small_objects, skeletonize

SVG_NS = "http://www.w3.org/2000/svg"

DEFAULT_WIDTH_UU = 623.67999
DEFAULT_HEIGHT_UU = 774.88


def load_binary_mask(path: Path, max_speck_px: int, max_hole_px: int) -> np.ndarray:
    """Foreground True = ink (dark on light paper). Handles aliased / bilevel PNG."""
    img = np.asarray(Image.open(path).convert("L"))
    u = np.unique(img)
    if len(u) == 2 and set(u.tolist()) <= {0, 255}:
        ink = img == 0
    else:
        t = threshold_otsu(img)
        if t <= 5:
            # Sparse ink: Otsu degenerates — separate dark tail from white page
            t = float(np.percentile(img, 99.9))
            if t >= 254:
                t = 128.0
        ink = img < t

    ink = remove_small_holes(ink, max_size=max_hole_px)
    ink = remove_small_objects(ink, max_size=max_speck_px)
    return ink


def skeleton_to_graph(skel: np.ndarray) -> nx.Graph:
    ys, xs = np.nonzero(skel)
    coords = list(zip(ys.tolist(), xs.tolist()))
    idx_set = set(coords)
    G = nx.Graph()
    G.add_nodes_from(coords)
    for y, x in coords:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                nb = (y + dy, x + dx)
                if nb in idx_set:
                    G.add_edge((y, x), nb)
    return G


def norm_edge(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    return tuple(sorted((a, b)))


def extract_polylines(G: nx.Graph) -> list[list[tuple[int, int]]]:
    """Chains between junctions/endpoints; odd-degree rings if no specials."""
    if G.number_of_nodes() == 0:
        return []

    special = {n for n in G.nodes() if G.degree[n] != 2}
    seen_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    paths: list[list[tuple[int, int]]] = []

    for s in special:
        for nb in list(G.neighbors(s)):
            e = norm_edge(s, nb)
            if e in seen_edges:
                continue
            path = [s, nb]
            seen_edges.add(e)
            prev, cur = s, nb
            while cur not in special:
                nxt_list = [x for x in G.neighbors(cur) if x != prev]
                if len(nxt_list) != 1:
                    break
                nxt = nxt_list[0]
                ek = norm_edge(cur, nxt)
                if ek in seen_edges:
                    break
                seen_edges.add(ek)
                path.append(nxt)
                prev, cur = cur, nxt
            paths.append(path)

    # Components that are simple cycles (every vertex degree 2)
    for comp in nx.connected_components(G):
        sub = G.subgraph(comp)
        if sub.number_of_nodes() == 0:
            continue
        if any(sub.degree[n] != 2 for n in sub):
            continue
        nodes = list(comp)
        start = nodes[0]
        nbrs = list(sub.neighbors(start))
        if not nbrs:
            continue
        nb = nbrs[0]
        e0 = norm_edge(start, nb)
        if e0 in seen_edges:
            continue
        path = [start, nb]
        seen_edges.add(e0)
        prev, cur = start, nb
        safety = 0
        max_it = sub.number_of_edges() + 3
        while safety < max_it:
            safety += 1
            nxt_list = [x for x in sub.neighbors(cur) if x != prev]
            if not nxt_list:
                break
            nxt = nxt_list[0]
            ek = norm_edge(cur, nxt)
            if ek in seen_edges:
                break
            seen_edges.add(ek)
            path.append(nxt)
            prev, cur = cur, nxt
            if nxt == start:
                break
        if len(path) > 3:
            paths.append(path)

    return paths


def polylines_to_paths(
    paths: list[list[tuple[int, int]]],
    w_px: int,
    h_px: int,
    tw: float,
    th: float,
    min_points: int,
) -> list[ET.Element]:
    sx = tw / w_px
    sy = th / h_px
    elems: list[ET.Element] = []
    for path in paths:
        if len(path) < min_points:
            continue
        parts: list[str] = []
        r0, c0 = path[0]
        parts.append(f"M{c0 * sx:.5f} {r0 * sy:.5f}")
        for i in range(1, len(path)):
            r, c = path[i]
            parts.append(f"L{c * sx:.5f} {r * sy:.5f}")
        d = " ".join(parts)
        pe = ET.Element(f"{{{SVG_NS}}}path")
        pe.set("d", d)
        # vpype follows geometry from `d`; filled closed shapes vs open strokes —
        # use explicit black stroke so svgelements treats as drawable curves.
        pe.set("fill", "none")
        pe.set("stroke", "#000000")
        pe.set("stroke-width", str(min(tw, th) * 0.0015))
        pe.set("stroke-linecap", "round")
        pe.set("stroke-linejoin", "round")
        elems.append(pe)
    return elems


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("png", type=Path)
    p.add_argument("svg", type=Path)
    p.add_argument("--width", type=float, default=DEFAULT_WIDTH_UU)
    p.add_argument("--height", type=float, default=DEFAULT_HEIGHT_UU)
    p.add_argument("--max-speck-px", type=int, default=16, help="remove fg blobs ≤ this size")
    p.add_argument("--max-hole-px", type=int, default=64, help="fill holes ≤ this size")
    p.add_argument("--min-polyline-points", type=int, default=2)
    args = p.parse_args()

    ink = load_binary_mask(args.png, args.max_speck_px, args.max_hole_px)
    h_px, w_px = ink.shape

    sk = skeletonize(ink)
    G = skeleton_to_graph(sk)
    polylines = extract_polylines(G)

    ET.register_namespace("", SVG_NS)
    svg = ET.Element(f"{{{SVG_NS}}}svg")
    svg.set("width", str(args.width))
    svg.set("height", str(args.height))
    svg.set("viewBox", f"0 0 {args.width} {args.height}")

    grp = ET.Element(f"{{{SVG_NS}}}g")
    grp.set("id", "skeleton_layer")
    for elem in polylines_to_paths(polylines, w_px, h_px, args.width, args.height, args.min_polyline_points):
        grp.append(elem)
    svg.append(grp)

    tree = ET.ElementTree(svg)
    tree.write(args.svg, encoding="utf-8", xml_declaration=True)

    if not polylines:
        print(f"  warning: no skeleton paths for {args.png}", file=sys.stderr)


if __name__ == "__main__":
    main()
