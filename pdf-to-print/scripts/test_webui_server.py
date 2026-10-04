#!/usr/bin/env python3
"""Tests for the web UI server: security, files, jobs, printer flow (fake printer, temp dirs).

Run: python3 scripts/test_webui_server.py
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from webui import pipeline_api as api  # noqa: E402
from webui.fake_printer import FakePrinter  # noqa: E402
from webui.jobs import JobRunner  # noqa: E402
from webui.server import App, serve  # noqa: E402

TMP = Path(tempfile.mkdtemp())
for name in ("pdfs", "output", "build", "home"):
    (TMP / name).mkdir()
api.PDF_DIR, api.OUTPUT, api.BUILD = TMP / "pdfs", TMP / "output", TMP / "build"
DEMO = FakePrinter(time_scale=0.0)
APP = App(TMP / "home", DEMO)
HTTPD = serve(APP, "127.0.0.1", 0)
PORT = HTTPD.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = APP.token


def call(path: str, body=None, *, token: str | None = TOKEN, raw: bytes | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    h = dict(headers or {})
    if token:
        h["X-Token"] = token
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = r.headers.get("Content-Type", "")
            payload = r.read()
            return r.status, json.loads(payload) if "json" in ctype else {"bytes": payload}
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_for(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timeout")


def job_text(pages: list[int]) -> str:
    out = ["; EXECUTABLE_BLOCK_START", "G28", "G1 Z93.5 F1800", "M400 U1"]
    for i, p in enumerate(pages, 1):
        out += [f";===== PAGE {p:02d} =====", f"M73 L{i}", "G0 X100 Y100 F30000", "G1 Z74.200 F1800",
                "G1 X110 Y100 F6000", "G1 Z80.200 F1800", "G1 Z93.5 F1800"]
        if i < len(pages):
            out += ["M400 U1"]
    return "\n".join(out + ["M400 U1", "M104 S0"]) + "\n"


def test_index_embeds_token_and_static_served() -> None:
    with urllib.request.urlopen(BASE + "/") as r:
        assert TOKEN in r.read().decode()
    with urllib.request.urlopen(BASE + "/static/app.js") as r:
        assert b"EventSource" in r.read()


def test_api_requires_token() -> None:
    assert call("/api/state", token=None)[0] == 401
    assert call("/api/state", token="wrong")[0] == 401
    assert call("/api/state")[0] == 200


def test_foreign_host_header_is_refused() -> None:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("GET", "/api/state", headers={"Host": "evil.example:80", "X-Token": TOKEN})
    assert conn.getresponse().status == 403


def test_pdf_upload_validation_and_name_sanitizing() -> None:
    code, body = call("/api/pdf", raw=b"not a pdf", headers={"X-Filename": "a.pdf"})
    assert code == 400 and "PDF" in body["error"], body
    code, body = call("/api/pdf", raw=b"%PDF-1.4 tiny", headers={"X-Filename": "../../evil.pdf"})
    assert code == 200 and body["pdf"] == "evil.pdf", body
    assert (api.PDF_DIR / "evil.pdf").is_file() and not (TMP / "evil.pdf").exists()


def test_files_endpoint_blocks_traversal() -> None:
    (api.OUTPUT / "ok.gcode").write_text("G28\n")
    assert call("/files/output/ok.gcode")[0] == 200
    assert call("/files/output/../home/printer.json")[0] == 400
    assert call("/files/%2e%2e/%2e%2e/etc/passwd")[0] in (400, 404)


def test_printer_config_is_validated_private_and_never_returned() -> None:
    assert call("/api/printer/config", {"ip": "1.2.3.4", "serial": "S", "access_code": "x"})[0] == 400
    code, _ = call("/api/printer/config", {"ip": "1.2.3.4", "serial": "01P00A123456789", "access_code": "ab12CD34"})
    assert code == 200
    cfg = TMP / "home" / "printer.json"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    _, state = call("/api/state")
    assert state["printer_config"] == {"ip": "1.2.3.4", "serial": "01P00A123456789", "has_code": True}
    assert "ab12CD34" not in json.dumps(state)


def test_build_rejects_bad_settings() -> None:
    code, body = call("/api/build", {"pdf": "missing.pdf"})
    assert code == 400 and "missing.pdf" in body["error"], body
    (api.PDF_DIR / "x.pdf").write_bytes(b"%PDF-1.4")
    code, body = call("/api/build", {"pdf": "x.pdf", "z_hop": "0.1"})
    assert code == 400 and "подъём" in body["error"], body


def test_build_does_not_overwrite_existing_output_without_consent() -> None:
    (api.PDF_DIR / "z.pdf").write_bytes(b"%PDF-1.4")
    (api.OUTPUT / "z.gcode").write_text("printed before\n")
    code, body = call("/api/build", {"pdf": "z.pdf"})
    assert code == 409 and body.get("exists") == "z.gcode", (code, body)
    assert (api.OUTPUT / "z.gcode").read_text() == "printed before\n"


def test_send_requires_passed_simulation_then_runs_on_fake_printer() -> None:
    (api.OUTPUT / "nb.gcode").write_text(job_text([1, 2]))
    assert call("/api/printer/connect", {})[0] == 200
    wait_for(lambda: APP.link.summary()["state"] == "IDLE")
    code, body = call("/api/printer/send", {"file": "nb.gcode", "method": "3mf"})
    assert code == 400 and "заблокирована" in body["error"], body
    report = api.OUTPUT / "nb.sim.md"
    report.write_text("# Printer simulation: nb.gcode — PASS\n\nholder=soft, paper stack 3.0 mm\n")
    code, body = call("/api/printer/send", {"file": "nb.gcode", "method": "3mf", "holder": "soft"})
    assert code == 200, body
    wait_for(lambda: APP.runner.job.state == "ok")
    assert "nb.3mf" in DEMO.ftp.files
    wait_for(lambda: "Установите держатель" in APP.link.summary()["guidance"])
    assert call("/api/printer/command", {"name": "resume"})[0] == 200
    wait_for(lambda: "страницу 02" in APP.link.summary()["guidance"])
    assert call("/api/printer/command", {"name": "G28"})[0] == 400


def test_sse_streams_events() -> None:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("GET", f"/api/events?token={TOKEN}")
    resp = conn.getresponse()
    assert resp.status == 200
    APP.files_changed()
    line = b""
    while not line.startswith(b"event:"):
        line = resp.fp.readline()
    assert line.strip() in (b"event: files", b"event: printer", b"event: job", b"event: log")
    conn.close()


def test_job_runner_runs_and_cancels() -> None:
    events: list[tuple[str, dict]] = []
    runner = JobRunner(lambda e, d: events.append((e, d)), ROOT)
    runner.run_command("t", "echo", [sys.executable, "-c", "print('hello')"], dict(os.environ))
    wait_for(lambda: runner.job.state == "ok")
    assert "hello" in runner.snapshot()["log"]
    runner.run_command("t", "sleep", [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    try:
        runner.run_command("t", "second", [sys.executable, "-c", "pass"], dict(os.environ))
    except RuntimeError as exc:
        assert "уже выполняется" in str(exc)
    else:
        raise AssertionError("expected busy error")
    time.sleep(0.3)
    assert runner.cancel()
    wait_for(lambda: runner.job.state == "cancelled")


def test_settings_map_to_env() -> None:
    (api.PDF_DIR / "y.pdf").write_bytes(b"%PDF-1.4")
    s = api.parse_settings({"pdf": "y.pdf", "holder": "soft", "page_order": "spread", "z_hop": "2.5",
                            "strikethrough": True, "start_page": "3"})
    cmd, env, out = api.build_command(s)
    assert out.name == "y_experimental_from3.gcode", out
    assert env["PDF_TO_PRINT_Z_HOP_MM"] == "2.5" and env["PDF_TO_PRINT_SOFT_HOLDER"] == "1"
    assert env["PDF_TO_PRINT_EXPERIMENTAL_STRIKETHROUGH"] == "1" and env["START_PAGE"] == "3"
    assert cmd[-1] == "spread" and "PDF_TO_PRINT_EXPERIMENTAL_VARIABLE_PRESSURE" not in env


def test_security_headers() -> None:
    with urllib.request.urlopen(BASE + "/") as r:
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    (api.OUTPUT / "h.md").write_text("x")
    req = urllib.request.Request(BASE + "/files/output/h.md", headers={"X-Token": TOKEN})
    with urllib.request.urlopen(req) as r:
        assert "sandbox" in r.headers["Content-Security-Policy"]
        assert r.headers["Content-Disposition"].startswith("attachment")


def test_cyrillic_and_spaces_in_pdf_name() -> None:
    from urllib.parse import quote

    code, body = call("/api/pdf", raw=b"%PDF-1.4", headers={"X-Filename": quote("Конспект 3 (итог).pdf")})
    assert code == 200 and body["pdf"] == "Конспект_3__итог_.pdf", body
    assert (api.PDF_DIR / body["pdf"]).is_file()


def test_bad_bodies_are_rejected_cleanly() -> None:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.putrequest("POST", "/api/build")
    for k, v in (("Host", f"127.0.0.1:{PORT}"), ("X-Token", TOKEN), ("Content-Length", "-1")):
        conn.putheader(k, v)
    conn.endheaders()
    assert conn.getresponse().status == 400
    assert call("/api/build", ["not", "an", "object"])[0] == 400


def test_send_gate_needs_exact_report_and_holder() -> None:
    g = api.OUTPUT / "gate.gcode"
    g.write_text("G28\n")
    rep = api.OUTPUT / "gate.sim.md"
    rep.write_text("# Printer simulation: other.gcode — PASS\n\nholder=soft\n")
    assert "симуляция" in api.send_check("gate.gcode", "soft")
    rep.write_text("# Printer simulation: gate.gcode — PASS\n\nno holder line\n")
    assert "держател" in api.send_check("gate.gcode", "soft")
    rep.write_text("# Printer simulation: gate.gcode — PASS\n\nholder=soft, paper\n")
    assert api.send_check("gate.gcode", "soft") == ""


def test_printer_config_rules() -> None:
    assert call("/api/printer/config", {"ip": "1.2.3.4; rm", "serial": "01P00A123456789", "access_code": "ab12CD34"})[0] == 400
    assert call("/api/printer/config", {"ip": "192.168.1.50", "serial": "01P00A123456789", "access_code": "ab12CD34"})[0] == 200
    code, body = call("/api/printer/config", {"ip": "192.168.1.51", "serial": "01P00A123456789"})
    assert code == 400 and "access code" in body["error"], body


def test_job_survives_non_utf8_output_and_on_done_errors() -> None:
    runner = JobRunner(lambda e, d: None, ROOT)
    runner.run_command("t", "bytes", [sys.executable, "-c",
                       "import sys; sys.stdout.buffer.write(b'ok \\xff\\n')"], dict(os.environ))
    wait_for(lambda: runner.job.state != "running")
    assert runner.job.state == "ok" and not runner.busy, runner.job

    def boom(_rc: int) -> dict:
        raise OSError("stat race")

    runner.run_command("t", "boom", [sys.executable, "-c", "pass"], dict(os.environ), boom)
    wait_for(lambda: runner.job.state != "running")
    assert runner.job.state == "failed" and not runner.busy


def test_cancel_keeps_runner_busy_until_process_exits() -> None:
    runner = JobRunner(lambda e, d: None, ROOT)
    code = "import signal, time; signal.signal(signal.SIGTERM, lambda *a: time.sleep(1.5) or exit(0)); time.sleep(30)"
    runner.run_command("t", "slow", [sys.executable, "-c", code], dict(os.environ))
    time.sleep(0.4)
    assert runner.cancel()
    assert runner.busy, "must stay busy until the process is gone"
    wait_for(lambda: runner.job.state == "cancelled", timeout=10)
    assert not runner.busy


def test_output_job_rejects_printer_supplied_paths() -> None:
    from webui.server import output_job
    assert output_job("../../etc/passwd") is None
    assert output_job("no_such_job_zz") is None


def test_motion_endpoint_overview_and_page() -> None:
    (api.OUTPUT / "mv.gcode").write_text(job_text([1, 2]))
    code, ov = call("/api/motion?file=mv.gcode")
    assert code == 200 and [p["page"] for p in ov["pages"]] == [1, 2], ov
    code, page = call("/api/motion?file=mv.gcode&page=2")
    assert code == 200 and page["page"] == 2 and len(page["x"]) == len(page["k"]) + 1, page.keys()
    assert call("/api/motion?file=mv.gcode&page=9")[0] == 400
    assert call("/api/motion?file=../x.gcode")[0] == 400
    with urllib.request.urlopen(BASE + "/static/viewer.js") as r:
        assert b"MotionViewer" in r.read()


def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"test_webui_server: {len(tests)} passed")


if __name__ == "__main__":
    main()
