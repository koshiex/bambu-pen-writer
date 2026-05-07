#!/usr/bin/env python3
"""Inspect SVG files extracted from PDF.

Reports for each SVG:
  - viewBox / declared page size (mm)
  - element counts: path / text / image / rect / etc
  - bounding box of actual content (non-blank area)
  - hint: vector vs raster

Usage:
  python3 scripts/inspect_svg.py build/inspect/page_01.svg [more_svgs...]
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"svg": "http://www.w3.org/2000/svg"}


def parse_viewbox(root: ET.Element) -> tuple[float, float, float, float] | None:
    vb = root.get("viewBox")
    if not vb:
        return None
    parts = [float(x) for x in vb.replace(",", " ").split()]
    if len(parts) != 4:
        return None
    return tuple(parts)  # type: ignore[return-value]


def parse_size_mm(root: ET.Element) -> tuple[float, float] | None:
    """Try width/height attributes. Inkscape default unit when extracting from PDF
    is user units (px @ 96dpi), unless explicit mm/cm/etc unit is present."""
    w = root.get("width")
    h = root.get("height")
    if not w or not h:
        return None

    def to_mm(val: str) -> float:
        m = re.match(r"^([\d.]+)(\D*)$", val.strip())
        if not m:
            return float("nan")
        num = float(m.group(1))
        unit = m.group(2).lower()
        if unit == "":
            return num * 25.4 / 96  # Inkscape user units = px @ 96 dpi
        if unit == "mm":
            return num
        if unit == "cm":
            return num * 10
        if unit == "px":
            return num * 25.4 / 96
        if unit == "in":
            return num * 25.4
        if unit == "pt":
            return num * 25.4 / 72
        return num

    return to_mm(w), to_mm(h)


def count_elements(root: ET.Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]  # strip ns
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def detect_raster(counts: dict[str, int], file_size: int) -> str:
    """Heuristic: if many <image> elements OR file very small with few paths -> raster."""
    images = counts.get("image", 0)
    paths = counts.get("path", 0)
    if images > 0 and paths < 10:
        return f"RASTER (image={images}, path={paths})"
    if paths > 100:
        return f"VECTOR (path={paths})"
    if paths > 0 and images == 0:
        return f"VECTOR (path={paths}, no images)"
    return "UNKNOWN — manual check"


def first_path_bbox_hint(svg_path: Path) -> str:
    """Cheap hint: scan first few paths and print their starting coordinates."""
    try:
        with svg_path.open() as f:
            content = f.read(200_000)
        path_strs = re.findall(r'd="([^"]{1,100})', content)[:5]
        return " | ".join(p[:60] + "..." if len(p) > 60 else p for p in path_strs)
    except Exception as e:
        return f"<error: {e}>"


def report(svg_path: Path) -> None:
    print(f"\n=== {svg_path.name} ({svg_path.stat().st_size:,} bytes) ===")
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"  parse error: {e}")
        return

    vb = parse_viewbox(root)
    size_mm = parse_size_mm(root)
    if vb:
        print(f"  viewBox: x={vb[0]:.2f} y={vb[1]:.2f} w={vb[2]:.2f} h={vb[3]:.2f}")
    if size_mm:
        print(f"  declared size: {size_mm[0]:.2f} × {size_mm[1]:.2f} mm")

    counts = count_elements(root)
    interesting = [
        "path", "text", "image", "rect", "g", "use", "circle", "polyline", "line"
    ]
    summary = {k: counts.get(k, 0) for k in interesting if counts.get(k, 0) > 0}
    print(f"  elements: {summary}")

    print(f"  detection: {detect_raster(counts, svg_path.stat().st_size)}")
    print(f"  first paths preview: {first_path_bbox_hint(svg_path)}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for arg in sys.argv[1:]:
        report(Path(arg))


if __name__ == "__main__":
    main()
