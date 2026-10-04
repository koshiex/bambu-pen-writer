"""Local web UI for the pen-plotter pipeline (stdlib HTTP server + SSE).

Security model (the server can start builds and drive a printer):
  * binds to 127.0.0.1 by default; requests whose Host header is not this server are refused
    (DNS-rebinding);
  * a random token is generated per start, embedded in the page and required on every /api
    and /files request (header X-Token or ?token=);
  * downloads only from output/ and build/<pdf>/sim/*.png; uploads only *.pdf ≤ 50 MB;
  * the printer access code is stored in ~/.config/pdf-to-print/printer.json (mode 600) and is
    never sent back to the browser.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import queue
import secrets
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import pipeline_api as api
from .jobs import JobRunner
from . import motion
from .printer_link import PrinterConfig, PrinterLink, job_pages

STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "viewer.js": "text/javascript", "style.css": "text/css"}
MAX_JSON = 64 * 1024
HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})*$")
PAGE_CSP = ("default-src 'self'; img-src 'self' data:; connect-src 'self'; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
FILE_CSP = "sandbox; default-src 'none'; img-src 'self'; frame-ancestors 'none'"
INLINE_TYPES = ("image/png", "image/jpeg")


class Broadcaster:
    def __init__(self) -> None:
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, event: str, data: dict) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:                 # slow client: drop backlog, tell it to re-sync
                    with q.mutex:
                        q.queue.clear()
                    q.put_nowait("event: reset\ndata: {}\n\n")


class ConfigStore:
    def __init__(self, home: Path) -> None:
        self.path = home / "printer.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        """Atomic write; the temp file is 0600 before any secret is written."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)


class App:
    def __init__(self, home: Path, demo=None) -> None:
        self.token = secrets.token_urlsafe(24)
        self.events = Broadcaster()
        self.runner = JobRunner(self.events.publish, api.ROOT)
        self.store = ConfigStore(home)
        self.link = PrinterLink(self._printer_update, pins=self.store.load().get("pins", {}),
                                on_pins_changed=self._save_pins)
        self.demo = demo
        self._last_printer: dict = {}

    def _save_pins(self, pins: dict) -> None:
        if self.demo is None:
            self.store.save({**self.store.load(), "pins": dict(pins)})

    def _printer_update(self, summary: dict) -> None:
        if summary != self._last_printer:
            self._last_printer = summary
            self.events.publish("printer", summary)

    def printer_config(self) -> PrinterConfig | None:
        if self.demo is not None:
            return PrinterConfig("127.0.0.1", self.demo.serial, self.demo.access_code,
                                 mqtt_port=self.demo.port, ftp_port=self.demo.ftp.port,
                                 use_tls=False)
        c = self.store.load()
        if not all(c.get(k) for k in ("ip", "serial", "access_code")):
            return None
        return PrinterConfig(c["ip"], c["serial"], c["access_code"])

    def state(self) -> dict:
        c = self.store.load()
        return {"files": api.list_files(), "runner": self.runner.snapshot(),
                "printer": self.link.summary(), "demo": self.demo is not None,
                "printer_config": {"ip": c.get("ip", ""), "serial": c.get("serial", ""),
                                   "has_code": bool(c.get("access_code"))}}

    def files_changed(self, _rc: int = 0) -> dict:
        self.events.publish("files", api.list_files())
        return {}


# ------------------------------------------------------------------ actions (POST)

class Conflict(ValueError):
    """409: the action would overwrite an existing file; resend with overwrite=true."""

    def __init__(self, message: str, name: str) -> None:
        super().__init__(message)
        self.name = name


def act_build(app: App, data: dict) -> dict:
    s = api.parse_settings(data)
    cmd, env, out = api.build_command(s)
    if out.exists() and not data.get("overwrite"):
        raise Conflict(f"output/{out.name} уже есть — перезаписать?", out.name)
    job = app.runner.run_command("build", f"Сборка {s.pdf} → {out.name}", cmd, env, app.files_changed)
    return {"job": asdict(job), "output": out.name}


def act_sheet(app: App, data: dict) -> dict:
    s = api.parse_settings(data)
    cmd, env, out = api.sheet_command(data.get("kind", ""), s, int(data.get("page") or 1))
    job = app.runner.run_command("sheet", f"Лист {data.get('kind')} → {out.name}", cmd, env,
                                 app.files_changed)
    return {"job": asdict(job), "output": out.name}


def act_lstop(app: App, data: dict) -> dict:
    cmd, out = api.lstop_command(data)
    job = app.runner.run_command("lstop", "L-упор → l_stop.stl", cmd, dict(os.environ),
                                 app.files_changed)
    return {"job": asdict(job), "output": out.name}


def act_simulate(app: App, data: dict) -> dict:
    holder = data.get("holder", "soft")
    if holder not in ("soft", "umts"):
        raise ValueError("держатель: soft или umts")
    lstop = api._num(data, "l_stop_height", api.L_STOP_RANGE, "высота L-упора, мм")
    cmd, report = api.simulate_command(data.get("file", ""), holder, lstop, data.get("reference", ""))
    job = app.runner.run_command("simulate", f"Проверка {data.get('file')} на модели", cmd,
                                 dict(os.environ), app.files_changed)
    return {"job": asdict(job), "report": report.name}


def _valid_host(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return bool(HOSTNAME.match(ip)) and len(ip) <= 253


def act_printer_config(app: App, data: dict) -> dict:
    current = app.store.load()
    ip, serial = str(data.get("ip", "")).strip(), str(data.get("serial", "")).strip()
    new_code = str(data.get("access_code", "")).strip()
    same_printer = ip == current.get("ip") and serial == current.get("serial")
    code = new_code or (current.get("access_code", "") if same_printer else "")
    if not _valid_host(ip):
        raise ValueError("IP принтера: адрес вида 192.168.1.50")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", serial):
        raise ValueError("серийный номер: буквы и цифры с экрана принтера")
    if not code:
        raise ValueError("введите access code (для нового IP/серийного номера — заново)")
    if len(code) != 8 or not code.isalnum():
        raise ValueError("access code — 8 символов с экрана принтера")
    pins = current.get("pins", {}) if same_printer else {}
    app.store.save({"ip": ip, "serial": serial, "access_code": code, "pins": pins})
    app.link.pins.clear()
    app.link.pins.update(pins)
    return {"saved": True}


def act_printer_connect(app: App, _data: dict) -> dict:
    cfg = app.printer_config()
    if cfg is None:
        raise ValueError("сначала сохраните настройки принтера")
    app.link.connect(cfg)
    return app.link.summary()


def act_printer_command(app: App, data: dict) -> dict:
    app.link.command(str(data.get("name", "")))
    return {"sent": data.get("name")}


def act_printer_send(app: App, data: dict) -> dict:
    name = api.safe_name(data.get("file", ""), ".gcode")
    method = data.get("method", "3mf")
    if method not in ("3mf", "gcode", "upload"):
        raise ValueError("способ: 3mf, gcode или upload")
    reason = api.send_check(name, data.get("holder", "soft"))
    if reason and not data.get("force"):
        raise ValueError(f"отправка заблокирована: {reason}")
    path = api.OUTPUT / name
    order = api.page_order_of(job_pages(path.read_text(errors="replace")))

    def task(log) -> dict:
        marks = set()

        def progress(sent: int, total: int) -> None:
            step = int(10 * sent / total)
            if step not in marks:
                marks.add(step)
                log(f"загрузка {name}: {10 * step}% ({sent // 1024} из {total // 1024} КБ)")

        log(f"отправка {name} ({method}, порядок страниц {order})")
        meta = app.link.send_job(path, method, order, progress)
        log("файл на принтере: " + meta.remote)
        log("команда запуска отправлена" if method != "upload" else
            "загружено без запуска: выберите файл на экране принтера")
        return {"remote": meta.remote, "pages": list(meta.pages)}

    job = app.runner.run_callable("send", f"Отправка {name} на принтер", task)
    return {"job": asdict(job)}


_overviews: dict[tuple[str, float, str], dict] = {}


def motion_api(q: dict) -> dict:
    """Overview (?file=) or one page (?file=&page=N) of the motion preview."""
    name = api.safe_name(q.get("file", ""), ".gcode")
    path = api.OUTPUT / name
    if not path.is_file():
        raise ValueError(f"нет файла output/{name}")
    holder = api.sim_status(path).get("holder") or q.get("holder", "soft")
    holder = "soft" if holder == "soft" else "umts"
    if "page" in q:
        return motion.page_motion(path, int(q["page"]), holder)
    key = (str(path), path.stat().st_mtime, holder)
    if key not in _overviews:
        _overviews.clear()
        _overviews[key] = motion.job_overview(path, holder)
    return _overviews[key]


ACTIONS = {
    "/api/build": act_build, "/api/sheet": act_sheet, "/api/lstop": act_lstop,
    "/api/simulate": act_simulate, "/api/printer/config": act_printer_config,
    "/api/printer/connect": act_printer_connect, "/api/printer/command": act_printer_command,
    "/api/printer/send": act_printer_send,
    "/api/printer/disconnect": lambda app, _d: (app.link.disconnect(), app.link.summary())[1],
    "/api/job/cancel": lambda app, _d: {"cancelled": app.runner.cancel()},
}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "pdf-to-print-webui"

    def log_message(self, fmt: str, *args) -> None:  # quiet; errors go to the UI
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if not (extra or {}).get("Content-Security-Policy"):
            self.send_header("Content-Security-Policy", PAGE_CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json(403, {"error": "unexpected Host header"})
            return False
        path = urlparse(self.path).path
        if path.startswith(("/api/", "/files/")):
            token = self.headers.get("X-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if not secrets.compare_digest(token, self.app.token):
                self._json(401, {"error": "bad token"})
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            self._route_get(url.path, q)
        except (ValueError, RuntimeError, OSError) as exc:
            self._json(400, {"error": str(exc)})

    def _route_get(self, path: str, q: dict) -> None:
        if path == "/":
            html = (STATIC / "index.html").read_text().replace("{{TOKEN}}", self.app.token)
            self._send(200, html.encode(), "text/html; charset=utf-8")
        elif path.startswith("/static/") and path[8:] in STATIC_FILES:
            self._send(200, (STATIC / path[8:]).read_bytes(), STATIC_FILES[path[8:]])
        elif path == "/api/state":
            self._json(200, self.app.state())
        elif path == "/api/events":
            self._sse()
        elif path == "/api/report":
            name = api.safe_name(q.get("file", ""), ".gcode")
            report = api.OUTPUT / f"{Path(name).stem}.sim.md"
            self._json(200, {"text": report.read_text() if report.is_file() else ""})
        elif path == "/api/motion":
            self._json(200, motion_api(q))
        elif path == "/api/previews":
            self._json(200, {"images": api.previews(api.pdf_stem_of(q.get("file", "")))})
        elif path == "/api/printer/camera":
            self._send(200, self.app.link.camera_jpeg(), "image/jpeg")
        elif path.startswith("/files/"):
            f = api.safe_file(path[len("/files/"):])
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            extra = {"Content-Security-Policy": FILE_CSP}
            if ctype not in INLINE_TYPES:
                ctype = "application/octet-stream"
                extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote_name(f.name)
            self._send(200, f.read_bytes(), ctype, extra)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("bad Content-Length")
            if path == "/api/pdf":
                if length > api.MAX_PDF_BYTES:
                    raise ValueError("PDF больше 50 МБ")
                name = api.save_pdf(unquote(self.headers.get("X-Filename", "")), self.rfile.read(length))
                self.app.files_changed()
                self._json(200, {"pdf": name})
                return
            if path not in ACTIONS:
                self._json(404, {"error": "not found"})
                return
            if length > MAX_JSON:
                raise ValueError("слишком большой запрос")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("ожидался JSON-объект")
            self._json(200, ACTIONS[path](self.app, data))
        except Conflict as exc:
            self._json(409, {"error": str(exc), "exists": exc.name})
        except (ValueError, RuntimeError, OSError) as exc:
            self._json(400, {"error": str(exc)})

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = self.app.events.subscribe()
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": keep-alive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.events.unsubscribe(q)


def quote_name(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")


def serve(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd


def wait_forever() -> None:
    while True:
        time.sleep(3600)
