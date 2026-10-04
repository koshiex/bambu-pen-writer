#!/usr/bin/env python3
"""Tests for printer_fidelity: G-code ink mapped back onto the source page raster.

Run: python3 scripts/test_printer_fidelity.py   (runs vpype; writes ~/.vpype.toml like the pipeline)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gcode_stroke_parse import parse_stroke_polylines  # noqa: E402
from printer_fidelity import PageAffine, page_fidelity  # noqa: E402
from printer_sim import PageStats  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
W_UU, H_UU = 623.67999, 774.88
W_PX, H_PX = 1949, 2421
PX_PER_UU = W_PX / W_UU


def source_page(tmp: Path, dot: bool = True) -> Path:
    im = Image.new("L", (W_PX, H_PX), 255)
    d = ImageDraw.Draw(im)
    d.line([(100 * PX_PER_UU, 100 * PX_PER_UU), (400 * PX_PER_UU, 100 * PX_PER_UU)], fill=0, width=5)
    if dot:
        c = (500 * PX_PER_UU, 600 * PX_PER_UU)
        d.ellipse([c[0] - 4, c[1] - 4, c[0] + 4, c[1] + 4], fill=0)
    png = tmp / "page_01.png"
    im.save(png)
    return png


def page_with(strokes_uu: list[list[tuple[float, float]]], aff: PageAffine) -> PageStats:
    p = PageStats(number=1)
    p.strokes = [np.array([aff.uu_to_pen(x, y) for x, y in s]) for s in strokes_uu]
    return p


def test_affine_roundtrip_through_real_pipeline() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "svg").mkdir()
        (t / "svg" / "page_01.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W_UU}" height="{H_UU}" '
            f'viewBox="0 0 {W_UU} {H_UU}"><path d="M100 120 L400 130 L380 600" stroke="black" '
            f'fill="none"/></svg>')
        subprocess.run([sys.executable, str(ROOT / "scripts" / "svg_to_gcode.py"), "--soft-holder",
                        "--svg-dir", str(t / "svg"), "--out-dir", str(t / "g")],
                       check=True, capture_output=True, cwd=str(ROOT))
        aff = PageAffine.for_holder("soft")
        pts = parse_stroke_polylines(t / "g" / "page_01.gcode", 74.2, 80.2)[0]
        back = sorted((round(u, 1), round(v, 1)) for u, v in
                      (aff.nozzle_to_uu(p.real, p.imag) for p in pts))
        want = sorted([(100.0, 120.0), (400.0, 130.0), (380.0, 600.0)])
        assert len(back) == 3, back
        for (u, v), (wu, wv) in zip(back, want):
            assert abs(u - wu) < 0.2 and abs(v - wv) < 0.2, (back, want)


def test_faithful_ink_passes() -> None:
    aff = PageAffine.for_holder("soft")
    with tempfile.TemporaryDirectory() as td:
        png = source_page(Path(td))
        page = page_with([[(100, 100), (400, 100)], [(499.5, 600), (500.5, 600)]], aff)
        res = page_fidelity(page, png, aff)
        assert not res.errors, res
        assert res.missing_components == 0


def test_missing_dot_is_text_missing() -> None:
    aff = PageAffine.for_holder("soft")
    with tempfile.TemporaryDirectory() as td:
        png = source_page(Path(td))
        page = page_with([[(100, 100), (400, 100)]], aff)
        res = page_fidelity(page, png, aff)
        assert "TEXT_MISSING" in {e.code for e in res.errors}, res


def test_stray_line_is_ink_outside_glyph() -> None:
    aff = PageAffine.for_holder("soft")
    with tempfile.TemporaryDirectory() as td:
        png = source_page(Path(td))
        page = page_with([[(100, 100), (400, 100)], [(499.5, 600), (500.5, 600)],
                          [(400, 100), (400, 140)]], aff)   # 40 uu ≈ 10.6 mm stray tail
        res = page_fidelity(page, png, aff)
        assert "INK_OUTSIDE_GLYPH" in {e.code for e in res.errors}, res
        assert res.max_outside_mm > 5.0


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_printer_fidelity: {len(tests)} passed")


if __name__ == "__main__":
    main()
