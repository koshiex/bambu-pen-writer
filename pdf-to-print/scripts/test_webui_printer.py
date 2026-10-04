#!/usr/bin/env python3
"""Tests for webui printer link (MQTT + FTPS + job flow) against the fake printer.

Run: python3 scripts/test_webui_printer.py
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webui.fake_printer import FakePrinter  # noqa: E402
from webui.mqtt_min import MqttError  # noqa: E402
from webui.printer_link import JobMeta, PrinterConfig, PrinterLink, job_pages, pause_guidance  # noqa: E402

CODE = "12345678"


def stroke(x: float) -> str:
    return (f"G0 X{x} Y100 F30000\nG1 Z74.200 F1800\nG1 X{x + 10} Y100 F6000\n"
            f"G1 Z80.200 F1800\n")


def job_text(pages: list[int]) -> str:
    out = ["; HEADER_BLOCK_START", "; EXECUTABLE_BLOCK_START", "M73 P0 R1", "G28",
           "G1 Z93.5 F1800", "M400 U1 ; install"]
    for i, p in enumerate(pages, start=1):
        out += [f";===== PAGE {p:02d} =====", f"M73 L{i}", stroke(60 + i), "G1 Z93.5 F1800"]
        if i < len(pages):
            out += ["M400 U1 ; flip", f";===== PAGE {pages[i]:02d} ====="]
    out += ["M400 U1 ; remove", "M104 S0", "; EXECUTABLE_BLOCK_END"]
    return "\n".join(out) + "\n"


def connected(fake: FakePrinter, code: str = CODE) -> tuple[PrinterLink, list[dict]]:
    updates: list[dict] = []
    link = PrinterLink(updates.append)
    cfg = PrinterConfig("127.0.0.1", fake.serial, code, mqtt_port=fake.port,
                        ftp_port=fake.ftp.port, use_tls=False)
    link.connect(cfg)
    return link, updates


def wait_for(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout waiting for condition")


def test_wrong_access_code_is_reported() -> None:
    fake = FakePrinter(access_code=CODE)
    try:
        connected(fake, code="00000000")
    except MqttError as exc:
        assert "access code" in str(exc), exc
    else:
        raise AssertionError("expected MqttError")
    finally:
        fake.close()


def test_status_after_pushall() -> None:
    fake = FakePrinter()
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        assert link.summary()["online"] is True
    finally:
        link.disconnect()
        fake.close()


def test_full_job_flow_with_page_guidance() -> None:
    fake = FakePrinter(time_scale=0.0)
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "nb.gcode"
            g.write_text(job_text([1, 2]))
            meta = link.send_job(g, "3mf", "sequential")
            assert meta.pages == (1, 2), meta
            blob = fake.ftp.files["nb.3mf"]
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                assert z.read("Metadata/plate_1.gcode").decode() == g.read_text()
        start = fake.commands[-1]
        assert start["command"] == "project_file" and start["url"] == "file:///sdcard/nb.3mf"
        assert start["use_ams"] is False and start["bed_leveling"] is False

        steps = [("Установите держатель", 0), ("страницу 02", 1), ("снимите держатель", 2)]
        for text, layer in steps:
            wait_for(lambda: link.summary()["state"] == "PAUSE" and link.summary()["layer"] == layer)
            assert text in link.summary()["guidance"], link.summary()
            link.command("resume")
            wait_for(lambda: link.summary()["state"] != "PAUSE" or link.summary()["layer"] != layer)
        wait_for(lambda: link.summary()["state"] == "FINISH")
    finally:
        link.disconnect()
        fake.close()


def test_refuses_to_send_while_busy() -> None:
    fake = FakePrinter(time_scale=0.0)
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "nb.gcode"
            g.write_text(job_text([1]))
            link.send_job(g, "3mf", "sequential")
            wait_for(lambda: link.summary()["state"] == "PAUSE")
            try:
                link.send_job(g, "3mf", "sequential")
            except RuntimeError as exc:
                assert "занят" in str(exc)
            else:
                raise AssertionError("expected busy error")
    finally:
        link.disconnect()
        fake.close()


def test_plain_gcode_method_uses_cache_and_gcode_file() -> None:
    fake = FakePrinter(time_scale=0.0)
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "nb.gcode"
            g.write_text(job_text([1]))
            link.send_job(g, "gcode", "sequential")
        assert "cache/nb.gcode" in fake.ftp.files
        assert fake.commands[-1] == {"sequence_id": fake.commands[-1]["sequence_id"],
                                     "command": "gcode_file", "param": "/sdcard/cache/nb.gcode"}
    finally:
        link.disconnect()
        fake.close()


def test_upload_only_does_not_start() -> None:
    fake = FakePrinter(time_scale=0.0)
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "nb.gcode"
            g.write_text(job_text([1]))
            link.send_job(g, "upload", "sequential")
        assert "nb.3mf" in fake.ftp.files
        assert not any(c.get("command") in ("project_file", "gcode_file") for c in fake.commands)
    finally:
        link.disconnect()
        fake.close()


def test_unknown_command_rejected() -> None:
    link = PrinterLink(lambda _: None)
    try:
        link.command("G28")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_job_pages_and_spread_guidance() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from page_order import SPREAD_ORDER_24

    text = job_text(list(SPREAD_ORDER_24))
    pages = job_pages(text)
    assert pages == tuple(SPREAD_ORDER_24), pages
    meta = JobMeta("x.gcode", "x.3mf", pages, "spread", 0.0)
    st = {"gcode_state": "PAUSE", "stg_cur": 5, "layer_num": 4}
    g = pause_guidance(st, meta, 0)
    assert "страницу 03" in g and "разворот 2" in g and "смените разворот" in g, g
    st["layer_num"] = 2
    assert "страницу 23" in pause_guidance(st, meta, 0)
    st["stg_cur"] = 16
    assert "пользователя" in pause_guidance(st, meta, 0)


def test_developer_mode_and_rejection_messages() -> None:
    from webui.printer_link import developer_mode, reject_reason

    assert developer_mode({"fun": "3EC18FFF9CFF"}) is True
    assert developer_mode({"fun": "3EC1AFFF9CFF"}) is False
    assert developer_mode({}) is None
    msg = reject_reason({"command": "project_file", "result": "failed",
                         "reason": "mqtt message verify failed", "err_code": 84033543})
    assert "Developer Mode" in msg, msg


def test_status_reports_developer_mode() -> None:
    fake = FakePrinter()
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["developer_mode"] is True)
    finally:
        link.disconnect()
        fake.close()


def test_mqtt_survives_a_packet_split_across_a_stall() -> None:
    """A PUBLISH arriving in two halves 1.5 s apart must still be parsed (no desync)."""
    import socket as _s
    import threading as _t
    from webui.mqtt_min import (CONNACK, SUBACK, MqttClient, MqttConfig, packet, publish_packet,
                                read_packet)

    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got: list[bytes] = []

    def broker() -> None:
        c, _ = srv.accept()
        read_packet(c)
        c.sendall(packet(CONNACK, b"\x00\x00"))
        read_packet(c)
        c.sendall(packet(SUBACK, b"\x00\x01\x00"))
        msg = publish_packet("t", b'{"print":{"gcode_state":"IDLE"}}')
        c.sendall(msg[:7])
        time.sleep(1.5)
        c.sendall(msg[7:] + publish_packet("t", b'{"print":{"layer_num":3}}'))
        time.sleep(1)
        c.close()

    _t.Thread(target=broker, daemon=True).start()
    cli = MqttClient(MqttConfig("127.0.0.1", srv.getsockname()[1], use_tls=False),
                     lambda _t2, p: got.append(p))
    cli.connect("t")
    wait_for(lambda: len(got) == 2, timeout=6)
    assert b"IDLE" in got[0] and b"layer_num" in got[1], got
    cli.close()


def test_bad_payload_does_not_kill_the_reader() -> None:
    fake = FakePrinter()
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        fake._report_raw(b"[1, 2, 3]")
        fake._report_raw(b"not json")
        fake._set(layer_num=7)
        wait_for(lambda: link.summary()["layer"] == 7)
        assert link.online
    finally:
        link.disconnect()
        fake.close()


def test_wrong_serial_is_reported_on_connect() -> None:
    fake = FakePrinter()
    link = PrinterLink(lambda _: None, first_report_timeout=1.0)
    cfg = PrinterConfig("127.0.0.1", "WRONGSERIAL", CODE, mqtt_port=fake.port,
                        ftp_port=fake.ftp.port, use_tls=False)
    try:
        link.connect(cfg)
    except RuntimeError as exc:
        assert "серийный" in str(exc), exc
    else:
        raise AssertionError("expected RuntimeError")
    finally:
        link.disconnect()
        fake.close()


def test_rejected_command_raises_with_developer_mode_hint() -> None:
    fake = FakePrinter(reject_commands=True)
    link, _ = connected(fake)
    try:
        wait_for(lambda: link.summary()["state"] == "IDLE")
        try:
            link.command("pause")
        except RuntimeError as exc:
            assert "Developer Mode" in str(exc), exc
        else:
            raise AssertionError("expected rejection")
    finally:
        link.disconnect()
        fake.close()


def test_send_refused_until_status_is_known() -> None:
    link = PrinterLink(lambda _: None)
    link.cfg = PrinterConfig("127.0.0.1", "S", CODE)
    link.client = type("C", (), {"alive": True})()
    try:
        link.send_job(Path("x.gcode"), "3mf", "sequential")
    except RuntimeError as exc:
        assert "статус" in str(exc), exc
    else:
        raise AssertionError("expected refusal")


def test_guidance_ignores_a_foreign_job() -> None:
    meta = JobMeta("nb.gcode", "nb.3mf", (1, 2), "sequential", 0.0)
    st = {"gcode_state": "PAUSE", "stg_cur": 5, "layer_num": 1, "subtask_name": "other"}
    assert "не отсюда" in pause_guidance(st, meta, 0)
    st["subtask_name"] = "nb"
    assert "страницу 02" in pause_guidance(st, meta, 0)


def test_tls_path_with_pinned_certificate() -> None:
    import shutil
    import subprocess as sp

    if not shutil.which("openssl"):
        print("    (skip: no openssl)")
        return
    with tempfile.TemporaryDirectory() as td:
        key, crt = Path(td) / "k.pem", Path(td) / "c.pem"
        sp.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                "-out", str(crt), "-days", "1", "-subj", "/CN=fake-p1s"], check=True,
               capture_output=True)
        fake = FakePrinter(time_scale=0.0, tls=(crt, key))
        pins: dict = {}
        link = PrinterLink(lambda _: None, pins=pins)
        cfg = PrinterConfig("127.0.0.1", fake.serial, CODE, mqtt_port=fake.port,
                            ftp_port=fake.ftp.port, use_tls=True)
        try:
            link.connect(cfg)
            wait_for(lambda: link.summary()["state"] == "IDLE")
            g = Path(td) / "nb.gcode"
            g.write_text(job_text([1]))
            link.send_job(g, "3mf", "sequential")
            assert "nb.3mf" in fake.ftp.files
            assert len(pins) == 2, pins                       # MQTT + FTPS pinned on first use
            pins[next(iter(pins))] = "0" * 64                  # certificate "changed"
            link.disconnect()
            try:
                link.connect(cfg)
            except Exception as exc:                            # noqa: BLE001
                assert "сертификат" in str(exc), exc
            else:
                raise AssertionError("expected pin mismatch")
        finally:
            link.disconnect()
            fake.close()


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_webui_printer: {len(tests)} passed")


if __name__ == "__main__":
    main()
