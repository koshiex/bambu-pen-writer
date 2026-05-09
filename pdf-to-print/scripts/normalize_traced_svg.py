#!/usr/bin/env python3
"""Wrap potrace SVG so root dimensions match Inkscape PDF→SVG export (px @ 96 dpi).

Potrace emits paths in bitmap pixel space (viewBox 0 0 W H). vpype + svg_to_gcode expect
the same user units as `extract_pages.sh`: portrait page ≈ 623.68 × 774.88 (165×205 mm).

Usage:
  python3 scripts/normalize_traced_svg.py INPUT_potrace.svg OUTPUT.svg
  python3 scripts/normalize_traced_svg.py INPUT.svg OUTPUT.svg --width 623.68 --height 774.88
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"

# Same portrait user units as Inkscape `--export-text-to-path` from this project's PDFs.
DEFAULT_WIDTH_UU = 623.67999
DEFAULT_HEIGHT_UU = 774.88


def parse_viewbox_wh(root: ET.Element) -> tuple[float, float]:
    vb = root.get("viewBox")
    if not vb:
        sys.exit("ERROR: potrace SVG missing viewBox")
    parts = [float(x) for x in vb.replace(",", " ").split()]
    if len(parts) != 4:
        sys.exit("ERROR: invalid viewBox")
    return parts[2], parts[3]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=str)
    p.add_argument("output", type=str)
    p.add_argument("--width", type=float, default=DEFAULT_WIDTH_UU, help="target width (user units)")
    p.add_argument("--height", type=float, default=DEFAULT_HEIGHT_UU, help="target height (user units)")
    args = p.parse_args()

    ET.register_namespace("", SVG_NS)

    tree = ET.parse(args.input)
    root = tree.getroot()

    rw, rh = parse_viewbox_wh(root)
    if rw <= 0 or rh <= 0:
        sys.exit("ERROR: non-positive raster dimensions from viewBox")

    tw, th = args.width, args.height
    sx = tw / rw
    sy = th / rh

    wrapper = ET.Element(f"{{{SVG_NS}}}g")
    wrapper.set("transform", f"scale({sx},{sy})")
    wrapper.set("id", "potrace_scaled")

    for child in list(root):
        root.remove(child)
        wrapper.append(child)
    root.append(wrapper)

    root.set("width", str(tw))
    root.set("height", str(th))
    root.set("viewBox", f"0 0 {tw} {th}")
    if "preserveAspectRatio" in root.attrib:
        del root.attrib["preserveAspectRatio"]

    tree.write(args.output, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    main()
