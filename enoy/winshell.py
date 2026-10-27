"""Make Windows show Enoy, not Python, on the taskbar.

Enoy runs inside pythonw.exe, so by default Windows groups its window with Python
(name, icon, jump list). Three things fix that:

1. the process gets its own AppUserModelID (``APP_ID``);
2. the Enoy shortcuts carry the same ID, so the taskbar takes the name, icon and
   "Pin to taskbar" target from the Start menu shortcut;
3. the window icon is set to ``launch/enoy.ico``.

Everything here is best effort: a failure is logged and never stops the app.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("enoy.winshell")

APP_ID = "Enoy.Desktop"
ICON = Path(__file__).resolve().parent.parent / "launch" / "enoy.ico"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("pwszVal", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]


VT_LPWSTR = 31
GPS_READWRITE = 2
IID_IPropertyStore = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
PKEY_AppUserModel_ID = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 5)

WM_SETICON = 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010


def set_app_id() -> bool:
    """Give this process its own taskbar identity. Call before any window opens."""
    if os.name != "nt":
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
        return True
    except Exception as exc:
        log.info("Could not set the taskbar app ID: %s", exc)
        return False


def _set_string(store: ctypes.c_void_p, key: tuple[str, int], value: str) -> None:
    # IPropertyStore vtable: 0 QueryInterface, 1 AddRef, 2 Release, 3 GetCount,
    # 4 GetAt, 5 GetValue, 6 SetValue, 7 Commit.
    vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    set_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                   ctypes.POINTER(PROPVARIANT))(vtbl[6])
    pkey = PROPERTYKEY(GUID(key[0]), key[1])
    pv = PROPVARIANT(vt=VT_LPWSTR, pwszVal=value)  # SetValue copies the string.
    set_value(store, ctypes.byref(pkey), ctypes.byref(pv))


def _call(store: ctypes.c_void_p, index: int) -> None:
    vtbl = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtbl[index])(store)


def tag_shortcut(lnk: str | Path) -> bool:
    """Store APP_ID in a .lnk file so the taskbar links the window to it."""
    if os.name != "nt" or not Path(lnk).is_file():
        return False
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)  # Apartment threaded; S_FALSE if already done.
    store = ctypes.c_void_p()
    try:
        ctypes.oledll.shell32.SHGetPropertyStoreFromParsingName(
            ctypes.c_wchar_p(str(lnk)), None, GPS_READWRITE, ctypes.byref(GUID(IID_IPropertyStore)),
            ctypes.byref(store))
        _set_string(store, PKEY_AppUserModel_ID, APP_ID)
        _call(store, 7)  # Commit
        return True
    except OSError as exc:
        log.info("Could not tag shortcut %s: %s", lnk, exc)
        return False
    finally:
        if store:
            _call(store, 2)  # Release


def set_window_icon(hwnd: int, icon: Path = ICON) -> bool:
    """Replace the Python icon pywebview puts on the window."""
    if os.name != "nt" or not hwnd or not icon.is_file():
        return False
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    ok = False
    for which, metric in ((1, 11), (0, 49)):  # ICON_BIG / SM_CXICON, ICON_SMALL / SM_CXSMICON
        size = user32.GetSystemMetrics(metric)
        handle = user32.LoadImageW(None, str(icon), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if handle:
            user32.SendMessageW(hwnd, WM_SETICON, which, handle)
            ok = True
    return ok


def default_shortcuts() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", ""))
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
    return [appdata / "Microsoft/Windows/Start Menu/Programs/Enoy.lnk", desktop / "Enoy.lnk"]


if __name__ == "__main__":
    # Used by setup.ps1: python -m enoy.winshell <shortcut.lnk> ...
    targets = [Path(a) for a in sys.argv[1:]] or default_shortcuts()
    for t in targets:
        print(("tagged " if tag_shortcut(t) else "skipped ") + str(t))
