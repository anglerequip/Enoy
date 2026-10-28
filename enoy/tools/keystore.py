"""API keys for optional providers, kept in Windows Credential Manager.

Keys never go into config.json, the database, logs, prompts or the UI: the UI
can only set, clear, or ask whether a key is set. Each key is a generic
credential named "Enoy/<name>" for the current Windows user. Names are the fixed
tool keys in NAMES or "provider:<id>" for Build workspace model providers.
"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes

NAMES = ("brave_search",)
_PROVIDER = re.compile(r"provider:[a-z0-9_-]{1,40}")
_PREFIX = "Enoy/"
_GENERIC = 1
_PERSIST_LOCAL = 2


class _CRED(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _api():
    if os.name != "nt":
        return None
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CRED))]
    adv.CredWriteW.argtypes = [ctypes.POINTER(_CRED), wintypes.DWORD]
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return adv


def valid(name: str) -> bool:
    return name in NAMES or bool(_PROVIDER.fullmatch(name or ""))


def _read(target: str) -> str | None:
    adv = _api()
    if adv is None:
        return None
    ptr = ctypes.POINTER(_CRED)()
    if not adv.CredReadW(target, _GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        c = ptr.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
    finally:
        adv.CredFree(ptr)


def _write(target: str, blob: bytes) -> None:
    adv = _api()
    if adv is None:
        raise RuntimeError("Saving keys needs Windows Credential Manager.")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = _CRED(Type=_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                 CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)), Persist=_PERSIST_LOCAL,
                 UserName="Enoy", Comment="API key used by Enoy")
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "Windows did not save the key.")


def _remove(target: str) -> None:
    adv = _api()
    if adv is not None:
        adv.CredDeleteW(target, _GENERIC, 0)


def get(name: str) -> str | None:
    return _read(_PREFIX + name) if valid(name) else None


def put(name: str, value: str) -> None:
    if not valid(name):
        raise ValueError("Unknown key name.")
    blob = value.strip().encode("utf-8")
    if not blob or len(blob) > 2000:
        raise ValueError("The key is empty or too long.")
    _write(_PREFIX + name, blob)


def delete(name: str) -> None:
    if valid(name):
        _remove(_PREFIX + name)


def status() -> dict:
    return {n: get(n) is not None for n in NAMES}


def provider_status(ids) -> dict:
    """Whether each Build provider has a key saved. Never the key itself."""
    return {i: get("provider:" + i) is not None for i in ids}
