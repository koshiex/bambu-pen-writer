"""Start the pen-plotter web UI: python3 -m webui [--demo-printer] [--port 8765]."""

from __future__ import annotations

import argparse
import os
import webbrowser
from pathlib import Path

from .server import App, serve, wait_forever


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--demo-printer", action="store_true",
                   help="built-in fake P1S (no hardware): uploads, pauses, page guidance")
    p.add_argument("--demo-scale", type=float, default=0.02,
                   help="demo printer: real seconds per model second (0.02 ≈ 13 s per page)")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    home = Path(os.environ.get("PDF_TO_PRINT_WEBUI_HOME", Path.home() / ".config" / "pdf-to-print"))
    demo = None
    if args.demo_printer:
        from .fake_printer import FakePrinter
        demo = FakePrinter(time_scale=args.demo_scale)
    app = App(home, demo)
    try:
        httpd = serve(app, "127.0.0.1", args.port)
    except OSError as exc:
        raise SystemExit(f"Порт {args.port} занят ({exc.strerror}): клиент уже запущен? "
                         f"Откройте http://127.0.0.1:{args.port}/ или укажите --port") from exc
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Pen plotter UI: {url}" + ("  (демо-принтер)" if demo else ""))
    if demo is not None:
        app.link.connect(app.printer_config())
    if not args.no_browser:
        webbrowser.open(url)
    try:
        wait_forever()
    except KeyboardInterrupt:
        print("\nостановлено")


if __name__ == "__main__":
    main()
