"""Application entry point: wires everything together and opens the window.

Window options, in order:
1. pywebview (native window using Microsoft Edge WebView2, built into Windows 11)
2. Microsoft Edge in app mode (a chromeless window)
3. the default web browser
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, config as config_mod, paths, winshell
from .backends.base import CLAUDE, LOCAL
from .backends.claude_code import ClaudeCodeBackend
from .backends.ollama import OllamaBackend
from .orchestrator import EventBus, Orchestrator
from .redact import redact
from .server import App, Server
from .storage import Storage

log = logging.getLogger("fusion")


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool = False) -> None:
    handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "fusion.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stderr and verbose:
        console = logging.StreamHandler()
        console.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)


def build(cfg: dict | None = None, cfg_warnings: list[str] | None = None, synchronous: bool = False,
          folder_picker=None, save_config: bool = True) -> tuple[App, Orchestrator]:
    if cfg is None:
        cfg, cfg_warnings = config_mod.load()
    storage = Storage(paths.database_path())
    interrupted = storage.mark_interrupted()
    if interrupted:
        log.info("Marked %d interrupted task(s) for resume: %s", len(interrupted), interrupted)
    backends = {LOCAL: OllamaBackend(cfg["ollama"]), CLAUDE: ClaudeCodeBackend(cfg["claude"])}
    last = storage.kv_get("claude_last_verified")
    if last:
        backends[CLAUDE].last_verified = last
    orc = Orchestrator(storage, cfg, backends, EventBus(), paths.backups_dir(), synchronous=synchronous)
    app = App(orc, cfg_warnings or [], folder_picker=folder_picker, save_config=save_config)
    return app, orc


# Single instance ----------------------------------------------------------------------

def _instance_file() -> Path:
    return paths.app_home() / "instance.json"


def running_instance() -> str | None:
    """URL of an already running Enoy, if one responds."""
    f = _instance_file()
    if not f.exists():
        return None
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        with urllib.request.urlopen(info["url"] + "static/app.js", timeout=2) as resp:
            if resp.status == 200:
                return info["url"]
    except Exception:
        return None
    return None


def message_box(text: str, title: str = "Enoy") -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(text)


# Windows ---------------------------------------------------------------------------

def find_edge() -> str | None:
    for candidate in (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("msedge")


def open_with_pywebview(url: str, app: App) -> bool:
    try:
        import webview  # type: ignore
    except ImportError:
        log.info("pywebview is not installed; trying Edge app mode.")
        return False
    winshell.set_app_id()  # Taskbar shows Enoy, not Python.
    window = webview.create_window("Enoy", url, width=1440, height=920, min_size=(1000, 640),
                                   background_color="#1f1e1c")

    def on_shown() -> None:
        try:
            winshell.set_window_icon(int(window.native.Handle.ToInt64()))
        except Exception as exc:
            log.info("Could not set the window icon: %s", exc)

    window.events.shown += on_shown

    def picker() -> str | None:
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def saver(filename: str) -> str | None:
        result = window.create_file_dialog(webview.SAVE_DIALOG, save_filename=filename)
        if isinstance(result, (list, tuple)):
            result = result[0] if result else None
        return result or None

    app.folder_picker = picker
    app.file_saver = saver
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(paths.sub_dir("webview")))
    except Exception as exc:
        log.warning("pywebview failed (%s); trying Edge app mode.", exc)
        return False
    return True


def tk_folder_picker() -> str | None:
    result: dict = {}

    def run():
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        result["path"] = filedialog.askdirectory(title="Open a project folder")
        root.destroy()

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return result.get("path") or None


def open_with_edge(url: str, app: App) -> bool:
    edge = find_edge()
    if not edge:
        return False
    app.folder_picker = tk_folder_picker
    profile = paths.sub_dir("edge-profile")
    proc = subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                             "--no-default-browser-check", "--window-size=1440,920"])
    # The dedicated profile makes this a separate Edge process that ends when
    # the window is closed. The heartbeat check covers Edge handing the window
    # to an existing process instead.
    while True:
        time.sleep(2)
        if proc.poll() is not None and time.time() - app.last_seen > 20:
            return True
        if time.time() - app.last_seen > 180:
            return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fusion-ai", description="Enoy")
    parser.add_argument("--no-window", action="store_true", help="Run the server only (for development).")
    parser.add_argument("--browser", action="store_true", help="Open in the default browser instead of a native window.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--data-dir", help="Use this folder for settings, database and logs (default %%LOCALAPPDATA%%\\FusionAI).")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["FUSION_AI_HOME"] = os.path.abspath(args.data_dir)

    setup_logging(args.verbose)
    existing = running_instance()
    if existing and not args.no_window:
        message_box("Enoy is already running. Switch to its window (check the taskbar).")
        return 0

    app, orc = build()
    orc.start_memory_supervisor()
    server = Server(app, args.port).start()
    _instance_file().write_text(json.dumps({"url": server.url, "pid": os.getpid()}), encoding="utf-8")
    log.info("Enoy %s listening on %s", __version__, server.url)
    url = server.url
    try:
        if args.no_window:
            print(f"Enoy running at {url}  (Ctrl+C to stop)", flush=True)
            while True:
                time.sleep(1)
        if args.browser:
            import webbrowser
            app.folder_picker = tk_folder_picker
            webbrowser.open(url)
            while time.time() - app.last_seen < 180:
                time.sleep(2)
            return 0
        if open_with_pywebview(url, app):
            return 0
        if open_with_edge(url, app):
            return 0
        import webbrowser
        app.folder_picker = tk_folder_picker
        webbrowser.open(url)
        while time.time() - app.last_seen < 180:
            time.sleep(2)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()
        if orc.memory is not None:
            orc.memory.stop()
            orc.memory.unload_on_exit()  # Frees the model's RAM and VRAM; Ollama itself keeps running.
        try:
            _instance_file().unlink()
        except OSError:
            pass
        orc.db.close()


if __name__ == "__main__":
    sys.exit(main())
