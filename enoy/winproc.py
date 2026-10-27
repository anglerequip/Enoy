"""Read-only Windows process information through the Win32 API (ctypes, no extra packages).

Used to find the local model's processes and measure their memory. Nothing here
changes or ends a process except `terminate` and `cap_working_set`, which the memory
controls use the measured process inventory.

Memory metric: the **private working set**, the same number Task Manager shows in
its "Memory" column: private memory that currently sits in RAM. Two other numbers
are reported but not used for the limit: the private commit (memory promised to
the process, much of it never touched or held as pinned GPU staging memory) and
the total working set (which also counts shared, memory-mapped model file pages).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

MIB = 1024 * 1024
IS_WINDOWS = os.name == "nt"

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400
QUOTA_LIMITS_HARDWS_MIN_DISABLE = 0x2
QUOTA_LIMITS_HARDWS_MAX_ENABLE = 0x4
QUOTA_LIMITS_HARDWS_MAX_DISABLE = 0x8


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    name: str
    path: str = ""
    private_ws: int | None = None   # Bytes. Task Manager's "Memory" column.
    private_commit: int | None = None
    working_set: int | None = None
    peak_working_set: int | None = None

    def as_dict(self) -> dict:
        mb = lambda v: None if v is None else round(v / MIB, 1)
        return {"pid": self.pid, "ppid": self.ppid, "name": self.name, "path": self.path,
                "private_ws_mib": mb(self.private_ws), "private_commit_mib": mb(self.private_commit),
                "working_set_mib": mb(self.working_set), "peak_working_set_mib": mb(self.peak_working_set)}


if IS_WINDOWS:
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    class PROCESS_MEMORY_COUNTERS_EX2(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t), ("PrivateWorkingSetSize", ctypes.c_size_t),
                    ("SharedCommitUsage", ctypes.c_ulonglong)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    _k32.GetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                                ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(wintypes.DWORD)]


def list_processes() -> list[ProcInfo]:
    """All processes: pid, parent pid and exe name. No memory figures (cheap)."""
    if not IS_WINDOWS:
        return []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(ProcInfo(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def measure(proc: ProcInfo) -> ProcInfo:
    """Fill in path and memory. Leaves them None if Windows denies access."""
    if not IS_WINDOWS:
        return proc
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, proc.pid)
    if not h:
        return proc
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            proc.path = buf.value
        pmc = PROCESS_MEMORY_COUNTERS_EX2()
        pmc.cb = ctypes.sizeof(pmc)
        if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            proc.private_ws = pmc.PrivateWorkingSetSize
            proc.private_commit = pmc.PrivateUsage
            proc.working_set = pmc.WorkingSetSize
            proc.peak_working_set = pmc.PeakWorkingSetSize
    finally:
        _k32.CloseHandle(h)
    return proc


def descendants(root_pid: int, procs: list[ProcInfo]) -> list[ProcInfo]:
    children: dict[int, list[ProcInfo]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p)
    out, todo, seen = [], [root_pid], {root_pid}
    while todo:
        for c in children.get(todo.pop(), []):
            if c.pid not in seen:  # PIDs are reused; guard against loops.
                seen.add(c.pid)
                out.append(c)
                todo.append(c.pid)
    return out


def terminate(pid: int) -> bool:
    if not IS_WINDOWS:
        return False
    h = _k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        return bool(_k32.TerminateProcess(h, 1))
    finally:
        _k32.CloseHandle(h)


def cap_working_set(pid: int, max_bytes: int) -> int | None:
    """Windows hard maximum on a process's working set: the RAM it may hold. Returns the cap
    Windows reports afterwards (bytes), or None if Windows refused.

    Pages above the cap are moved out by Windows (to the page file); the process keeps
    running, possibly slower. The cap lasts until the process ends. It does not limit
    commit or page file use.
    """
    if not IS_WINDOWS:
        return None
    h = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    try:
        if not _k32.SetProcessWorkingSetSizeEx(h, MIB, max_bytes,
                                               QUOTA_LIMITS_HARDWS_MIN_DISABLE | QUOTA_LIMITS_HARDWS_MAX_ENABLE):
            return None
        lo, hi, flags = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
        if not _k32.GetProcessWorkingSetSizeEx(h, ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(flags)):
            return None
        return hi.value if flags.value & QUOTA_LIMITS_HARDWS_MAX_ENABLE else None
    finally:
        _k32.CloseHandle(h)


def release_working_set_cap(pid: int) -> bool:
    """Remove a hard working-set maximum set by `cap_working_set`."""
    if not IS_WINDOWS:
        return False
    h = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    try:
        lo, hi, flags = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
        if not _k32.GetProcessWorkingSetSizeEx(h, ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(flags)):
            return False
        return bool(_k32.SetProcessWorkingSetSizeEx(h, lo.value, hi.value,
                                                    QUOTA_LIMITS_HARDWS_MIN_DISABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE))
    finally:
        _k32.CloseHandle(h)
