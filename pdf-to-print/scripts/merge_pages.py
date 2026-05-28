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
                                 [--minutes-per-page MIN] [--page-order sequential|spread]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from holder_config import (  # noqa: E402
    PARK_NOZZLE_X_MM,
    PARK_NOZZLE_Y_MM,
    select_profile,
    travel_feed_mm_min,
    z_travel_feed_mm_min,
    z_travel_clearance_for_profile,
)
from page_order import (  # noqa: E402
    SPREAD_BOUNDARIES,
    is_spread_boundary,
    page_num_from_path,
    page_paths,
)


def read(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"ERROR: missing {path}")
    return path.read_text()


def patch_holder_templates(
    text: str,
    z_travel_clearance: float,
    park_x: float,
    park_y: float,
    travel_f: int,
    z_travel_f: int,
) -> str:
    """Substitute holder placeholders in start/pause/end templates."""
    return (
        text.replace("{Z_TRAVEL_CLEARANCE}", f"{z_travel_clearance:.1f}")
        .replace("{PARK_X}", f"{park_x:.0f}")
        .replace("{PARK_Y}", f"{park_y:.0f}")
        .replace("{TRAVEL_FEED}", str(travel_f))
        .replace("{Z_TRAVEL_FEED}", str(z_travel_f))
    )


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
    parser.add_argument(
        "--page-order",
        choices=("sequential", "spread"),
        default="sequential",
        help="merge order: sequential (PDF 1..N) or spread (unfolded 24-page signature)",
    )
    parser.add_argument(
        "--soft-holder",
        action="store_true",
        help="use soft-holder Z travel clearance in templates (env PDF_TO_PRINT_SOFT_HOLDER)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        metavar="N",
        help="resume from page N (1-based); output contains pages >= N only",
    )
    args = parser.parse_args()

    if args.start_page < 1:
        sys.exit(f"ERROR: --start-page must be >= 1, got {args.start_page}")

    soft_holder = args.soft_holder or os.environ.get(
        "PDF_TO_PRINT_SOFT_HOLDER", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    pause_beep = os.environ.get("PDF_TO_PRINT_PAUSE_BEEP", "1").strip().lower() in (
        "1", "true", "yes", "on"
    )
    profile = select_profile(soft_holder=soft_holder)
    z_clear = z_travel_clearance_for_profile(profile)

    gcode_dir = Path(args.gcode_dir)
    tpl_dir = Path(args.templates_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.page_order == "spread" and args.start_page > 1 and not is_spread_boundary(args.start_page):
        print(
            f"WARN: --start-page {args.start_page} is not at a spread boundary "
            f"{sorted(SPREAD_BOUNDARIES)}. Operator must mount the correct loose sheet "
            f"at the correct orientation manually.",
            file=sys.stderr,
        )

    pages = page_paths(gcode_dir, args.page_order, start_page=args.start_page)
    n = len(pages)
    mpp = args.minutes_per_page
    total_min = n * mpp

    # Required Bambu blocks
    header = read(tpl_dir / "bambu_header_block.gcode")
    header = patch_header(header, n, total_min)
    config = read(tpl_dir / "bambu_config_block.gcode")

    # Our content (Z lift scales with holder — soft-holder needs ~83 mm vs UMTS 50 mm)
    park_kw = dict(
        park_x=PARK_NOZZLE_X_MM,
        park_y=PARK_NOZZLE_Y_MM,
        travel_f=travel_feed_mm_min(),
        z_travel_f=z_travel_feed_mm_min(),
    )
    start = patch_holder_templates(read(tpl_dir / "bambu_start.gcode"), z_clear, **park_kw)
    end = patch_holder_templates(read(tpl_dir / "bambu_end.gcode"), z_clear, **park_kw)
    pause_tpl = patch_holder_templates(read(tpl_dir / "page_pause.gcode"), z_clear, **park_kw)

    if not pause_beep:
        pause_tpl = re.sub(
            r"; --- page-flip alert beep.*?; --- end beep ---\n",
            "", pause_tpl, flags=re.S,
        )
        end = re.sub(
            r"; --- print-complete fanfare.*?; --- end fanfare ---\n",
            "", end, flags=re.S,
        )

    order_label = "spread (unfolded signature)" if args.page_order == "spread" else "sequential"
    start_label = f" from page {args.start_page}" if args.start_page > 1 else ""
    print(
        f"Merging {n} pages{start_label} -> {out_path} "
        f"(order={order_label}, holder={profile.name}, park=({PARK_NOZZLE_X_MM:.0f},"
        f"{PARK_NOZZLE_Y_MM:.0f}) Z={z_clear:.1f}, "
        f"~{mpp} min/page, est {total_min} min total)"
    )

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
            page_num = page_num_from_path(page)
            out.write(f";===== PAGE {page_num:02d} =====\n")
            out.write(layer_marker(i))
            page_progress = int((i - 1) / n * 100)
            page_remaining = (n - i + 1) * mpp
            out.write(f"M73 P{page_progress} R{page_remaining}\n")
            out.write(page.read_text())

            if i < n:
                next_page_num = page_num_from_path(pages[i])
                next_progress = int(i / n * 100)
                next_remaining = (n - i) * mpp
                pause = pause_tpl
                pause = pause.replace("{NEXT_PAGE}", f"{next_page_num:02d}")
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
