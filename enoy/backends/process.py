"""Run a native agent, stream its events, and stop its entire process tree."""
from __future__ import annotations

import subprocess
import threading
import time

from .base import BackendError
from .claude_code import _kill_tree


def run(cmd, prompt, cwd, request, timeout, on_line):
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output, errors, failures = [], [], []

    def read_output():
        try:
            for line in proc.stdout:
                output.append(line)
                on_line(line)
        except Exception as exc:
            failures.append(exc)

    def read_errors():
        errors.append(proc.stderr.read())

    def write_input():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    workers = [threading.Thread(target=fn, daemon=True) for fn in (read_output, read_errors, write_input)]
    for worker in workers:
        worker.start()
    started = time.monotonic()
    try:
        while proc.poll() is None or workers[0].is_alive():
            if request.cancel.is_set():
                raise BackendError("cancelled", "Agent request cancelled.")
            if time.monotonic() - started > timeout:
                raise BackendError("timeout", f"Agent did not finish within {timeout:.0f} seconds.")
            if failures:
                raise failures[0]
            workers[0].join(0.1)
            if not workers[0].is_alive():
                request.cancel.wait(0.05)
        if failures:
            raise failures[0]
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
        for worker in workers:
            worker.join(5)
        proc.stdout.close()
        proc.stderr.close()
    return "".join(output), "".join(errors), proc.returncode
