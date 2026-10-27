"""Integrated title bar for the desktop window (Windows 10/11, pywebview + WebView2).

The window stays a normal resizable Windows window: borders, Aero Snap, Win+arrow
keys, the system menu (Alt+Space), multi-monitor moves and DPI changes are all
Windows' own. Only the caption strip is removed:

- WM_NCCALCSIZE: the client area starts at the top of the window, so the native
  caption is gone. Left, right and bottom resize borders stay non-client. When
  maximized, the client area is pulled in by the frame size so nothing is cut off.
- The top 4 px of the client area are not covered by the web view; WM_NCHITTEST
  reports them as the top resize edge, so the window can be resized from the top.
- WebView2 "non-client region support": page elements with CSS `app-region: drag`
  act as the real title bar (drag, snap, double-click to maximize, right-click
  system menu). Buttons and inputs are `app-region: no-drag`.
- Minimize, maximize/restore and close are page buttons that post the standard
  WM_SYSCOMMAND messages, so Windows animates and handles them as usual.

Everything here is best effort: if a step fails, the window keeps its normal frame.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("enoy.winframe")

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_SIZE = 0x0005
WM_SYSCOMMAND = 0x0112
WM_EXITSIZEMOVE = 0x0232
WM_APP_FIXSIZE = 0x8000 + 0x107
SIZE_RESTORED, SIZE_MAXIMIZED = 0, 2
HTTOP, HTTOPLEFT, HTTOPRIGHT = 12, 13, 14
SC = {"minimize": 0xF020, "maximize": 0xF030, "restore": 0xF120, "close": 0xF060}
SWP_FRAMECHANGED, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER = 0x20, 0x2, 0x1, 0x4
SM_CXFRAME, SM_CYFRAME, SM_CXPADDEDBORDER = 32, 33, 92
DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWA_BORDER_COLOR = 20, 33, 34
TOP_STRIP = 4  # Logical pixels kept for resizing from the top edge.

LRESULT = ctypes.c_ssize_t
SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  ctypes.c_size_t, ctypes.c_size_t)


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_procs: dict[int, SUBCLASSPROC] = {}  # Kept alive as long as the window lives.


def _api():
    comctl = ctypes.windll.comctl32
    comctl.SetWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
    comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    comctl.DefSubclassProc.restype = LRESULT
    _user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    _user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]
    return comctl


def _scale(hwnd: int) -> float:
    try:
        return _user32.GetDpiForWindow(hwnd) / 96
    except Exception:
        return 1.0


def _frame(hwnd: int) -> tuple[int, int]:
    dpi = _user32.GetDpiForWindow(hwnd) or 96
    pad = _user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)
    return (_user32.GetSystemMetricsForDpi(SM_CXFRAME, dpi) + pad, _user32.GetSystemMetricsForDpi(SM_CYFRAME, dpi) + pad)


def is_maximized(hwnd: int) -> bool:
    return bool(_user32.IsZoomed(hwnd))


def command(hwnd: int, action: str) -> None:
    if action == "toggle":
        action = "restore" if is_maximized(hwnd) else "maximize"
    _user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC[action], 0)


def set_colors(hwnd: int, dark: bool, border_hex: str) -> None:
    """Dark-mode frame and a border colour that matches the app instead of the accent outline."""
    dwm = ctypes.windll.dwmapi
    val = ctypes.c_int(1 if dark else 0)
    dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), 4)
    corner = ctypes.c_int(2)  # Rounded corners (Windows 11; ignored on Windows 10).
    dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), 4)
    h = border_hex.lstrip("#")
    if len(h) == 6:
        colorref = ctypes.c_uint(int(h[4:6], 16) << 16 | int(h[2:4], 16) << 8 | int(h[0:2], 16))
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(colorref), 4)


def install(hwnd: int, on_size=None) -> None:
    """Remove the caption strip. Must run on the window's UI thread."""
    comctl = _api()
    # WinForms re-adds the old caption height when it restores from maximized; remember the
    # normal size ourselves and put it back after a restore.
    state = {"size": None, "was_max": False, "restore_to": None}

    def remember(h):
        if not is_maximized(h) and not _user32.IsIconic(h):
            rect = wintypes.RECT()
            _user32.GetWindowRect(h, ctypes.byref(rect))
            state["size"] = (rect.right - rect.left, rect.bottom - rect.top)

    def proc(h, msg, wparam, lparam, uid, ref):
        if msg == WM_NCCALCSIZE and wparam:
            params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
            top = params.rgrc[0].top
            result = comctl.DefSubclassProc(h, msg, wparam, lparam)  # Side and bottom borders as usual.
            params.rgrc[0].top = top + (_frame(h)[1] if is_maximized(h) else 0)
            return result
        if msg == WM_NCHITTEST:
            hit = comctl.DefSubclassProc(h, msg, wparam, lparam)
            if hit == 1 and not is_maximized(h):  # HTCLIENT: the uncovered strip at the very top.
                rect = wintypes.RECT()
                _user32.GetWindowRect(h, ctypes.byref(rect))
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                x = ctypes.c_short(lparam & 0xFFFF).value
                edge = int(TOP_STRIP * _scale(h))
                if y < rect.top + edge:
                    corner = int(16 * _scale(h))
                    return HTTOPLEFT if x < rect.left + corner else HTTOPRIGHT if x > rect.right - corner else HTTOP
            return hit
        if msg == WM_SIZE:
            if wparam == SIZE_MAXIMIZED:
                if not state["was_max"]:
                    state["restore_to"] = state["size"]  # The size before maximizing, frozen.
                state["was_max"] = True
            elif wparam == SIZE_RESTORED and state["was_max"]:
                state["was_max"] = False
                _user32.PostMessageW(h, WM_APP_FIXSIZE, 0, 0)
            elif wparam == SIZE_RESTORED and state["restore_to"] is None:
                remember(h)
            if on_size is not None:
                try:
                    on_size(is_maximized(h))
                except Exception:  # Never break the window's message loop.
                    log.debug("on_size failed", exc_info=True)
        elif msg == WM_EXITSIZEMOVE:
            remember(h)
        elif msg == WM_APP_FIXSIZE:
            size, state["restore_to"] = state["restore_to"], None
            if size and not is_maximized(h) and not _user32.IsIconic(h):
                _user32.SetWindowPos(h, None, 0, 0, size[0], size[1], SWP_NOMOVE | SWP_NOZORDER)
                state["size"] = size
            return 0
        return comctl.DefSubclassProc(h, msg, wparam, lparam)

    cb = SUBCLASSPROC(proc)
    _procs[hwnd] = cb
    if not comctl.SetWindowSubclass(hwnd, cb, 0xE107, 0):
        raise OSError("SetWindowSubclass failed")
    # A 1 px frame extension keeps DWM drawing the shadow and Windows 11's rounded corners.
    margins = (ctypes.c_int * 4)(0, 0, 1, 0)
    ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
    remember(hwnd)
