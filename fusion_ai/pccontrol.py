"""PC access: permission profiles and the desktop actions Enoy can take.

Permissions are granular capabilities, checked in the backend right before each
action (hiding a button is not protection). Text written by a model is never
treated as permission: actions come only from what the user typed, matched by
fixed patterns below, and every action and refusal is logged in Activity.

Implemented (Windows APIs through ctypes, no extra packages):
  hardware.read     read-only hardware inventory
  processes.read    names and memory of running programs (no command lines)
  windows.observe   list visible windows (title, program)
  windows.control   focus, minimize, maximize, restore a window
  browser.navigate  open an http(s) address in the default browser
  apps.open         start a program from your approved list
Not implemented (shown as unavailable, never faked): clicking and typing in other
apps, reading other apps' controls, browser form filling and logins, screenshots,
changing system settings.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from urllib.parse import urlparse

from . import winproc

CAPABILITIES = {
    "hardware.read": ("Read hardware inventory", "CPU, RAM, GPU, disks, Windows version, local model runtime. Read-only.", True),
    "processes.read": ("See running programs", "Program names and memory use. No command lines or window contents.", True),
    "windows.observe": ("See open windows", "Window titles and which program owns them.", True),
    "windows.control": ("Switch between windows", "Focus, minimize, maximize or restore a window.", True),
    "browser.navigate": ("Open web pages", "Opens an http(s) address in your default browser. Nothing is typed or submitted.", True),
    "apps.open": ("Open approved programs", "Only programs on your approved list.", True),
    "desktop.interact": ("Click and type in other apps", "Not available in this version.", False),
    "browser.fill_forms": ("Fill in web forms and log in", "Not available in this version. Enoy never handles passwords.", False),
    "screen.capture": ("Take screenshots", "Not available in this version.", False),
    "system.settings.modify": ("Change system settings", "Never allowed.", False),
}
OBSERVE = {"hardware.read", "processes.read", "windows.observe"}
ASSIST = OBSERVE | {"windows.control", "browser.navigate", "apps.open"}
PROFILES = {
    "observe": ("Observe", OBSERVE, "Read-only: hardware, running programs and open windows."),
    "assist": ("Assist", ASSIST, "Also opens web pages and approved programs and switches windows."),
    "automation": ("Authorized automation", ASSIST,
                   "Same actions as Assist in this version: multi-step automation (clicking, typing, forms) is not built yet."),
}


class PermissionDenied(Exception):
    def __init__(self, capability: str, message: str):
        super().__init__(message)
        self.capability = capability


def allowed(cfg: dict) -> set[str]:
    caps = set(PROFILES[cfg["pc"]["profile"]][1])
    if not cfg["pc"].get("hardware_read", True):
        caps.discard("hardware.read")
    return caps


def check(cfg: dict, capability: str) -> None:
    """Raise PermissionDenied unless the current profile grants `capability`. Called before every action."""
    label, _, implemented = CAPABILITIES[capability]
    if not implemented:
        raise PermissionDenied(capability, f"{label} is not available in Enoy.")
    if capability not in allowed(cfg):
        profile = PROFILES[cfg["pc"]["profile"]][0]
        if capability == "hardware.read":
            hint = "Turn on 'Read hardware inventory' in Settings → Memory & PC."
        else:
            hint = "It needs the Assist profile: Settings → Memory & PC."
        raise PermissionDenied(capability, f"{label} is not allowed in the {profile} profile. {hint}")


def summary(cfg: dict) -> dict:
    caps = allowed(cfg)
    return {
        "profile": cfg["pc"]["profile"],
        "profiles": {k: {"label": v[0], "desc": v[2]} for k, v in PROFILES.items()},
        "capabilities": [{"id": k, "label": v[0], "desc": v[1], "implemented": v[2], "allowed": k in caps}
                         for k, v in CAPABILITIES.items()],
        "approved_apps": cfg["pc"].get("approved_apps", []),
    }


# ---------------------------------------------------------------- windows

@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    program: str
    state: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


if winproc.IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND

SW = {"minimize": 6, "maximize": 3, "restore": 9}


def list_windows() -> list[Window]:
    if not winproc.IS_WINDOWS:
        return []
    names = {p.pid: p.name for p in winproc.list_processes()}
    found: list[Window] = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, 4):  # Skip hidden and owned (popup) windows.
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        state = "minimized" if _user32.IsIconic(hwnd) else "maximized" if _user32.IsZoomed(hwnd) else "normal"
        if buf.value not in ("Program Manager",):
            found.append(Window(int(hwnd), buf.value, pid.value, names.get(pid.value, "?"), state))
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return found


def find_window(query: str) -> Window | None:
    q = query.lower().strip()
    wins = list_windows()
    for w in wins:  # Exact program name first (e.g. "notepad"), then title text.
        if w.program.lower() in (q, q + ".exe"):
            return w
    for w in wins:
        if q in w.title.lower():
            return w
    return None


def window_action(action: str, hwnd: int) -> None:
    if not winproc.IS_WINDOWS or not _user32.IsWindow(hwnd):
        raise ValueError("That window no longer exists.")
    if action == "focus":
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW["restore"])
        _user32.SetForegroundWindow(hwnd)  # Windows may only flash the taskbar button if another app has focus.
    elif action in SW:
        _user32.ShowWindow(hwnd, SW[action])
    else:
        raise ValueError(f"Unknown window action: {action}")


def processes(limit: int = 25) -> list[dict]:
    """Running programs by memory. Names and numbers only; command lines can hold secrets."""
    rows = []
    for p in winproc.list_processes():
        winproc.measure(p)
        if p.private_ws:
            rows.append({"pid": p.pid, "name": p.name, "memory_mib": round(p.private_ws / winproc.MIB)})
    rows.sort(key=lambda r: -r["memory_mib"])
    return rows[:limit]


# ---------------------------------------------------------------- opening things

def check_url(url: str) -> str:
    raw = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise ValueError("Only http and https addresses can be opened.")
    if u.username or u.password or "@" in u.netloc:
        raise ValueError("Addresses with a user name or password in them are not opened.")
    if not u.hostname or "." not in u.hostname and u.hostname != "localhost":
        raise ValueError("That does not look like a web address.")
    return raw


def open_url(url: str) -> str:
    safe = check_url(url)
    webbrowser.open(safe, new=2)
    return safe


def approved_app(cfg: dict, name: str) -> dict | None:
    q = name.lower().strip()
    for app in cfg["pc"].get("approved_apps", []):
        if app["name"].lower() == q or os.path.splitext(os.path.basename(app["path"]))[0].lower() == q:
            return app
    return None


def open_app(app: dict) -> None:
    # No shell: the approved path is started as is, without arguments.
    subprocess.Popen([app["path"]], close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


# ---------------------------------------------------------------- what the user asked for

_HW = r"(hardware|specs?|gpu|graphics card|video card|vram|cpu|processor|ram|memory|disk space|storage|cores|windows version)"
_MINE = r"(do i have|have i got|i have|does (this|my) (pc|computer|laptop|machine) have|is (in|on) (this|my) (pc|computer|laptop|machine)|on (this|my) (pc|computer|laptop|machine)|in my (pc|computer|laptop|machine)|my (pc|computer|laptop|machine|system))"
HARDWARE_Q = re.compile(
    rf"\b(what|which|how much|how many)\b[^.?!\n]{{0,40}}\b{_HW}\b[^.?!\n]{{0,40}}\b{_MINE}\b"
    rf"|\bwhat('?s| is| are)\s+(my|this (pc|computer)'?s?)\s+(pc\s+|computer\s+)?{_HW}\b"
    rf"|\b(show|list|tell me|check)\b[^.?!\n]{{0,20}}\b(my|this pc'?s?)\s+(pc\s+|computer\s+|system\s+)?{_HW}\b"
    rf"|\b(my|this (pc|computer)'?s?)\s+(pc\s+|system\s+|computer\s+)?specs\b", re.I)
URL_RE = re.compile(r"^(?:please\s+)?(?:open|go to|navigate to|visit|browse to)\s+(?:the\s+)?(?:website\s+|page\s+)?"
                    r"((?:https?://)?[\w-]+(?:\.[\w-]+)+(?::\d+)?(?:/\S*)?)\s*[.!]?$", re.I)
# Bare names like "main.py" are files, not sites: without http(s):// only these endings count.
SITE_TLDS = {"com", "org", "net", "io", "dev", "ai", "app", "se", "uk", "de", "no", "dk", "fi", "eu", "gg", "tv", "me",
             "co", "us", "info", "edu", "gov", "xyz", "site", "online", "fr", "nl", "es", "it", "ca", "au", "jp"}
KNOWN_APPS = {"notepad", "calculator", "calc", "paint", "chrome", "edge", "firefox", "explorer", "file explorer", "spotify",
              "discord", "steam", "word", "excel", "powerpoint", "outlook", "teams", "vscode", "vs code", "code",
              "visual studio code", "terminal", "powershell", "cmd", "roblox studio", "obs", "task manager", "settings"}


def looks_like_site(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == "localhost" or host.rsplit(".", 1)[-1].lower() in SITE_TLDS
APP_RE = re.compile(r"^(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+)?(?:app\s+|program\s+)?([\w][\w .+-]{0,40}?)\s*[.!]?$", re.I)
WIN_RE = re.compile(r"^(?:please\s+)?(focus|switch to|bring up|minimi[sz]e|maximi[sz]e|restore)\s+(?:the\s+)?(?:window\s+)?(.+?)"
                    r"(?:\s+window)?\s*[.!]?$", re.I)
LIST_WIN_RE = re.compile(r"\b(what|which|list|show)\b.*\b(windows|apps|programs|applications)\b.*\b(open|running)\b"
                         r"|\blist (my |the )?(open )?windows\b", re.I)
PROC_RE = re.compile(r"\b(what|which)\b.*\b(using|uses|eating)\b.*\b(memory|ram)\b|\btop (processes|programs)\b", re.I)


def detect(text: str) -> tuple[str, str] | None:
    """A PC request typed by the user, or None. Only short, direct requests count."""
    t = text.strip()
    if len(t) > 300 or "\n" in t.strip():
        return None
    if m := URL_RE.match(t):
        return ("open_url", m.group(1))
    if m := WIN_RE.match(t):
        verb = m.group(1).lower()
        action = "focus" if verb in ("focus", "switch to", "bring up") else "minimize" if verb.startswith("minimi") \
            else "maximize" if verb.startswith("maximi") else "restore"
        return (action, m.group(2))
    if LIST_WIN_RE.search(t):
        return ("list_windows", "")
    if PROC_RE.search(t):
        return ("processes", "")
    if HARDWARE_Q.search(t):
        return ("hardware", "")
    if m := APP_RE.match(t):
        return ("open_app", m.group(1))
    return None
