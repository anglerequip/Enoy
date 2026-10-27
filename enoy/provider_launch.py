"""Open the provider's interactive CLI without accepting arbitrary commands."""
from __future__ import annotations

import base64
import os
import subprocess

from .backends import claude_code, codex
from . import paths


def open_chatgpt():
    """Open ordinary ChatGPT without handing it prompts, files or CLI credentials."""
    import webbrowser
    if not webbrowser.open("https://chatgpt.com/", new=2):
        raise ValueError("Could not open your browser. Visit https://chatgpt.com/ to use regular ChatGPT.")
    return {"opened": True, "message": "ChatGPT opened in your browser. Enoy sent no prompt to Codex or the API."}


def install_missing():
    if os.name != "nt":
        raise ValueError("Automatic provider setup is supported on Windows only.")
    from .runtime import resource_root
    script = resource_root() / "launch" / "install-providers.ps1"
    if not script.is_file():
        raise ValueError("Provider setup is missing. Download the complete Enoy installer.")
    subprocess.Popen(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", str(script), "-Pause"], cwd=str(script.parent),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {"opened": True, "message": "Setup opened. When it finishes, sign into Codex and Claude with your own accounts."}


def launch(provider: str, settings: dict, action: str) -> dict:
    if provider not in ("codex", "claude") or action not in ("launch", "login"):
        raise ValueError("Unknown provider or launch action.")
    if not settings.get("enabled", True):
        raise ValueError("Enable this provider in Settings → Models first.")
    if os.name != "nt":
        raise ValueError("Opening a provider terminal is supported on Windows only.")
    finder = codex.find_executable if provider == "codex" else claude_code.find_executable
    exe = finder(settings.get("executable", ""))
    if not exe:
        raise ValueError("Provider CLI was not found. Install it or set its executable path in Settings → Models.")
    # PowerShell single-quoted literals keep configured paths out of command syntax.
    command = "& '" + exe.replace("'", "''") + "'"
    if action == "login":
        command += " login" if provider == "codex" else " auth login"
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-EncodedCommand", encoded],
        cwd=str(paths.claude_workdir()), creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return {"opened": True, "message": "Terminal opened. Complete any sign-in steps, then check status here."}
