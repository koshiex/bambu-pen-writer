#!/usr/bin/env python3
"""Merge per-page G-code drawing blocks with Bambu wrapper into a single file.

Output structure (must match Bambu Studio gcode format for P1S SD-card playback):
  ; HEADER_BLOCK_START ... HEADER_BLOCK_END
  ; CONFIG_BLOCK_START ... CONFIG_BLOCK_END   (verbatim from benchy template)
  ; EXECUTABLE_BLOCK_START
  M73 P0 R<min>                       (progress placeholder)
  M201/M203/M204/M205 motion limits   (required by P1S firmware)
  M106 S0 / M106 P2 S0                (fans off)
  ; FEATURE: Custom
  [bambu_start.gcode body]
  ; CHANGE_LAYER / ; Z_HEIGHT / ; LAYER_HEIGHT
  M73 L1 / M73 P<%> R<min>
  ;===== PAGE 01 =====
  [page_01.gcode]
  [page_pause.gcode (NEXT_PAGE=02)]
  ; CHANGE_LAYER ...
  ;===== PAGE 02 =====
  ...
  [bambu_end.gcode]
  M73 P100 R0
  ; EXECUTABLE_BLOCK_END

Templates:
  templates/bambu_header_block.gcode  — extracted from real Bambu Studio benchy
  templates/bambu_config_block.gcode  — extracted from real Bambu Studio benchy
  templates/bambu_start.gcode         — our minimal init (will sit inside EXECUTABLE_BLOCK)
  templates/bambu_end.gcode           — our minimal end
  templates/page_pause.gcode          — page-flip pause

Usage:
  python3 scripts/merge_pages.py [--gcode-dir DIR] [--templates-dir DIR] [--out PATH]
                                 [--minutes-per-page MIN]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def read(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"ERROR: missing {path}")
    return path.read_text()


def patch_header(template: str, n_pages: int, total_min: int) -> str:
    """Update HEADER_BLOCK fields to reflect our content."""
    out = template
    out = re.sub(
        r"; total layer number:.*",
        f"; total layer number: {n_pages}",
        out,
    )
    out = re.sub(
        r"; model printing time:.*",
        f"; model printing time: {total_min}m 0s; total estimated time: {total_min}m 0s",
        out,
    )
    return out


def motion_limits() -> str:
    """P1S motion limits — verbatim from benchy. Firmware uses these to validate
    speeds in user G-code."""
    return (
        "M201 X20000 Y20000 Z500 E5000\n"
        "M203 X500 Y500 Z20 E30\n"
        "M204 P20000 R5000 T20000\n"
        "M205 X9.00 Y9.00 Z3.00 E2.50\n"
        "M106 S0\n"
        "M106 P2 S0\n"
        "; FEATURE: Custom\n"
    )


def layer_marker(layer_num: int, z_height: float = 25.0) -> str:
    """Bambu-style layer change marker. P1S firmware uses these for LCD progress
    UI and to tell layers apart for pause/resume.
    Format derived from benchy at lines 603-605:
        ; CHANGE_LAYER
        ; Z_HEIGHT: N
        ; LAYER_HEIGHT: N
    """
    return (
        f"; CHANGE_LAYER\n"
        f"; Z_HEIGHT: {z_height:.2f}\n"
        f"; LAYER_HEIGHT: 0.10\n"
        f"M73 L{layer_num}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcode-dir", default="build/gcode")
    parser.add_argument("--templates-dir", default="templates")
    parser.add_argument("--out", default="output/notebook.gcode")
    parser.add_argument("--minutes-per-page", type=int, default=15,
                        help="rough estimate per page for M73 remaining-time")
    args = parser.parse_args()

    gcode_dir = Path(args.gcode_dir)
    tpl_dir = Path(args.templates_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pages = sorted(gcode_dir.glob("page_*.gcode"))
    if not pages:
        sys.exit(f"ERROR: no page_*.gcode in {gcode_dir}")

    n = len(pages)
    mpp = args.minutes_per_page
    total_min = n * mpp

    # Required Bambu blocks
    header = read(tpl_dir / "bambu_header_block.gcode")
    header = patch_header(header, n, total_min)
    config = read(tpl_dir / "bambu_config_block.gcode")

    # Our content
    start = read(tpl_dir / "bambu_start.gcode")
    end = read(tpl_dir / "bambu_end.gcode")
    pause_tpl = read(tpl_dir / "page_pause.gcode")

    print(f"Merging {n} pages -> {out_path} (~{mpp} min/page, est {total_min} min total)")

    with out_path.open("w") as out:
        # Bambu HEADER + CONFIG (firmware-required, verbatim from real Bambu file)
        out.write(header)
        out.write("\n")
        out.write(config)
        out.write("\n")

        # EXECUTABLE_BLOCK
        out.write("; EXECUTABLE_BLOCK_START\n")
        out.write(f"M73 P0 R{total_min}\n")
        out.write(motion_limits())

        # Our minimal start G-code
        out.write(start)
        out.write("\n")

        # Per-page content
        for i, page in enumerate(pages, start=1):
            out.write(f";===== PAGE {i:02d} =====\n")
            out.write(layer_marker(i))
            page_progress = int((i - 1) / n * 100)
            page_remaining = (n - i + 1) * mpp
            out.write(f"M73 P{page_progress} R{page_remaining}\n")
            out.write(page.read_text())

            if i < n:
                next_progress = int(i / n * 100)
                next_remaining = (n - i) * mpp
                pause = pause_tpl
                pause = pause.replace("{NEXT_PAGE}", f"{i+1:02d}")
                pause = pause.replace("{PROGRESS}", str(next_progress))
                pause = pause.replace("{REMAINING}", str(next_remaining))
                out.write(pause)

        out.write("\nM73 P100 R0\n")
        out.write(end)
        out.write("\n; EXECUTABLE_BLOCK_END\n")

    size_mb = out_path.stat().st_size / 1e6
    pause_count = n - 1
    print(f"✅ {out_path} ({size_mb:.1f} MB, {n} pages, "
          f"{pause_count} flip-pauses, ~{total_min} min estimated)")


if __name__ == "__main__":
    main()
