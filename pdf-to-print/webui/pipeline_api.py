"""Web UI ↔ pipeline scripts: validated settings → command lines, file listing, safe paths.

Everything the UI can run is a fixed script with an argument list built here (no shell); every
number is range-checked with the same limits the scripts enforce, so the UI shows the error
before a build starts.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PDF_DIR, OUTPUT, BUILD = ROOT / "pdfs", ROOT / "output", ROOT / "build"
sys.path.insert(0, str(SCRIPTS))
from holder_config import (DRAW_ACCEL_RANGE_MM_S2, DRAW_SPEED_RANGE_MM_S,  # noqa: E402
                           Z_HOP_RANGE_MM)
from page_order import SPREAD_ORDER_24  # noqa: E402

MAX_PDF_BYTES = 50 * 1024 * 1024
SAFE_NAME = re.compile(r"^[\w.\-]{1,120}$", re.UNICODE)
SHEETS = ("zbench", "hop-ladder", "speed", "page", "edge-check")
SHEET_OUTPUT = {"zbench": "test_zbench.gcode", "hop-ladder": "test_hop_ladder.gcode",
                "speed": "test_speed.gcode", "edge-check": "test_edge_check.gcode"}
TIME_FACTOR_RANGE = (0.5, 5.0)
L_STOP_RANGE = (0.5, 40.0)


def python() -> str:
    venv = ROOT / ".venv" / "bin" / "python3"
    return str(venv) if venv.exists() else sys.executable


def _num(data: dict, key: str, bounds: tuple[float, float], label: str) -> float | None:
    raw = data.get(key)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: «{raw}» — не число") from exc
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{label}: {value:g} вне диапазона {bounds[0]:g}…{bounds[1]:g}")
    return value


def safe_name(name: str, suffix: str) -> str:
    base = Path(str(name)).name
    if not SAFE_NAME.match(base) or not base.lower().endswith(suffix):
        raise ValueError(f"недопустимое имя файла: {name!r}")
    return base


@dataclass(frozen=True)
class Settings:
    pdf: str
    holder: str = "soft"
    page_order: str = "sequential"
    pressure: bool = False
    feedrate: bool = False
    strikethrough: bool = False
    z_hop: float | None = None
    draw_speed: float | None = None
    draw_accel: float | None = None
    time_factor: float | None = None
    start_page: int = 1
    l_stop_height: float | None = None
    reference: str = ""

    @property
    def stem(self) -> str:
        return Path(self.pdf).stem

    @property
    def experimental(self) -> bool:
        return self.pressure or self.feedrate or self.strikethrough

    @property
    def output_name(self) -> str:
        suffix = "_experimental" if self.experimental else ""
        start = f"_from{self.start_page}" if self.start_page > 1 else ""
        return f"{self.stem}{suffix}{start}.gcode"


def parse_settings(data: dict) -> Settings:
    pdf = safe_name(data.get("pdf", ""), ".pdf")
    if not (PDF_DIR / pdf).is_file():
        raise ValueError(f"нет файла pdfs/{pdf}")
    holder = data.get("holder", "soft")
    order = data.get("page_order", "sequential")
    if holder not in ("soft", "umts"):
        raise ValueError("держатель: soft или umts")
    if order not in ("sequential", "spread"):
        raise ValueError("порядок страниц: sequential или spread")
    start = int(_num(data, "start_page", (1, 999), "начать со страницы") or 1)
    reference = data.get("reference") or ""
    if reference:
        reference = safe_name(reference, ".gcode")
        if not (OUTPUT / reference).is_file():
            raise ValueError(f"нет эталонного файла output/{reference}")
    return Settings(
        pdf=pdf, holder=holder, page_order=order, pressure=bool(data.get("pressure")),
        feedrate=bool(data.get("feedrate")), strikethrough=bool(data.get("strikethrough")),
        z_hop=_num(data, "z_hop", Z_HOP_RANGE_MM, "подъём пера, мм"),
        draw_speed=_num(data, "draw_speed", DRAW_SPEED_RANGE_MM_S, "скорость пера, мм/с"),
        draw_accel=_num(data, "draw_accel", DRAW_ACCEL_RANGE_MM_S2, "ускорение, мм/с²"),
        time_factor=_num(data, "time_factor", TIME_FACTOR_RANGE, "поправка времени"),
        start_page=start,
        l_stop_height=_num(data, "l_stop_height", L_STOP_RANGE, "высота L-упора, мм"),
        reference=reference)


def motion_env(s: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PDF_TO_PRINT_")}
    pairs = {"PDF_TO_PRINT_Z_HOP_MM": s.z_hop, "PDF_TO_PRINT_DRAW_SPEED_MM_S": s.draw_speed,
             "PDF_TO_PRINT_DRAW_ACCEL_MM_S2": s.draw_accel,
             "PDF_TO_PRINT_TIME_FACTOR": s.time_factor}
    env.update({k: f"{v:g}" for k, v in pairs.items() if v is not None})
    if s.holder == "soft":
        env["PDF_TO_PRINT_SOFT_HOLDER"] = "1"
    return env


def build_command(s: Settings) -> tuple[list[str], dict[str, str], Path]:
    env = motion_env(s)
    env["PDF_TO_PRINT_PAGE_ORDER"] = s.page_order
    env["START_PAGE"] = str(s.start_page)
    for flag, key in ((s.pressure, "VARIABLE_PRESSURE"), (s.feedrate, "VARIABLE_FEEDRATE"),
                      (s.strikethrough, "STRIKETHROUGH")):
        if flag:
            env[f"PDF_TO_PRINT_EXPERIMENTAL_{key}"] = "1"
    if s.l_stop_height is not None:
        env["PDF_TO_PRINT_SIM_L_STOP_HEIGHT"] = f"{s.l_stop_height:g}"
    if s.reference:
        env["PDF_TO_PRINT_SIM_REFERENCE"] = str(OUTPUT / s.reference)
    out = OUTPUT / s.output_name
    return ["bash", str(SCRIPTS / "build.sh"), str(PDF_DIR / s.pdf), str(out), "raster",
            s.page_order], env, out


def sheet_command(kind: str, s: Settings, page: int = 1) -> tuple[list[str], dict[str, str], Path]:
    if kind not in SHEETS:
        raise ValueError(f"неизвестный лист {kind!r}")
    gdir = BUILD / s.stem / "gcode"
    cmd = [python(), str(SCRIPTS / "calibration_sheets_gcode.py"), kind, "--holder", s.holder]
    if kind in ("speed", "page", "edge-check") and not gdir.is_dir():
        raise ValueError(f"сначала соберите {s.pdf}: нет {gdir.relative_to(ROOT)}")
    if kind == "speed":
        cmd += ["--source", str(gdir / "page_01.gcode")]
    elif kind == "page":
        cmd += ["--gcode", str(gdir / f"page_{page:02d}.gcode")]
    elif kind == "edge-check":
        cmd += ["--gcode-dir", str(gdir)]
    name = SHEET_OUTPUT.get(kind, f"test_page_{page:02d}.gcode")
    return cmd, motion_env(s), OUTPUT / name


def lstop_command(data: dict) -> tuple[list[str], Path]:
    height = _num(data, "height", (1.0, 20.0), "высота упора, мм") or 3.0
    md = _num(data, "magnet_d", (3.0, 25.0), "диаметр магнита, мм") or 10.0
    mh = _num(data, "magnet_h", (1.0, 10.0), "толщина магнита, мм") or 2.0
    out = OUTPUT / "l_stop.stl"
    return [python(), str(SCRIPTS / "l_stop_model.py"), "--height", f"{height:g}",
            "--magnet-d", f"{md:g}", "--magnet-h", f"{mh:g}", "--out", str(out)], out


def pdf_stem_of(gcode_name: str) -> str:
    return re.sub(r"(_experimental)?(_from\d+)?$", "", Path(gcode_name).stem)


def simulate_command(gcode_name: str, holder: str, l_stop_height: float | None = None,
                     reference: str = "") -> tuple[list[str], Path]:
    name = safe_name(gcode_name, ".gcode")
    stem = pdf_stem_of(name)
    report = OUTPUT / f"{Path(name).stem}.sim.md"
    cmd = [python(), str(SCRIPTS / "printer_sim.py"), str(OUTPUT / name), "--holder", holder,
           "--report", str(report)]
    if (BUILD / stem / "png").is_dir() and not name.startswith("test_"):
        cmd += ["--png-dir", str(BUILD / stem / "png"), "--render-dir", str(BUILD / stem / "sim")]
    if l_stop_height is not None:
        cmd += ["--l-stop-height", f"{l_stop_height:g}"]
    if reference:
        cmd += ["--reference", str(OUTPUT / safe_name(reference, ".gcode"))]
    return cmd, report


# ------------------------------------------------------------------ files

def clean_pdf_name(name: str) -> str:
    """User file name → safe pdfs/ name: letters (any script), digits, . _ - kept, rest → '_'."""
    stem = Path(str(name)).name
    stem = stem[:-4] if stem.lower().endswith(".pdf") else stem
    stem = re.sub(r"[^\w.\-]", "_", stem, flags=re.UNICODE).lstrip(".")[:110]
    if not stem:
        raise ValueError(f"недопустимое имя файла: {name!r}")
    return stem + ".pdf"


def save_pdf(name: str, data: bytes) -> str:
    base = clean_pdf_name(name)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF больше 50 МБ")
    if not data.startswith(b"%PDF"):
        raise ValueError("это не PDF")
    PDF_DIR.mkdir(exist_ok=True)
    (PDF_DIR / base).write_bytes(data)
    return base


def sim_status(gcode: Path) -> dict:
    report = gcode.with_name(gcode.stem + ".sim.md")
    if not report.is_file():
        return {"status": "нет проверки", "fresh": False}
    head = report.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
    status = "PASS" if head and head[0] == f"# Printer simulation: {gcode.name} — PASS" else "FAIL"
    holder = next((m.group(1) for m in (re.search(r"holder=(\w+)", ln) for ln in head) if m), "")
    return {"status": status, "holder": holder, "report": f"output/{report.name}",
            "fresh": report.stat().st_mtime >= gcode.stat().st_mtime}


def gcode_info(path: Path) -> dict:
    with path.open("r", errors="replace") as fh:
        head = fh.read(4096)
    m = re.search(r"total estimated time: (\d+)m", head)
    return {"name": path.name, "size": path.stat().st_size, "mtime": path.stat().st_mtime,
            "est_min": int(m.group(1)) if m else None, **sim_status(path)}


def list_files() -> dict:
    pdfs = sorted(p.name for p in PDF_DIR.glob("*.pdf")) if PDF_DIR.is_dir() else []
    gcodes = sorted(OUTPUT.glob("*.gcode"), key=lambda p: p.stat().st_mtime, reverse=True)
    others = sorted(p.name for p in OUTPUT.glob("*") if p.suffix in (".stl", ".png", ".md"))
    return {"pdfs": pdfs, "gcodes": [gcode_info(p) for p in gcodes], "others": others}


def previews(stem: str) -> list[str]:
    d = BUILD / Path(stem).name / "sim"
    return [f"build/{d.parent.name}/sim/{p.name}" for p in sorted(d.glob("page_*.png"))]


def _plain(name: str) -> str:
    if not SAFE_NAME.match(name) or name.startswith("."):
        raise ValueError(f"недопустимое имя: {name!r}")
    return name


def safe_file(rel: str) -> Path:
    """Downloadable files: output/<name> and build/<pdf>/sim/<page>.png only."""
    parts = rel.split("/")
    try:
        if len(parts) == 2 and parts[0] == "output":
            path = OUTPUT / _plain(parts[1])
        elif len(parts) == 4 and parts[0] == "build" and parts[2] == "sim" and parts[3].endswith(".png"):
            path = BUILD / _plain(parts[1]) / "sim" / _plain(parts[3])
        else:
            raise ValueError(rel)
    except ValueError as exc:
        raise ValueError(f"файл недоступен: {rel}") from exc
    if not path.is_file():
        raise ValueError(f"файл недоступен: {rel}")
    return path


def send_check(gcode_name: str, holder: str) -> str:
    """'' if the job may go to the printer, else the reason (Russian)."""
    path = OUTPUT / safe_name(gcode_name, ".gcode")
    if not path.is_file():
        return f"нет файла output/{gcode_name}"
    st = sim_status(path)
    if st["status"] != "PASS":
        return "симуляция не пройдена или не запускалась — сначала «Проверить на модели»"
    if not st["fresh"]:
        return "файл новее отчёта симуляции — проверьте ещё раз"
    want = "soft" if holder == "soft" else "umts"
    if st.get("holder") != want:
        return f"файл проверен для держателя «{st.get('holder') or '?'}», а выбран {want}"
    return ""


def page_order_of(pages: tuple[int, ...]) -> str:
    return "spread" if tuple(pages) == tuple(SPREAD_ORDER_24) else "sequential"
