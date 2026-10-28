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

from . import __version__, config as config_mod, devstore, paths, winshell
from .backends.base import CLAUDE, CODEX
from .backends.codex import CodexBackend
from .backends.claude_code import ClaudeCodeBackend
from .orchestrator import EventBus, Orchestrator
from .redact import redact
from .server import App, Server
from .storage import Storage

log = logging.getLogger("enoy")


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool = False) -> None:
    handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "enoy.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
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
    build_interrupted = devstore.DevStore(storage).recover()
    if build_interrupted:
        log.info("Marked %d interrupted Build task(s): %s", len(build_interrupted), build_interrupted)
    backends = {CLAUDE: ClaudeCodeBackend(cfg["claude"])}
    backends[CODEX] = CodexBackend(cfg["codex"])
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
                                   background_color="#1f1e1c", text_select=True)

    def on_shown() -> None:
        try:
            winshell.set_window_icon(int(window.native.Handle.ToInt64()))
        except Exception as exc:
            log.info("Could not set the window icon: %s", exc)

    window.events.shown += on_shown

    def before_show(window) -> None:
        # Runs on the window's UI thread, before it is shown: swap the Windows caption for Enoy's own header.
        try:
            app.window = DesktopFrame(window.native)
        except Exception as exc:
            log.info("Keeping the standard window frame: %s", exc)

    if os.name == "nt" and os.environ.get("ENOY_NATIVE_FRAME") != "1":
        window.events.before_show += before_show

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
    app.shutdown_callback = window.destroy
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(paths.sub_dir("webview")))
    except Exception as exc:
        log.warning("pywebview failed (%s); trying Edge app mode.", exc)
        return False
    finally:
        app.shutdown_callback = None
    return True


class DesktopFrame:
    """The pywebview window with Enoy's integrated header (see winframe.py)."""

    def __init__(self, form):
        import clr  # noqa: F401  (pythonnet, loaded by pywebview)
        import System.Windows.Forms as WinForms
        from System import Action
        from . import winframe
        self.form = form
        self._wf, self._winforms, self._action = winframe, WinForms, Action
        self.hwnd = int(form.Handle.ToInt64())
        self.strip = int(winframe.TOP_STRIP * winframe._scale(self.hwnd))
        form.Padding = WinForms.Padding(0, self.strip, 0, 0)
        winframe.install(self.hwnd, self._on_size)
        form.browser.webview.CoreWebView2InitializationCompleted += self._on_webview_ready
        winframe.set_colors(self.hwnd, True, "#1f1e1c")

    def _on_webview_ready(self, sender, args):
        if args.IsSuccess:
            # Elements with CSS app-region: drag become the window's real title bar (drag, snap, system menu).
            sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True

    def _on_size(self, maximized: bool) -> None:
        top = 0 if maximized else self.strip  # No resize strip when maximized.
        if self.form.Padding.Top != top:
            self.form.Padding = self._winforms.Padding(0, top, 0, 0)

    def command(self, action: str) -> None:
        self._wf.command(self.hwnd, action)

    def state(self) -> dict:
        return {"maximized": self._wf.is_maximized(self.hwnd)}

    def theme(self, dark: bool, color: str) -> None:
        from System.Drawing import ColorTranslator
        self._wf.set_colors(self.hwnd, dark, color)
        self.form.BeginInvoke(self._action(lambda: setattr(self.form, "BackColor", ColorTranslator.FromHtml(color))))


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
        if app.shutdown_requested.is_set():
            if proc.poll() is None:
                proc.terminate()
            return True
        if proc.poll() is not None and time.time() - app.last_seen > 20:
            return True
        if time.time() - app.last_seen > 180:
            return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="enoy", description="Enoy")
    parser.add_argument("--no-window", action="store_true", help="Run the server only (for development).")
    parser.add_argument("--browser", action="store_true", help="Open in the default browser instead of a native window.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--data-dir", help="Use this folder for settings, database and logs (default %%LOCALAPPDATA%%\\Enoy).")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["ENOY_HOME"] = os.path.abspath(args.data_dir)

    setup_logging(args.verbose)
    existing = running_instance()
    if existing and not args.no_window:
        message_box("Enoy is already running. Switch to its window (check the taskbar).")
        return 0

    app, orc = build()
    server = Server(app, args.port).start()
    _instance_file().write_text(json.dumps({"url": server.url, "pid": os.getpid(),
                                          "version": __version__, "source": str(Path(__file__).resolve())}),
                                encoding="utf-8")
    log.info("Enoy %s listening on %s", __version__, server.url)
    url = server.url
    try:
        if args.no_window:
            print(f"Enoy running at {url}  (Ctrl+C to stop)", flush=True)
            while not app.shutdown_requested.is_set():
                time.sleep(1)
            return 0
        if args.browser:
            import webbrowser
            app.folder_picker = tk_folder_picker
            webbrowser.open(url)
            while not app.shutdown_requested.is_set() and time.time() - app.last_seen < 180:
                time.sleep(2)
            return 0
        if open_with_pywebview(url, app):
            return 0
        if open_with_edge(url, app):
            return 0
        import webbrowser
        app.folder_picker = tk_folder_picker
        webbrowser.open(url)
        while not app.shutdown_requested.is_set() and time.time() - app.last_seen < 180:
            time.sleep(2)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()
        try:
            _instance_file().unlink()
        except OSError:
            pass
        orc.db.close()


if __name__ == "__main__":
    sys.exit(main())
