"""System and backend diagnostics shown on the Diagnostics screen."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys

from . import __version__, paths


def memory_gb() -> dict:
    if os.name != "nt":
        return {}

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return {}
    return {"total_gb": round(stat.ullTotalPhys / 2**30, 1), "free_gb": round(stat.ullAvailPhys / 2**30, 1)}


def gpu_info() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            gpus.append({"name": parts[0], "vram_total_mb": int(parts[1]), "vram_used_mb": int(parts[2]), "driver": parts[3]})
    return gpus


def windows_version() -> str:
    if os.name != "nt":
        return platform.platform()
    ver = sys.getwindowsversion()
    name = "Windows 11" if ver.build >= 22000 else "Windows 10" if ver.major == 10 else "Windows"
    return f"{name} (build {ver.build})"


def collect(orchestrator, config_warnings: list[str]) -> dict:
    from .backends.base import CLAUDE, LOCAL

    local = orchestrator.backends[LOCAL].status().as_dict()
    claude = orchestrator.backends[CLAUDE].status().as_dict() if CLAUDE in orchestrator.backends else None
    advice = []
    if not local["available"]:
        if "Cannot reach" in local["detail"]:
            advice.append("Ollama is not running. Start it from the Start menu (Ollama), then press Refresh.")
        elif "not installed" in local["detail"]:
            advice.append("The configured model is not installed. Choose an installed model in Settings, "
                          "or download it yourself in a terminal: ollama pull <model>.")
    loaded = (local.get("extra") or {}).get("loaded")
    if loaded and loaded.get("gpu_percent") is not None and loaded["gpu_percent"] < 100:
        advice.append(f"Only {loaded['gpu_percent']}% of the model is in GPU memory; the rest runs on the CPU, which makes "
                      "answers several times slower. Other programs also use GPU memory (games, Discord, Spotify, browsers, "
                      "screen recorders such as Medal); closing them before starting a task frees room. A smaller context "
                      "size (Settings > Local model) also helps a little.")
    if claude and not claude["available"]:
        advice.append("Claude is optional. Enoy keeps working locally without it.")
    log_file = paths.logs_dir() / "fusion.log"
    tail = ""
    if log_file.exists():
        with open(log_file, encoding="utf-8", errors="replace") as fh:
            tail = "".join(fh.readlines()[-60:])
    return {
        "app_version": __version__,
        "python": sys.version.split()[0],
        "os": windows_version(),
        "memory": memory_gb(),
        "gpus": gpu_info(),
        "cpu_count": os.cpu_count(),
        "local": local,
        "claude": claude,
        "config_warnings": config_warnings,
        "paths": {
            "data": str(paths.app_home()),
            "config": str(paths.config_path()),
            "database": str(paths.database_path()),
            "logs": str(paths.logs_dir()),
            "file_backups": str(paths.backups_dir()),
            "config_backups": str(paths.config_backups_dir()),
        },
        "advice": advice,
        "log_tail": tail,
    }
