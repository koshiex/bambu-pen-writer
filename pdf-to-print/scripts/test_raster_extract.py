#!/usr/bin/env python3
"""Tests for scripts/extract_pages_raster.sh rasterisation (PDF page -> PNG -> skeleton SVG).

Ghostscript is the default rasteriser: Inkscape's PDF import drops the alternate glyphs Word
embeds and shifts the rest of the line, so the plotted text did not match the PDF.

Run: python3 scripts/test_raster_extract.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "extract_pages_raster.sh"
PAGE_PT = (165 / 25.4 * 72, 205 / 25.4 * 72)          # notebook page in points


def make_pdf(path: Path) -> None:
    ps = path.with_suffix(".ps")
    ps.write_text(
        "%!PS\n"
        f"<< /PageSize [{PAGE_PT[0]:.2f} {PAGE_PT[1]:.2f}] >> setpagedevice\n"
        "/Helvetica findfont 28 scalefont setfont\n"
        "40 500 moveto (plotter) show\n"
        "4 setlinewidth 40 300 moveto 400 300 lineto stroke\n"
        "showpage\n")
    subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                    f"-sOutputFile={path}", str(ps)], check=True)


def run(pdf: Path, out: Path, keep: Path, **env: str) -> subprocess.CompletedProcess:
    full = {**os.environ, "PNG_KEEP_DIR": str(keep), **env}
    return subprocess.run(["bash", str(SCRIPT), str(pdf), str(out)], cwd=ROOT, env=full,
                          capture_output=True, text=True)


def test_ghostscript_is_the_default_rasteriser() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        pdf = t / "doc.pdf"
        make_pdf(pdf)
        r = run(pdf, t / "svg", t / "png")
        assert r.returncode == 0, r.stderr
        assert "rasterizer=gs" in r.stdout, r.stdout
        assert (t / "svg" / "page_01.svg").is_file()
        img = Image.open(t / "png" / "page_01.png")
        assert img.mode == "L", img.mode                       # gs pnggray: white paper, no alpha
        assert abs(img.size[0] - 165 / 25.4 * 300) <= 2, img.size
        ink = np.asarray(img) < 128
        assert 0.002 < ink.mean() < 0.2, ink.mean()            # text + rule rendered, page not black


def test_unknown_rasteriser_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        pdf = t / "doc.pdf"
        make_pdf(pdf)
        r = run(pdf, t / "svg", t / "png", RASTERIZER="poppler")
        assert r.returncode != 0 and "RASTERIZER" in r.stderr, (r.returncode, r.stderr)


def main() -> None:
    if shutil.which("gs") is None:
        print("test_raster_extract: skipped (no gs)")
        return
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_raster_extract: {len(tests)} passed")


if __name__ == "__main__":
    main()
