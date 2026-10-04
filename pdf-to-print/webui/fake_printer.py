"""Fake Bambu P1S for tests and the web UI demo mode (no hardware needed).

FakeBroker  — plain-TCP MQTT broker speaking the subset the printer uses: checks user/access
              code, answers `pushall`, `pause`/`resume`/`stop`, `project_file`/`gcode_file`.
FakeFtp     — plain FTP server keeping uploaded files in memory (USER/PASS/TYPE/PBSZ/PROT/
              PASV/STOR/SIZE/QUIT).
FakePrinter — runs an uploaded job: splits the G-code at `M400 U1`, spends each segment's
              planner-model time × `time_scale`, pauses like the firmware (gcode_state PAUSE,
              layer_num from `M73 L<n>`), and reports progress on `device/<serial>/report`.

The real printer uses TLS on 8883 / implicit TLS on 990; the fakes are plain TCP on random
ports, the clients take `use_tls=False` for them.
"""

from __future__ import annotations

import io
import json
import re
import socket
import ssl
import struct
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .mqtt_min import (CONNACK, CONNECT, PINGREQ, PINGRESP, PUBACK, PUBLISH, SUBACK, SUBSCRIBE,
                       packet, parse_publish, publish_packet, read_packet)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from plot_time_sim import estimate_lines  # noqa: E402

LAYER_RE = re.compile(r"^M73 L(\d+)", re.M)


def _server_ctx(tls: tuple[Path, Path] | None) -> ssl.SSLContext | None:
    if tls is None:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tls[0]), str(tls[1]))
    return ctx


def _listener() -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s


# ------------------------------------------------------------------ FTP

class FakeFtp:
    def __init__(self, user: str, password: str, ctx: ssl.SSLContext | None = None) -> None:
        self.user, self.password, self.ctx = user, password, ctx
        self.files: dict[str, bytes] = {}
        self.sock = _listener()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        if self.ctx is not None:                     # implicit TLS like the printer's port 990
            try:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                conn.close()
                return
        prot_p = False
        f = conn.makefile("rwb", buffering=0)

        def reply(text: str) -> None:
            f.write((text + "\r\n").encode())

        reply("220 fake bambu ftp")
        data_srv: socket.socket | None = None
        authed = False
        for raw in f:
            cmd, _, arg = raw.decode().strip().partition(" ")
            cmd = cmd.upper()
            if cmd == "USER":
                reply("331 password please")
            elif cmd == "PASS":
                authed = arg == self.password
                reply("230 logged in" if authed else "530 login incorrect")
            elif not authed:
                reply("530 not logged in")
            elif cmd in ("TYPE", "PBSZ", "PROT", "OPTS"):
                prot_p = prot_p or (cmd == "PROT" and arg.upper() == "P")
                reply("200 ok")
            elif cmd == "PASV":
                data_srv = _listener()
                p = data_srv.getsockname()[1]
                reply(f"227 Entering Passive Mode (127,0,0,1,{p // 256},{p % 256})")
            elif cmd == "STOR" and data_srv is not None:
                reply("150 ok to send")
                dconn, _ = data_srv.accept()
                if prot_p and self.ctx is not None:
                    dconn = self.ctx.wrap_socket(dconn, server_side=True)
                chunks = []
                while True:
                    try:
                        chunk = dconn.recv(65536)
                    except ssl.SSLError:             # client closes without close_notify (Bambu style)
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                dconn.close()
                data_srv.close()
                self.files[arg.lstrip("/")] = b"".join(chunks)
                reply("226 transfer complete")
            elif cmd == "SIZE":
                name = arg.lstrip("/")
                reply(f"213 {len(self.files[name])}" if name in self.files else "550 no file")
            elif cmd == "QUIT":
                reply("221 bye")
                break
            else:
                reply("502 not implemented")
        conn.close()


# ------------------------------------------------------------------ printer state machine

@dataclass
class _Job:
    name: str
    segments: list[tuple[float, int]]      # (seconds, last M73 L in segment)
    index: int = 0
    elapsed: float = 0.0
    total: float = 0.0


@dataclass
class FakePrinterState:
    gcode_state: str = "IDLE"
    mc_percent: int = 0
    mc_remaining_time: int = 0
    layer_num: int = 0
    total_layer_num: int = 0
    stg_cur: int = 0
    print_error: int = 0
    subtask_name: str = ""
    gcode_file: str = ""
    nozzle_temper: float = 25.0
    fun: str = "3EC18FFF9CFF"              # developer mode on (signature bit clear)
    bed_temper: float = 24.0
    hms: list = field(default_factory=list)


def job_segments(gcode: str) -> list[tuple[float, int]]:
    """Split at executable `M400 U1`; seconds per segment from the planner model."""
    parts = re.split(r"^M400 U1.*$", gcode, flags=re.M)
    out, layer = [], 0
    for part in parts:
        layers = [int(v) for v in LAYER_RE.findall(part)]
        layer = layers[-1] if layers else layer
        out.append((estimate_lines(["M204 S5000"] + part.splitlines()).total, layer))
    return out


def gcode_from_upload(name: str, data: bytes) -> str:
    if name.endswith(".3mf"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            plate = next(n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
            return z.read(plate).decode()
    return data.decode()


class FakePrinter:
    """Broker + FTP + job runner. `time_scale` multiplies model time (0 = instant)."""

    def __init__(self, serial: str = "01P00TEST000001", access_code: str = "12345678",
                 time_scale: float = 0.0, reject_commands: bool = False,
                 tls: tuple[Path, Path] | None = None) -> None:
        self.serial, self.access_code, self.time_scale = serial, access_code, time_scale
        self.reject_commands = reject_commands
        self.ctx = _server_ctx(tls)
        self.state = FakePrinterState()
        if reject_commands:
            self.state.fun = "3EC1AFFF9CFF"          # signature required (developer mode off)
        self.ftp = FakeFtp("bblp", access_code, self.ctx)
        self.sock = _listener()
        self.port = self.sock.getsockname()[1]
        self.commands: list[dict] = []
        self._clients: list[socket.socket] = []
        self._lock = threading.RLock()
        self._job: _Job | None = None
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._tick, daemon=True).start()

    # ---- MQTT side
    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        try:
            if self.ctx is not None:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            first, body = read_packet(conn)
            if first != CONNECT or not self._auth_ok(body):
                conn.sendall(packet(CONNACK, b"\x00\x04"))
                conn.close()
                return
            conn.sendall(packet(CONNACK, b"\x00\x00"))
            with self._lock:
                self._clients.append(conn)
            while True:
                first, body = read_packet(conn)
                kind = first & 0xF0
                if kind == SUBSCRIBE & 0xF0:
                    conn.sendall(packet(SUBACK, body[:2] + b"\x00"))
                elif kind == PINGREQ:
                    conn.sendall(packet(PINGRESP, b""))
                elif kind == PUBLISH:
                    topic, payload, qos, pid = parse_publish(first, body)
                    if qos == 1:
                        conn.sendall(packet(PUBACK, struct.pack("!H", pid)))
                    if topic == f"device/{self.serial}/request":
                        self._command(json.loads(payload))
        except (ConnectionError, OSError, ValueError, ssl.SSLError):
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)

    def _auth_ok(self, body: bytes) -> bool:
        pos = 2 + 4 + 1 + 1 + 2                          # "MQTT", level, flags, keepalive
        fields = []
        for _ in range(3):                               # client id, user, password
            n = int.from_bytes(body[pos:pos + 2], "big")
            fields.append(body[pos + 2:pos + 2 + n].decode())
            pos += 2 + n
        return fields[1] == "bblp" and fields[2] == self.access_code

    def _report(self, fields: dict) -> None:
        self._report_raw(json.dumps({"print": fields}).encode())

    def _report_raw(self, payload: bytes) -> None:
        msg = publish_packet(f"device/{self.serial}/report", payload)
        with self._lock:
            for c in list(self._clients):
                try:
                    c.sendall(msg)
                except OSError:
                    self._clients.remove(c)

    def _full(self) -> dict:
        s = self.state
        return {"command": "push_status", **{k: getattr(s, k) for k in s.__dataclass_fields__}}

    # ---- commands
    def _command(self, msg: dict) -> None:
        if "pushing" in msg:
            self._report(self._full())
            return
        cmd = msg.get("print", {})
        self.commands.append(cmd)
        name = cmd.get("command")
        if self.reject_commands:
            self._report({"command": name, "result": "failed", "reason": "mqtt message verify failed",
                          "err_code": 84033543, "sequence_id": cmd.get("sequence_id")})
            return
        with self._lock:
            if name in ("project_file", "gcode_file"):
                self._start(cmd)
            elif name == "pause" and self.state.gcode_state == "RUNNING":
                self._set(gcode_state="PAUSE", stg_cur=16)
            elif name == "resume" and self.state.gcode_state == "PAUSE":
                if self._job is not None and self.state.stg_cur == 5:
                    self._job.index += 1
                self._set(gcode_state="RUNNING", stg_cur=0)
            elif name == "stop":
                self._job = None
                self._set(gcode_state="FAILED", stg_cur=0)
        self._report({"command": name, "result": "SUCCESS", "sequence_id": cmd.get("sequence_id")})

    def _start(self, cmd: dict) -> None:
        name = cmd.get("url", "").split("/")[-1] if cmd["command"] == "project_file" else cmd["param"]
        name = name.lstrip("/").removeprefix("sdcard/").lstrip("/")
        data = self.ftp.files.get(name) or self.ftp.files.get(f"cache/{Path(name).name}")
        if data is None:
            self._set(gcode_state="FAILED", print_error=0x0500C010)
            return
        segments = job_segments(gcode_from_upload(name, data))
        self._job = _Job(name, segments, total=sum(s for s, _ in segments))
        layers = max((layer for _, layer in segments), default=0)
        self._set(gcode_state="RUNNING", subtask_name=Path(name).stem, gcode_file=name,
                  total_layer_num=layers, layer_num=0, mc_percent=0, print_error=0, stg_cur=0)

    def _set(self, **fields) -> None:
        for k, v in fields.items():
            setattr(self.state, k, v)
        self._report(fields)

    # ---- time
    def _tick(self) -> None:
        while True:
            time.sleep(0.05)
            with self._lock:
                job = self._job
                if job is None or self.state.gcode_state != "RUNNING":
                    continue
                seconds, layer = job.segments[job.index]
                done_before = sum(s for s, _ in job.segments[:job.index])
                job.elapsed += 0.05 / self.time_scale if self.time_scale > 0 else seconds
                if job.elapsed - done_before < seconds:
                    self._progress(job, job.elapsed)
                    continue
                job.elapsed = done_before + seconds
                self._progress(job, job.elapsed, layer)
                if job.index < len(job.segments) - 1:
                    self._set(gcode_state="PAUSE", stg_cur=5)      # M400 U1
                else:
                    self._job = None
                    self._set(gcode_state="FINISH", mc_percent=100, mc_remaining_time=0)

    def _progress(self, job: _Job, elapsed: float, layer: int | None = None) -> None:
        pct = int(100 * elapsed / job.total) if job.total else 100
        fields = {"mc_percent": pct, "mc_remaining_time": int((job.total - elapsed) / 60)}
        if layer is not None:
            fields["layer_num"] = layer
        if any(getattr(self.state, k) != v for k, v in fields.items()):
            self._set(**fields)

    def close(self) -> None:
        self.sock.close()
        self.ftp.sock.close()
