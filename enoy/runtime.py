"""Commands for Enoy's stdio tools in source and packaged installations."""
import sys
from pathlib import Path


def module_command(module):
    if module not in ("enoy.tools.mcp_server", "enoy.blender_bridge"):
        raise ValueError("Unknown Enoy helper module.")
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("Enoy-tools.exe")), "--module", module]
    return [sys.executable, "-m", module]


def resource_root():
    return Path(__file__).resolve().parent.parent
