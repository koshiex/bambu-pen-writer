"""Upload a file to the printer's SD card over implicit FTPS (port 990, user bblp).

Bambu quirks handled here (stdlib ftplib cannot do them out of the box):
  * implicit TLS: the control socket is TLS from the first byte;
  * the data channel must reuse the control channel's TLS session ("522 session reuse
    required" otherwise);
  * the printer never answers the TLS close_notify, so ftplib's `conn.unwrap()` after STOR
    hangs — the data socket is closed without unwrap.
`use_tls=False` talks plain FTP (fake printer in tests / demo).
"""

from __future__ import annotations

import ftplib
import socket
import ssl
from pathlib import Path
from typing import BinaryIO, Callable

BLOCK = 64 * 1024


class _ImplicitFtpTls(ftplib.FTP_TLS):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sock: socket.socket | None = None

    @property
    def sock(self):  # type: ignore[override]
        return self._sock

    @sock.setter
    def sock(self, value) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # printer uses a self-signed LAN certificate
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2   # data-channel session reuse (ha-bambulab)
    return ctx


def _stor(ftp: ftplib.FTP, remote: str, fh: BinaryIO,
          progress: Callable[[int], None] | None) -> str:
    """storbinary without the TLS unwrap that hangs on Bambu firmware."""
    ftp.voidcmd("TYPE I")
    conn, _ = ftp.ntransfercmd(f"STOR {remote}")
    sent = 0
    try:
        while buf := fh.read(BLOCK):
            conn.sendall(buf)
            sent += len(buf)
            if progress:
                progress(sent)
    finally:
        conn.close()
    return ftp.voidresp()


def upload(host: str, access_code: str, local: Path | BinaryIO, remote_name: str, *,
           port: int = 990, use_tls: bool = True, timeout: float = 30.0,
           progress: Callable[[int], None] | None = None,
           on_tls: Callable[[ssl.SSLSocket], None] | None = None) -> str:
    """Upload and return the server's final reply; verifies the stored size via SIZE.
    `on_tls(control_socket)` runs right after the TLS handshake (certificate pinning)."""
    ftp: ftplib.FTP
    if use_tls:
        ftp = _ImplicitFtpTls(context=_context(), timeout=timeout)
    else:
        ftp = ftplib.FTP(timeout=timeout)
    try:
        ftp.connect(host, port)
        if use_tls and on_tls is not None:
            on_tls(ftp.sock)
    except BaseException:
        ftp.close()
        raise
    try:
        ftp.login("bblp", access_code)
        if use_tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        fh = local.open("rb") if isinstance(local, Path) else local
        size = fh.seek(0, 2)
        fh.seek(0)
        try:
            reply = _stor(ftp, remote_name, fh, progress)
        except ftplib.error_temp as exc:             # "426" after a complete transfer is benign
            reply = str(exc)
        finally:
            if isinstance(local, Path):
                fh.close()
        stored = ftp.size(remote_name)
    finally:
        try:
            ftp.quit()
        except (OSError, ftplib.Error, EOFError):
            ftp.close()
    if stored != size:
        raise ftplib.error_reply(f"upload incomplete: {stored} of {size} bytes ({reply})")
    return reply
