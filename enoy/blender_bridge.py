"""Transparent stdio MCP relay with a cross-process Blender lock and handoff journal.

No SDK dependency: the installed upstream server owns tool schemas, images, apps,
resources and protocol negotiation. Stdout carries only the original JSON-RPC.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .redact import redact


class SceneLock:
    def __init__(self, folder, timeout=600, name="scene.lock", cancel=None):
        self.path = Path(folder) / name
        self.timeout = timeout
        self.cancel = cancel
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            if self.cancel and self.cancel.is_set():
                self.file.close()
                raise InterruptedError("Cancelled while waiting for Blender")
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise TimeoutError("Blender is busy with another provider. Try again after it finishes.")
                time.sleep(.05)

    def __exit__(self, *_):
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
        finally:
            self.file.close()


def record(folder, actor, request, response):
    path = Path(folder) / "activity.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = []
    params = request.get("params") or {}
    result = response.get("result") or {}
    texts = [item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"]
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "provider": actor,
           "tool": params.get("name") or request["method"],
           "arguments": redact(json.dumps(params.get("arguments", {}), ensure_ascii=False))[:1200],
           "ok": not bool(response.get("error") or result.get("isError")),
           "result": redact("\n".join(texts) or json.dumps(response.get("error") or {}))[:2000]}
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps((previous + [row])[-40:], ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def relay(command, folder, actor, source=None, sink=None):
    source, sink = source or sys.stdin, sink or sys.stdout
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    requests, responses = queue.Queue(), queue.Queue()
    output_lock, input_lock = threading.Lock(), threading.Lock()
    closed = threading.Event()

    def send(stream, value, lock):
        with lock:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()

    def read_client():
        try:
            for line in source:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "method" in msg and "id" in msg:
                    requests.put(msg)
                else:  # Notifications (including cancellation) and server-request replies.
                    send(proc.stdin, msg, input_lock)
        except (OSError, ValueError):
            pass
        finally:
            closed.set()
            requests.put(None)

    def read_server():
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and "method" not in msg:
                    responses.put(msg)
                else:
                    send(sink, msg, output_lock)
        finally:
            responses.put(None)

    workers = [threading.Thread(target=fn, daemon=True) for fn in (read_client, read_server)]
    for worker in workers:
        worker.start()

    def roundtrip(msg):
        send(proc.stdin, msg, input_lock)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            try:
                reply = responses.get(timeout=.2)
            except queue.Empty:
                if closed.is_set():
                    raise EOFError("MCP client disconnected")
                continue
            if reply is None:
                raise EOFError("Blender MCP server disconnected")
            if reply.get("id") == msg["id"]:
                return reply
            send(sink, reply, output_lock)
        raise TimeoutError("Blender MCP tool did not finish within 600 seconds")

    try:
        while True:
            msg = requests.get()
            if msg is None:
                break
            try:
                if msg["method"] in ("tools/call", "resources/read"):
                    with SceneLock(folder):
                        try:
                            reply = roundtrip(msg)
                        except BaseException:
                            # Stop an unfinished edit before allowing the next owner.
                            if proc.poll() is None:
                                proc.kill()
                            proc.wait(timeout=5)
                            raise
                        try:
                            record(folder, actor, msg, reply)
                        except (OSError, ValueError, TypeError) as exc:
                            print(f"Could not record Blender handoff: {exc}", file=sys.stderr)
                else:
                    reply = roundtrip(msg)
                send(sink, reply, output_lock)
            except (OSError, ValueError, EOFError, TimeoutError) as exc:
                send(sink, {"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32603, "message": str(exc)}}, output_lock)
                # Do not release the lock and keep an unfinished upstream edit alive.
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdin.close()
        workers[1].join(5)
        proc.stdout.close()


def main():
    command = os.environ.get("ENOY_BLENDER_COMMAND")
    if not command or not Path(command).is_file():
        raise SystemExit("Set ENOY_BLENDER_COMMAND to the installed MCP for Blender executable.")
    from . import paths
    folder = os.environ.get("ENOY_BLENDER_STATE") or str(paths.app_home() / "blender/9876")
    relay([command], folder, os.environ.get("ENOY_BLENDER_ACTOR", "codex"))


if __name__ == "__main__":
    main()
