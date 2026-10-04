"""Bambu P1S over LAN: status (MQTT), sending a job (FTPS + start command), camera snapshot.

Firmware 01.08.02+ in LAN-only mode pushes status to any client and accepts FTPS uploads, but
commands from third-party clients (start/pause/resume/stop) only with "Developer Mode" enabled
on the printer screen (detected from print.fun). Errors are surfaced, never swallowed.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import socket
import ssl
import struct
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ftps
from .mqtt_min import MqttClient, MqttConfig

PAGE_MARKER = re.compile(r"^;=+ PAGE (\d+) =+", re.M)
STAGES = {5: "пауза из G-code (M400)", 16: "пауза пользователя", 30: "пауза из G-code",
          6: "нет филамента", 17: "пауза: передняя крышка", 20: "пауза: ошибка температуры"}
GCODE_PAUSE_STAGES = {5, 30}
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}
MAX_CAMERA_FRAME = 5 * 1024 * 1024
SPREAD_SLOTS = ("левая, наружная", "левая, внутренняя", "правая, внутренняя", "правая, наружная")
COMMANDS = ("pause", "resume", "stop")


@dataclass(frozen=True)
class PrinterConfig:
    ip: str
    serial: str
    access_code: str
    mqtt_port: int = 8883
    ftp_port: int = 990
    camera_port: int = 6000
    use_tls: bool = True


@dataclass(frozen=True)
class JobMeta:
    """What was sent, so pauses can be explained ("put page 23 next")."""
    file: str
    remote: str
    pages: tuple[int, ...]
    page_order: str
    sent_at: float


def job_pages(gcode: str) -> tuple[int, ...]:
    """PDF page numbers in print order from ;===== PAGE NN ===== markers (pause templates repeat
    the next page's marker, so consecutive duplicates collapse)."""
    out: list[int] = []
    for m in PAGE_MARKER.finditer(gcode):
        n = int(m.group(1))
        if not out or out[-1] != n:
            out.append(n)
    return tuple(out)


def wrap_3mf(gcode: bytes) -> bytes:
    """Minimal project the firmware prints: Metadata/plate_1.gcode (+ its md5)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(gcode).hexdigest().upper())
    return buf.getvalue()


def project_file_payload(remote_name: str, seq: str, md5: str = "") -> dict:
    """Start a .3mf from the SD-card root (P1 series: file:///sdcard/<name>; md5 "" like
    ha-bambulab / bambu-printer-manager). No AMS, no calibrations: the job's own start
    G-code homes; there is no filament."""
    return {"print": {
        "sequence_id": seq, "command": "project_file", "param": "Metadata/plate_1.gcode",
        "url": f"file:///sdcard/{remote_name}", "file": remote_name, "md5": md5,
        "subtask_name": Path(remote_name).stem, "project_id": "0", "profile_id": "0",
        "task_id": "0", "subtask_id": "0", "bed_type": "auto", "bed_leveling": False,
        "flow_cali": False, "vibration_cali": False, "layer_inspect": False,
        "timelapse": False, "use_ams": False, "ams_mapping": ""}}


def gcode_file_payload(remote_path: str, seq: str) -> dict:
    """Plain .gcode by absolute SD path (OpenBambuAPI; less proven than project_file)."""
    return {"print": {"sequence_id": seq, "command": "gcode_file",
                      "param": f"/sdcard/{remote_path}"}}


DEV_MODE_SIGNATURE_BIT = 0x20000000   # print.fun: set = signed commands required (dev mode off)
VERIFY_FAILED = "mqtt message verify failed"


def developer_mode(state: dict) -> bool | None:
    """True/False from print.fun (ha-bambulab const.py); None if the printer did not say."""
    fun = state.get("fun")
    try:
        return not int(str(fun), 16) & DEV_MODE_SIGNATURE_BIT if fun not in (None, "") else None
    except ValueError:
        return None


def pause_guidance(state: dict, meta: JobMeta | None, pauses_seen: int) -> str:
    """Operator instruction for the current pause, Russian."""
    if state.get("gcode_state") != "PAUSE":
        return ""
    if state.get("stg_cur") not in GCODE_PAUSE_STAGES:
        return f"Пауза: {STAGES.get(state.get('stg_cur'), 'причина неизвестна')}"
    foreign = meta is not None and state.get("subtask_name") not in (None, "", Path(meta.remote).stem)
    if meta is None or foreign:
        return "Пауза из задания (страница не определена: задание отправлено не отсюда)"
    n = len(meta.pages)
    done = int(state.get("layer_num") or 0) or pauses_seen - 1
    if done <= 0:
        return "Установите держатель с ручкой, проверьте тетрадь у упора и нажмите «Продолжить»"
    if done >= n:
        return "Тетрадь готова: снимите держатель и нажмите «Стоп» (не «Продолжить»)"
    nxt = meta.pages[done]
    if meta.page_order == "spread" and n == 24:
        spread, slot = done // 4 + 1, SPREAD_SLOTS[done % 4]
        change = " — смените разворот" if done % 4 == 0 else ""
        return f"Положите страницу {nxt:02d}: разворот {spread}, {slot}{change}"
    return f"Перелистните на страницу {nxt:02d} ({done + 1} из {n})"


class PrinterLink:
    def __init__(self, on_update: Callable[[dict], None], *, pins: dict | None = None,
                 on_pins_changed: Callable[[dict], None] | None = None,
                 first_report_timeout: float = 8.0, reply_timeout: float = 5.0) -> None:
        self.on_update = on_update
        self.pins = pins if pins is not None else {}
        self.on_pins_changed = on_pins_changed
        self.first_report_timeout, self.reply_timeout = first_report_timeout, reply_timeout
        self.cfg: PrinterConfig | None = None
        self.client: MqttClient | None = None
        self.state: dict = {}
        self.meta: JobMeta | None = None
        self.pauses_seen = 0
        self.error = ""
        self._seq = int(time.time()) % 100000
        self._lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._first_report = threading.Event()
        self._waiters: dict[str, list] = {}

    # ---- TLS pinning (trust on first use)
    def _pin(self, sock: ssl.SSLSocket, port: int) -> None:
        key = f"{self.cfg.ip}:{port}"
        der = sock.getpeercert(binary_form=True) or b""
        fp = hashlib.sha256(der).hexdigest()
        known = self.pins.get(key)
        if known is None:
            self.pins[key] = fp
            if self.on_pins_changed:
                self.on_pins_changed(self.pins)
        elif known != fp:
            sock.close()
            raise ssl.SSLError(f"сертификат принтера {key} изменился (было {known[:12]}…, стало "
                               f"{fp[:12]}…): возможна подмена в сети. Если меняли принтер или "
                               f"прошивку — сбросьте закрепление в настройках")

    # ---- connection
    def connect(self, cfg: PrinterConfig) -> None:
        with self._conn_lock:
            self._disconnect()
            self.cfg, self.state, self.error = cfg, {}, ""
            self._first_report.clear()
            mcfg = MqttConfig(cfg.ip, cfg.mqtt_port, password=cfg.access_code, use_tls=cfg.use_tls,
                              tls_context=tls_context() if cfg.use_tls else None,
                              on_tls=lambda sock: self._pin(sock, cfg.mqtt_port))
            client = MqttClient(mcfg, self._on_message)
            client.connect(f"device/{cfg.serial}/report")
            self.client = client
            self._publish({"pushing": {"sequence_id": self._next_seq(), "command": "pushall",
                                       "version": 1, "push_target": 1}}, wait_reply=False)
            if not self._first_report.wait(self.first_report_timeout):
                self._disconnect()
                raise RuntimeError("принтер не прислал статус: проверьте серийный номер "
                                   "(экран принтера → Настройки → Устройство)")

    def disconnect(self) -> None:
        with self._conn_lock:
            self._disconnect()

    def _disconnect(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    @property
    def online(self) -> bool:
        return self.client is not None and self.client.alive

    # ---- status
    def _on_message(self, _topic: str, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        fields = msg.get("print") if isinstance(msg, dict) else None
        if not isinstance(fields, dict):
            return
        with self._lock:
            before = self.state.get("gcode_state")
            failed = str(fields.get("result", "")).lower() in ("fail", "failed") or fields.get("err_code")
            if failed:
                self.error = reject_reason(fields)
            waiter = self._waiters.get(str(fields.get("sequence_id")))
            if waiter is not None and ("result" in fields or failed):
                waiter[1] = fields
                waiter[0].set()
            self.state.update({k: v for k, v in fields.items()
                               if k not in ("command", "result", "reason", "sequence_id")})
            if before != "PAUSE" and self.state.get("gcode_state") == "PAUSE" \
                    and self.state.get("stg_cur") in GCODE_PAUSE_STAGES:
                self.pauses_seen += 1
        if "gcode_state" in self.state:
            self._first_report.set()
        self.on_update(self.summary())

    def summary(self) -> dict:
        s = self.state
        stage = s.get("stg_cur")
        return {
            "online": self.online, "configured": self.cfg is not None, "error": self.error,
            "state": s.get("gcode_state", ""), "percent": s.get("mc_percent"),
            "remaining_min": s.get("mc_remaining_time"), "layer": s.get("layer_num"),
            "total_layers": s.get("total_layer_num"), "stage": STAGES.get(stage, stage),
            "print_error": s.get("print_error") or 0, "hms": len(s.get("hms") or []),
            "job": s.get("subtask_name", ""), "nozzle": s.get("nozzle_temper"),
            "bed": s.get("bed_temper"),
            "guidance": pause_guidance(s, self.meta, self.pauses_seen),
            "sent_job": self.meta.file if self.meta else "",
            "developer_mode": developer_mode(s),
        }

    # ---- commands
    def command(self, name: str) -> bool:
        """Send pause/resume/stop; True if the printer confirmed, False if it stayed silent.
        Raises RuntimeError if the printer rejected it."""
        if name not in COMMANDS:
            raise ValueError(f"unknown command {name!r}")
        reply = self._publish({"print": {"sequence_id": self._next_seq(), "command": name, "param": ""}})
        return reply is not None

    def send_job(self, gcode_path: Path, method: str, page_order: str,
                 progress: Callable[[int, int], None] | None = None) -> JobMeta:
        """Upload `gcode_path` and (unless method == 'upload') start it."""
        if self.cfg is None or not self.online:
            raise RuntimeError("принтер не подключён")
        state = self.state.get("gcode_state")
        if state not in IDLE_STATES:
            raise RuntimeError(f"принтер занят или статус ещё не получен ({state or 'нет данных'}): "
                               "дождитесь IDLE/FINISH")
        data = gcode_path.read_bytes()
        if method in ("3mf", "upload"):
            remote, blob = gcode_path.stem + ".3mf", wrap_3mf(data)
        elif method == "gcode":
            remote, blob = f"cache/{gcode_path.name}", data
        else:
            raise ValueError(f"unknown method {method!r}")
        total = len(blob)
        ftps.upload(self.cfg.ip, self.cfg.access_code, io.BytesIO(blob), remote,
                    port=self.cfg.ftp_port, use_tls=self.cfg.use_tls,
                    progress=(lambda sent: progress(sent, total)) if progress else None,
                    on_tls=lambda sock: self._pin(sock, self.cfg.ftp_port))
        meta = JobMeta(gcode_path.name, remote, job_pages(data.decode(errors="replace")),
                       page_order, time.time())
        self.meta, self.pauses_seen, self.error = meta, 0, ""
        if method == "3mf":
            self._publish(project_file_payload(remote, self._next_seq()))
        elif method == "gcode":
            self._publish(gcode_file_payload(remote, self._next_seq()))
        return meta

    def camera_jpeg(self, timeout: float = 10.0) -> bytes:
        """One frame from the chamber camera (TLS on 6000, 80-byte auth packet)."""
        if self.cfg is None:
            raise RuntimeError("принтер не настроен")
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") \
            + self.cfg.access_code.encode().ljust(32, b"\0")
        deadline = time.monotonic() + timeout
        with socket.create_connection((self.cfg.ip, self.cfg.camera_port), timeout=timeout) as raw:
            with tls_context().wrap_socket(raw, server_hostname=self.cfg.ip) as sock:
                self._pin(sock, self.cfg.camera_port)
                sock.sendall(auth)
                frame = b""
                for _ in range(2):                      # first frame after connect may be stale
                    if time.monotonic() > deadline:
                        break
                    size = int.from_bytes(_recv(sock, 16)[:4], "little")
                    if not 0 < size <= MAX_CAMERA_FRAME:
                        raise RuntimeError(f"камера: неверный размер кадра {size}")
                    frame = _recv(sock, size)
                if not frame.startswith(b"\xff\xd8"):
                    raise RuntimeError("камера вернула не JPEG")
                return frame

    def _publish(self, msg: dict, wait_reply: bool = True) -> dict | None:
        """Publish; wait for the printer's reply with the same sequence_id (None if silent).
        Raises RuntimeError when the printer answers with a failure."""
        if self.client is None or self.cfg is None:
            raise RuntimeError("принтер не подключён")
        body = msg.get("print") or msg.get("pushing") or {}
        seq = str(body.get("sequence_id"))
        waiter = [threading.Event(), None]
        if wait_reply:
            self._waiters[seq] = waiter
        try:
            self.client.publish(f"device/{self.cfg.serial}/request", json.dumps(msg).encode())
            if not wait_reply or not waiter[0].wait(self.reply_timeout):
                return None
        finally:
            self._waiters.pop(seq, None)
        reply = waiter[1]
        if str(reply.get("result", "")).lower() in ("fail", "failed") or reply.get("err_code"):
            raise RuntimeError(reject_reason(reply))
        return reply

    def _next_seq(self) -> str:
        with self._lock:
            self._seq += 1
            return str(self._seq)


def reject_reason(fields: dict) -> str:
    reason = str(fields.get("reason") or fields.get("err_code") or "")
    if VERIFY_FAILED in reason or str(fields.get("err_code")) in ("84033543", "0x05024007"):
        return ("принтер отклонил команду: включите на экране принтера LAN Only + Developer Mode "
                "(или используйте «Только загрузить» и запуск с экрана)")
    return f"принтер отклонил «{fields.get('command')}»: {reason}"


def tls_context() -> ssl.SSLContext:
    """Printer: self-signed LAN cert; TLS 1.2 like ha-bambulab (FTPS session reuse)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _recv(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("camera stream closed")
        buf += chunk
    return bytes(buf)
