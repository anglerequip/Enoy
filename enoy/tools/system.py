"""Read-only PC information tools, answered by Windows itself.

One PowerShell/CIM query (no administrator rights) collects the static parts:
CPU, motherboard and BIOS, memory modules, graphics adapters, physical disks,
Windows. Live values come from Windows APIs at call time: memory in use
(GlobalMemoryStatusEx), free space per drive, GPU memory in use (nvidia-smi
when present). VRAM: nvidia-smi, else the display driver's registry value
(WMI's AdapterRAM stops at 4 GB, so it is never used for the total).

Nothing is changed and nothing is guessed: a value Windows does not report is
returned as null with a note. Permission: the PC profile's "hardware.read".
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from .. import diagnostics, hardware
from .registry import Tool, ToolContext, ToolError

PERMISSION = "pc:hardware.read"
_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$o = [ordered]@{}
$o.cpu = @(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,L2CacheSize,L3CacheSize)
$o.board = @(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,Version)
$o.bios = @(Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,@{n='ReleaseDate';e={if ($_.ReleaseDate) { $_.ReleaseDate.ToString('yyyy-MM-dd') }}})
$o.system = @(Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory)
$o.ram = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,DeviceLocator,SMBIOSMemoryType)
$o.slots = @(Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MemoryDevices,MaxCapacityEx)
$o.gpu = @(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,DriverVersion,@{n='DriverDate';e={if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') }}},CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
$o.vram = @(Get-ItemProperty 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -Name 'HardwareInformation.qwMemorySize','DriverDesc' | Select-Object DriverDesc,'HardwareInformation.qwMemorySize')
$o.disks = @(Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size,HealthStatus)
$o.os = @(Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,@{n='InstallDate';e={if ($_.InstallDate) { $_.InstallDate.ToString('yyyy-MM-dd') }}},@{n='LastBoot';e={if ($_.LastBootUpTime) { $_.LastBootUpTime.ToString('s') }}})
$o | ConvertTo-Json -Depth 4 -Compress
"""
_lock = threading.Lock()
_cache: dict = {}
MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "LPDDR5"}


def _gib(b) -> float | None:
    return round(int(b) / 2**30, 1) if b else None


def _collect(cancel=None) -> dict:
    """Static inventory from Windows (cached for 10 minutes; parts do not change while Enoy runs)."""
    with _lock:
        if _cache.get("at", 0) > time.time() - 600:
            return _cache["data"]
        if os.name != "nt":
            raise ToolError("unavailable", "Hardware inspection is implemented for Windows only.")
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise ToolError("timeout", "Windows did not report the hardware within 40 seconds.") from exc
        except OSError as exc:
            raise ToolError("unavailable", f"Could not start PowerShell to ask Windows: {exc}") from exc
        try:
            raw = json.loads(proc.stdout.strip() or "{}")
        except ValueError as exc:
            raise ToolError("failed", "Windows' hardware report could not be read: "
                            + (proc.stderr.strip()[:200] or proc.stdout.strip()[:200])) from exc
        if not raw.get("cpu"):
            raise ToolError("failed", "Windows returned no hardware data. " + proc.stderr.strip()[:200])
        data = {"raw": raw, "collected": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        _cache.update(at=time.time(), data=data)
        return data


def _first(raw: dict, key: str) -> dict:
    items = raw.get(key) or []
    return items[0] if items else {}


def cpu(raw: dict) -> dict:
    c = _first(raw, "cpu")
    return {"name": (c.get("Name") or "").strip() or None, "manufacturer": c.get("Manufacturer"),
            "cores": c.get("NumberOfCores"), "logical_processors": c.get("NumberOfLogicalProcessors"),
            "max_clock_mhz": c.get("MaxClockSpeed"), "l2_cache_kb": c.get("L2CacheSize"),
            "l3_cache_kb": c.get("L3CacheSize"), "sockets": len(raw.get("cpu") or [])}


def memory(raw: dict) -> dict:
    live = diagnostics.memory_gb()
    sticks = [{"slot": (r.get("DeviceLocator") or "").strip(), "size_gib": _gib(r.get("Capacity")),
               "speed_mt_s": r.get("ConfiguredClockSpeed") or r.get("Speed"),
               "type": MEMORY_TYPES.get(r.get("SMBIOSMemoryType")),
               "manufacturer": (r.get("Manufacturer") or "").strip() or None,
               "part_number": (r.get("PartNumber") or "").strip() or None} for r in raw.get("ram") or []]
    slots = _first(raw, "slots").get("MemoryDevices")
    total = live.get("total_gb")
    free = live.get("free_gb")
    return {"installed_gib": round(sum(s["size_gib"] or 0 for s in sticks), 1) or None,
            "usable_by_windows_gib": total, "in_use_gib": round(total - free, 1) if total and free is not None else None,
            "available_gib": free, "in_use_percent": round((total - free) / total * 100) if total and free is not None else None,
            "modules": sticks, "slots_total": slots,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": "in_use/available are measured now; installed is the sum of the modules Windows reports."}


def gpus(raw: dict) -> list[dict]:
    smi = {g["name"]: g for g in diagnostics.gpu_info()}
    reg = {(v.get("DriverDesc") or ""): v.get("HardwareInformation.qwMemorySize") for v in raw.get("vram") or []}
    out = []
    for g in raw.get("gpu") or []:
        name = (g.get("Name") or "").strip()
        s = smi.get(name)
        total_mb = s["vram_total_mb"] if s else (round(int(reg[name]) / 2**20) if reg.get(name) else None)
        out.append({
            "name": name, "vendor": g.get("AdapterCompatibility"), "driver_version": g.get("DriverVersion"),
            "driver_date": g.get("DriverDate"),
            "vram_total_mib": total_mb,
            "vram_used_mib": s["vram_used_mb"] if s else None,
            "vram_source": "nvidia-smi" if s else "display driver (registry)" if total_mb else
                           "not reported by Windows for this adapter",
            "nvidia_driver": s["driver"] if s else None,
            "display": (f"{g['CurrentHorizontalResolution']}x{g['CurrentVerticalResolution']} @ {g.get('CurrentRefreshRate')} Hz"
                        if g.get("CurrentHorizontalResolution") else None),
        })
    return out


def storage(raw: dict) -> dict:
    phys = [{"model": (d.get("FriendlyName") or "").strip(), "type": d.get("MediaType") or None,
             "bus": d.get("BusType"), "size_gb": round(int(d["Size"]) / 1e9) if d.get("Size") else None,
             "health": d.get("HealthStatus")} for d in raw.get("disks") or []]
    vols = [{"drive": d["drive"], "total_gib": d["total_gb"], "free_gib": d["free_gb"]} for d in hardware.disks()]
    return {"physical_disks": phys, "volumes": vols,
            "note": "Disk sizes in GB (10^9 bytes, as on the label); volumes in GiB (2^30 bytes, as Windows shows)."}


def board(raw: dict) -> dict:
    b, bios, sysm = _first(raw, "board"), _first(raw, "bios"), _first(raw, "system")
    return {"system_manufacturer": sysm.get("Manufacturer"), "system_model": sysm.get("Model"),
            "motherboard_manufacturer": b.get("Manufacturer"), "motherboard_model": b.get("Product"),
            "motherboard_version": b.get("Version"), "bios_vendor": bios.get("Manufacturer"),
            "bios_version": bios.get("SMBIOSBIOSVersion"), "bios_date": bios.get("ReleaseDate")}


def windows(raw: dict) -> dict:
    o = _first(raw, "os")
    extra = hardware.windows_details()
    return {"edition": o.get("Caption"), "version": o.get("Version"), "build": o.get("BuildNumber"),
            "release": extra.get("displayVersion"), "architecture": o.get("OSArchitecture"),
            "installed": o.get("InstallDate"), "last_boot": o.get("LastBoot")}


def _with_meta(data: dict, collected: str) -> dict:
    data["retrieved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data["static_parts_collected_at"] = collected
    data["source"] = "Windows (CIM/WMI, registry, GlobalMemoryStatusEx, nvidia-smi), read-only"
    return data


def _section(fn):
    def run(args: dict, ctx: ToolContext) -> dict:
        info = _collect(ctx.cancel)
        out = fn(info["raw"])
        return _with_meta(out if isinstance(out, dict) else {"items": out}, info["collected"])
    return run


def _inventory(args: dict, ctx: ToolContext) -> dict:
    info = _collect(ctx.cancel)
    raw = info["raw"]
    return _with_meta({"computer": board(raw), "cpu": cpu(raw), "memory": memory(raw), "gpus": gpus(raw),
                       "storage": storage(raw), "windows": windows(raw)}, info["collected"])


def _summary(args: dict, ctx: ToolContext) -> dict:
    info = _collect(ctx.cancel)
    raw = info["raw"]
    m, b = memory(raw), board(raw)
    return _with_meta({
        "computer": f"{b['system_manufacturer'] or ''} {b['system_model'] or ''}".strip() or None,
        "cpu": cpu(raw)["name"], "ram_gib": m["installed_gib"], "ram_in_use_gib": m["in_use_gib"],
        "gpus": [f"{g['name']} ({g['vram_total_mib']:,} MiB VRAM)" if g["vram_total_mib"] else g["name"] for g in gpus(raw)],
        "disks": [f"{d['model']} {d['size_gb']} GB {d['type'] or ''}".strip() for d in storage(raw)["physical_disks"]],
        "windows": f"{windows(raw)['edition']} {windows(raw)['release'] or ''} build {windows(raw)['build']}".strip(),
    }, info["collected"])


def _tool(name, desc, fn, output, params=None) -> Tool:
    return Tool(name=name, description=desc, run=fn, output=output,
                parameters={"type": "object", "properties": params or {}, "required": []},
                permission=PERMISSION, timeout_s=45, cache_ttl_s=0)


SYSTEM_TOOLS = [
    _tool("get_hardware_inventory", "Every part of this PC that Windows reports: computer model, motherboard and BIOS, "
          "CPU, memory modules and usage, graphics cards with VRAM, disks and free space, Windows version. Use it to "
          "list the parts in the PC.", _inventory, "computer, cpu, memory, gpus, storage, windows"),
    _tool("get_system_summary", "One-line overview of this PC: model, CPU, RAM, GPUs with VRAM, disks, Windows.",
          _summary, "computer, cpu, ram_gib, ram_in_use_gib, gpus, disks, windows"),
    _tool("get_cpu_info", "This PC's processor: name, maker, cores, threads, clock speed, cache.", _section(cpu),
          "name, manufacturer, cores, logical_processors, max_clock_mhz, caches"),
    _tool("get_gpu_info", "This PC's graphics cards: name, driver version and date, VRAM total and in use.",
          _section(gpus), "items: name, driver, vram_total_mib, vram_used_mib, vram_source"),
    _tool("get_memory_info", "This PC's RAM: installed amount, memory modules and slots, and how much is in use now.",
          _section(memory), "installed_gib, in_use_gib, available_gib, in_use_percent, modules, slots_total"),
    _tool("get_storage_info", "This PC's disks (model, SSD/HDD, bus, size, health) and free space per drive.",
          _section(storage), "physical_disks, volumes"),
    _tool("get_motherboard_info", "This PC's maker and model, motherboard and BIOS version/date.", _section(board),
          "system and motherboard manufacturer/model, bios vendor/version/date"),
    _tool("get_windows_info", "Windows edition, version, build, architecture, install date, last boot.",
          _section(windows), "edition, version, build, release, architecture"),
]
