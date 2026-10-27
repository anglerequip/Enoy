"""Read-only hardware inventory from Windows itself (never guessed by a model).

Reads CPU name (registry), logical processors, RAM (GlobalMemoryStatusEx), GPUs
(nvidia-smi, else Windows' display adapter list), disks (free/total per drive),
Windows version and hardware usage (
memory). No documents, browser data, credentials or personal files are read.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import string
import subprocess
import sys
import time

from . import diagnostics

_CACHE: dict = {}


def cpu_name() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except OSError:
        return None


def windows_details() -> dict:
    out = {"name": diagnostics.windows_version(), "architecture": os.environ.get("PROCESSOR_ARCHITECTURE", "")}
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                for key in ("DisplayVersion", "EditionID"):
                    try:
                        out[key[0].lower() + key[1:]] = str(winreg.QueryValueEx(k, key)[0])
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def gpus() -> list[dict]:
    found = diagnostics.gpu_info()  # nvidia-smi: exact VRAM and usage.
    if found:
        return [{**g, "source": "nvidia-smi"} for g in found]
    if os.name != "nt":
        return []
    try:  # Other vendors: Windows' adapter list (VRAM reported by WMI caps at 4 GB, so it is left out).
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_VideoController).Name"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return [{"name": n.strip(), "source": "Windows display adapters"} for n in out.splitlines() if n.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def disks() -> list[dict]:
    out = []
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << i):
                continue
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # Fixed disks only (no network/removable).
                continue
            try:
                u = shutil.disk_usage(root)
                out.append({"drive": root, "total_gb": round(u.total / 2**30, 1), "free_gb": round(u.free / 2**30, 1)})
            except OSError:
                pass
    return out


def inventory(orchestrator=None, max_age_s: float = 30) -> dict:
    """Everything measured, with `source` notes. Cached briefly (it runs a couple of commands)."""
    if _CACHE.get("at", 0) > time.time() - max_age_s:
        inv = dict(_CACHE["inv"])
    else:
        inv = {
            "cpu": {"name": cpu_name(), "logical_processors": os.cpu_count()},
            "memory": diagnostics.memory_gb(),
            "gpus": gpus(),
            "disks": disks(),
            "windows": windows_details(),
            "python": sys.version.split()[0],
        }
        _CACHE.update(at=time.time(), inv=dict(inv))
    return inv


def as_text(inv: dict) -> str:
    """Plain facts for the model's context."""
    lines = []
    cpu = inv.get("cpu", {})
    lines.append(f"CPU: {cpu.get('name') or 'unknown'} ({cpu.get('logical_processors')} logical processors)")
    mem = inv.get("memory") or {}
    if mem:
        lines.append(f"RAM: {mem.get('total_gb')} GB total, {mem.get('free_gb')} GB free right now")
    for g in inv.get("gpus", []):
        if "vram_total_mb" in g:
            lines.append(f"GPU: {g['name']}, {g['vram_total_mb']:,} MB VRAM ({g['vram_used_mb']:,} MB in use), driver {g['driver']}")
        else:
            lines.append(f"GPU: {g['name']} (VRAM not reported by Windows for this adapter)")
    for d in inv.get("disks", []):
        lines.append(f"Disk {d['drive']}: {d['free_gb']} GB free of {d['total_gb']} GB")
    w = inv.get("windows", {})
    lines.append(f"Windows: {w.get('name')} {w.get('displayVersion', '')} {w.get('editionID', '')} {w.get('architecture', '')}".strip())
    return "\n".join(lines)
