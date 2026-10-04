"""Minimal MQTT 3.1.1 client (stdlib only) — enough for the Bambu LAN broker.

Bambu printers in LAN mode run an MQTT broker on 8883 (TLS, self-signed cert, user `bblp`,
password = access code). We need CONNECT, SUBSCRIBE, PUBLISH (QoS 0/1) and keep-alive pings;
no paho in the environment, so the protocol is implemented here. Packet helpers are shared with
the fake broker used by tests and the demo printer.

Robustness rules: partial packets survive read timeouts (persistent buffer), a connection that
stays silent longer than 1.5 × keep-alive is treated as dead, and an exception while handling one
packet is recorded but never kills the reader.
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK = 0x10, 0x20, 0x30, 0x40, 0x82, 0x90
PINGREQ, PINGRESP, DISCONNECT = 0xC0, 0xD0, 0xE0
CONNACK_ERRORS = {1: "unacceptable protocol version", 2: "client id rejected",
                  3: "broker unavailable", 4: "bad user name or password (access code?)",
                  5: "not authorized"}
MAX_PACKET = 8 * 1024 * 1024


class MqttError(RuntimeError):
    pass


def encode_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_str(s: str | bytes) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def packet(first: int, body: bytes) -> bytes:
    return bytes([first]) + encode_length(len(body)) + body


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def read_packet(sock: socket.socket) -> tuple[int, bytes]:
    """Blocking read of one packet (fake broker / handshake)."""
    first = recv_exact(sock, 1)[0]
    mult, length = 1, 0
    for _ in range(4):
        b = recv_exact(sock, 1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    else:
        raise MqttError("malformed remaining length")
    return first, recv_exact(sock, length) if length else b""


def try_parse(buf: bytearray) -> tuple[int, bytes, int] | None:
    """(first byte, body, bytes consumed) if `buf` starts with a complete packet."""
    if len(buf) < 2:
        return None
    mult, length, i = 1, 0, 1
    while True:
        if i >= len(buf):
            return None
        b = buf[i]
        length += (b & 0x7F) * mult
        i += 1
        if not b & 0x80:
            break
        mult *= 128
        if i > 4:
            raise MqttError("malformed remaining length")
    if length > MAX_PACKET:
        raise MqttError(f"packet too large: {length}")
    if len(buf) < i + length:
        return None
    return buf[0], bytes(buf[i:i + length]), i + length


def connect_packet(client_id: str, user: str, password: str, keepalive: int) -> bytes:
    var = encode_str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", keepalive)
    return packet(CONNECT, var + encode_str(client_id) + encode_str(user) + encode_str(password))


def subscribe_packet(pid: int, topic: str) -> bytes:
    return packet(SUBSCRIBE, struct.pack("!H", pid) + encode_str(topic) + b"\x00")


def publish_packet(topic: str, payload: bytes, qos: int = 0, pid: int = 0) -> bytes:
    body = encode_str(topic) + (struct.pack("!H", pid) if qos else b"") + payload
    return packet(PUBLISH | (qos << 1), body)


def parse_publish(first: int, body: bytes) -> tuple[str, bytes, int, int]:
    """(topic, payload, qos, packet id)."""
    qos = (first >> 1) & 0x03
    tlen = struct.unpack_from("!H", body)[0]
    topic = body[2:2 + tlen].decode()
    pos, pid = 2 + tlen, 0
    if qos:
        pid = struct.unpack_from("!H", body, pos)[0]
        pos += 2
    return topic, body[pos:], qos, pid


@dataclass
class MqttConfig:
    host: str
    port: int = 8883
    user: str = "bblp"
    password: str = ""
    use_tls: bool = True
    keepalive: int = 60
    timeout: float = 10.0
    tls_context: ssl.SSLContext | None = None
    on_tls: Callable[[ssl.SSLSocket], None] | None = None     # certificate pinning hook


class MqttClient:
    """Reader thread; on_message(topic, payload) is called from that thread."""

    def __init__(self, cfg: MqttConfig, on_message: Callable[[str, bytes], None]) -> None:
        self.cfg = cfg
        self.on_message = on_message
        self.sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._pid = 0
        self._acks: dict[int, threading.Event] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buf = bytearray()
        self.last_rx = 0.0
        self.last_error = ""

    # ---- handshake
    def connect(self, subscribe: str) -> None:
        raw = socket.create_connection((self.cfg.host, self.cfg.port), timeout=self.cfg.timeout)
        try:
            sock = self._secure(raw)
            self.sock = sock
            client_id = f"pdf-to-print-{uuid.uuid4().hex[:8]}"
            self._send(connect_packet(client_id, self.cfg.user, self.cfg.password, self.cfg.keepalive))
            self._expect_connack(read_packet(sock))
            self._send(subscribe_packet(self._next_pid(), subscribe))
            self._expect_suback(read_packet(sock))
        except BaseException:
            raw.close()
            self.sock = None
            raise
        sock.settimeout(1.0)
        self.last_rx = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mqtt-reader", daemon=True)
        self._thread.start()

    def _secure(self, raw: socket.socket) -> socket.socket:
        if not self.cfg.use_tls:
            return raw
        ctx = self.cfg.tls_context or ssl.create_default_context()
        if self.cfg.tls_context is None:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(raw, server_hostname=self.cfg.host)
        if self.cfg.on_tls is not None:
            self.cfg.on_tls(sock)
        return sock

    @staticmethod
    def _expect_connack(pkt: tuple[int, bytes]) -> None:
        first, body = pkt
        if first != CONNACK or len(body) < 2:
            raise MqttError(f"unexpected reply to CONNECT: {first:#x}")
        if body[1]:
            raise MqttError(f"broker refused: {CONNACK_ERRORS.get(body[1], body[1])}")

    @staticmethod
    def _expect_suback(pkt: tuple[int, bytes]) -> None:
        first, body = pkt
        if first != SUBACK or len(body) < 3 or body[2] == 0x80:
            raise MqttError("subscription refused by the printer")

    # ---- publishing
    def publish(self, topic: str, payload: bytes, qos: int = 1, wait: float = 5.0) -> None:
        """QoS 1 waits for PUBACK (raises MqttError if none within `wait` seconds)."""
        pid = self._next_pid() if qos else 0
        ev = threading.Event()
        if qos:
            self._acks[pid] = ev
        try:
            self._send(publish_packet(topic, payload, qos, pid))
            if qos and wait and not ev.wait(wait):
                raise MqttError("printer did not acknowledge the message (no PUBACK)")
        finally:
            self._acks.pop(pid, None)

    def close(self) -> None:
        self._stop.set()
        try:
            self._send(packet(DISCONNECT, b""))
        except (OSError, MqttError):
            pass
        if self.sock is not None:
            self.sock.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _next_pid(self) -> int:
        with self._lock:
            self._pid = self._pid % 65535 + 1
            return self._pid

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise MqttError("not connected")
        with self._lock:
            self.sock.sendall(data)

    # ---- reader
    def _next_packet(self) -> tuple[int, bytes] | None:
        """Next complete packet, or None on read timeout (partial data stays buffered)."""
        while True:
            parsed = try_parse(self._buf)
            if parsed is not None:
                first, body, used = parsed
                del self._buf[:used]
                return first, body
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk

    def _loop(self) -> None:
        last_ping = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self.last_rx > 1.5 * self.cfg.keepalive:
                self.last_error = "printer silent longer than 1.5 × keep-alive"
                break
            try:
                if now - last_ping > self.cfg.keepalive / 2:
                    self._send(packet(PINGREQ, b""))
                    last_ping = now
                pkt = self._next_packet()
            except (OSError, ConnectionError, MqttError) as exc:
                self.last_error = str(exc)
                break
            if pkt is not None:
                self.last_rx = time.monotonic()
                self._dispatch(*pkt)

    def _dispatch(self, first: int, body: bytes) -> None:
        try:
            if first & 0xF0 == PUBLISH:
                topic, payload, qos, pid = parse_publish(first, body)
                if qos == 1:
                    self._send(packet(PUBACK, struct.pack("!H", pid)))
                self.on_message(topic, payload)
            elif first & 0xF0 == PUBACK and len(body) >= 2:
                ev = self._acks.get(struct.unpack("!H", body[:2])[0])
                if ev is not None:
                    ev.set()
        except Exception as exc:  # noqa: BLE001 — one bad packet must not stop the reader
            self.last_error = f"bad packet: {exc}"
