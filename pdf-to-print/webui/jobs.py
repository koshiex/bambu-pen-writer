"""One background job at a time (build, sheet, simulation, upload) with a streamed log.

A job is "running" until its worker thread has really finished (process exited, result
recorded); cancel only signals the process group (SIGTERM, then SIGKILL after a grace period).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

Broadcast = Callable[[str, dict], None]
LOG_LINES = 4000
KILL_GRACE_S = 5.0


@dataclass
class Job:
    id: int
    kind: str
    title: str
    state: str = "running"            # running | ok | failed | cancelled
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None
    result: dict = field(default_factory=dict)
    cancel_requested: bool = False


class JobRunner:
    def __init__(self, broadcast: Broadcast, cwd: Path) -> None:
        self.broadcast, self.cwd = broadcast, cwd
        self.job: Job | None = None
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._next_id = 1

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def snapshot(self) -> dict:
        return {"job": self._public(self.job) if self.job else None, "log": list(self.log)[-400:]}

    @staticmethod
    def _public(job: Job) -> dict:
        d = asdict(job)
        d.pop("cancel_requested", None)
        return d

    def _begin(self, kind: str, title: str) -> Job:
        with self._lock:
            if self.busy:
                raise RuntimeError(f"уже выполняется: {self.job.title}")
            self.job = Job(self._next_id, kind, title)
            self._next_id += 1
            self.log.clear()
        self.broadcast("job", self._public(self.job))
        return self.job

    def emit(self, line: str) -> None:
        line = line.rstrip("\n")
        self.log.append(line)
        self.broadcast("log", {"line": line})

    def _finish(self, job: Job, state: str, returncode: int | None, result: dict) -> None:
        job.returncode, job.result, job.finished = returncode, result, time.time()
        job.state = "cancelled" if job.cancel_requested else state
        self._procs.pop(job.id, None)
        self.broadcast("job", self._public(job))

    def run_command(self, kind: str, title: str, cmd: list[str], env: dict[str, str],
                    on_done: Callable[[int], dict] | None = None) -> Job:
        job = self._begin(kind, title)
        self.emit("$ " + " ".join(Path(c).name if i < 2 else c for i, c in enumerate(cmd)))
        threading.Thread(target=self._command_worker, args=(job, cmd, env, on_done),
                         name=f"job-{job.id}", daemon=True).start()
        return job

    def _command_worker(self, job: Job, cmd: list[str], env: dict[str, str],
                        on_done: Callable[[int], dict] | None) -> None:
        state, rc, result = "failed", None, {}
        try:
            proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                    bufsize=1, start_new_session=True)
            self._procs[job.id] = proc
            for line in proc.stdout:
                self.emit(line)
            rc = proc.wait()
            state = "ok" if rc == 0 else "failed"
            if on_done is not None:
                result = on_done(rc) or {}
        except Exception as exc:  # noqa: BLE001 — reported to the UI, the job always finishes
            self.emit(f"ERROR: {exc}")
            state = "failed"
            self._kill(job.id, signal.SIGKILL)
        finally:
            self._finish(job, state, rc, result)

    def run_callable(self, kind: str, title: str, fn: Callable[[Callable[[str], None]], dict]) -> Job:
        job = self._begin(kind, title)

        def worker() -> None:
            state, result = "failed", {}
            try:
                result = fn(self.emit) or {}
                state = "ok"
            except Exception as exc:  # noqa: BLE001 — surfaced to the UI, not swallowed
                self.emit(f"ERROR: {exc}")
                self.emit(traceback.format_exc(limit=3))
                result = {"error": str(exc)}
            finally:
                self._finish(job, state, 0 if state == "ok" else 1, result)

        threading.Thread(target=worker, name=f"job-{job.id}", daemon=True).start()
        return job

    def cancel(self) -> bool:
        job = self.job
        if job is None or job.state != "running" or job.id not in self._procs:
            return False
        job.cancel_requested = True
        self.emit("— отмена… —")
        self._kill(job.id, signal.SIGTERM)

        def escalate() -> None:
            time.sleep(KILL_GRACE_S)
            if job.state == "running":
                self._kill(job.id, signal.SIGKILL)

        threading.Thread(target=escalate, daemon=True).start()
        return True

    def _kill(self, job_id: int, sig: int) -> None:
        proc = self._procs.get(job_id)
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
