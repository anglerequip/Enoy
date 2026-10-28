"""Command policy and execution.

Models never run commands. They may suggest one; the user runs it from the UI,
and every run passes through classify() first:

  allowlisted -> runs without a prompt (configured safe checks such as pytest)
  approval    -> runs after the user confirms
  high_risk   -> runs only after a confirmation that names the risk
  blocked     -> never runs from Enoy

Commands run without a shell (no pipes, redirection or chaining), inside the
project folder, with credential-like environment variables removed, a timeout,
and cancellation. This reduces risk; it is not an OS-level sandbox, and a
command that is approved can still use the network.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .redact import is_secret_env_name, redact

MAX_OUTPUT_CHARS = 60_000

SHELL_METACHARS = ["&&", "||", "|", ";", ">", "<", "`", "$(", "%", "^", "\n", "&"]

BLOCKED_PROGRAMS = {
    "format", "diskpart", "bcdedit", "shutdown", "restart-computer", "stop-computer",
    "reg", "regedit", "runas", "sudo", "takeown", "icacls", "cacls", "netsh", "sc",
    "schtasks", "vssadmin", "wmic", "cipher", "mimikatz", "cmdkey", "vaultcmd",
    "set-executionpolicy", "set-mppreference", "fsutil", "attrib", "mklink",
}

# Programs that delete, install, download or change system state.
HIGH_RISK_PROGRAMS = {
    "del", "erase", "rd", "rmdir", "rm", "remove-item", "move", "mv", "ren", "rename",
    "curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "bitsadmin",
    "winget", "choco", "scoop", "msiexec", "setx", "powershell", "pwsh", "cmd", "bash",
    "wsl", "ssh", "scp", "ftp", "start", "explorer", "certutil",
}

HIGH_RISK_SUBCOMMANDS = [
    ("pip", "install"), ("pip", "uninstall"), ("pip3", "install"),
    ("python", "-m pip"), ("py", "-m pip"),
    ("npm", "install"), ("npm", "i"), ("npm", "uninstall"), ("npm", "publish"), ("npm", "exec"),
    ("npx",), ("pnpm", "add"), ("pnpm", "install"), ("yarn", "add"), ("yarn", "install"),
    ("git", "push"), ("git", "reset"), ("git", "clean"), ("git", "checkout"), ("git", "rebase"),
    ("git", "rm"), ("git", "restore"), ("git", "stash", "drop"), ("git", "branch", "-D"),
    ("git", "pull"), ("git", "fetch"), ("git", "clone"),
    ("cargo", "install"), ("dotnet", "tool"), ("rokit", "add"), ("aftman", "add"), ("wally", "install"),
]

CREDENTIAL_HINTS = [".env", "id_rsa", ".ssh", ".aws", "credentials", ".npmrc", ".git-credentials", "login data", "cookies"]


@dataclass
class Policy:
    level: str                       # allowlisted | approval | high_risk | blocked
    reasons: list[str] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"level": self.level, "reasons": self.reasons, "argv": self.argv}


def split_command(command: str) -> list[str]:
    return [a.strip('"') if a.startswith('"') and a.endswith('"') else a for a in shlex.split(command, posix=False)]


def _program_name(arg: str) -> str:
    name = Path(arg.strip('"')).name.lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _matches_prefix(argv: list[str], prefix: list[str]) -> bool:
    if len(argv) < len(prefix):
        return False
    return [_program_name(argv[0])] + [a.lower() for a in argv[1:len(prefix)]] == [p.lower() for p in prefix]


def classify(command: str, allowlist: list[str]) -> Policy:
    command = command.strip()
    if not command:
        return Policy("blocked", ["Empty command."])
    for meta in SHELL_METACHARS:
        if meta in command:
            return Policy(
                "blocked",
                [f"Contains '{meta.strip() or 'newline'}'. Enoy runs one program at a time without a shell, so pipes, redirection, chaining and variable expansion are not allowed."],
            )
    try:
        argv = split_command(command)
    except ValueError as exc:
        return Policy("blocked", [f"Could not parse the command: {exc}"])
    if not argv:
        return Policy("blocked", ["Empty command."])
    program = _program_name(argv[0])
    lowered = command.lower()
    if program in BLOCKED_PROGRAMS:
        return Policy("blocked", [f"'{program}' changes system settings or needs administrator rights."], argv)
    if any(hint in lowered for hint in CREDENTIAL_HINTS):
        return Policy("blocked", ["The command refers to a credentials or secrets location."], argv)
    if any(a.lower().startswith(("-encodedcommand", "-enc")) for a in argv[1:]):
        return Policy("blocked", ["Encoded PowerShell commands are not allowed."], argv)

    reasons: list[str] = []
    if program in HIGH_RISK_PROGRAMS:
        reasons.append(f"'{program}' can delete files, download data, or start other programs.")
    for prefix in HIGH_RISK_SUBCOMMANDS:
        parts = list(prefix)
        if len(parts) == 2 and " " in parts[1]:
            parts = [parts[0], *parts[1].split()]
        if _matches_prefix(argv, parts):
            reasons.append(f"'{' '.join(parts)}' installs software, uses the network, or discards work.")
            break
    if any(a.startswith(("http://", "https://")) for a in argv):
        reasons.append("The command contains a URL (network access).")
    if any(os.path.isabs(a) or a.startswith("..") or ":\\" in a for a in argv[1:]):
        reasons.append("The command refers to a path outside the project folder.")
    if reasons:
        return Policy("high_risk", reasons, argv)

    for entry in allowlist:
        try:
            prefix = split_command(entry)
        except ValueError:
            continue
        if prefix and _matches_prefix(argv, prefix):
            return Policy("allowlisted", [f"Matches allowlist entry '{entry}'."], argv)
    return Policy("approval", ["Not on the allowlist; needs your approval."], argv)


def safe_env() -> dict[str, str]:
    """Copy of the environment without variables that look like credentials."""
    return {k: v for k, v in os.environ.items() if not is_secret_env_name(k)}


@dataclass
class RunResult:
    status: str          # passed | failed | timeout | cancelled | error
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + f"\n\n... [{len(text) - MAX_OUTPUT_CHARS} characters omitted] ...\n\n" + text[-half:]


def resolve_program(program: str, cwd: Path) -> str:
    """Find the executable, preferring the project's own virtual environment.

    `python` / `pytest` in a project with .venv or venv should use that
    environment, not whatever Python happens to be first on PATH.
    """
    name = _program_name(program)
    if name in ("python", "python3", "pytest", "pip") and not os.path.isabs(program):
        for venv in (".venv", "venv", "env"):
            candidate = cwd / venv / "Scripts" / f"{name if name != 'python3' else 'python'}.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which(program) or program


def run(argv: list[str], cwd: Path, timeout_s: float, cancel: threading.Event | None = None,
        venv_root: Path | None = None) -> RunResult:
    """Run a program without a shell. Never raises for process failures.

    venv_root is where to look for the project's virtual environment when cwd is a Build worktree,
    which has the project's files but not its untracked .venv.
    """
    start = time.monotonic()
    exe = resolve_program(argv[0], Path(venv_root or cwd))
    try:
        proc = subprocess.Popen(
            [exe, *argv[1:]], cwd=str(cwd), env=safe_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return RunResult("error", None, "", f"Could not start '{argv[0]}': {exc}", time.monotonic() - start)

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def pump(stream, sink):
        for chunk in iter(lambda: stream.read(4096), ""):
            sink.append(chunk)

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for r in readers:
        r.start()
    status = None
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            status = "cancelled"
            break
        if time.monotonic() - start > timeout_s:
            status = "timeout"
            break
        time.sleep(0.05)
    if status is not None:
        _kill_tree(proc)
    proc.wait()
    for r in readers:
        r.join(timeout=2)
    duration = time.monotonic() - start
    stdout = redact(_truncate("".join(out_chunks)))
    stderr = redact(_truncate("".join(err_chunks)))
    if status == "timeout":
        stderr += f"\n[Enoy stopped the command after {timeout_s:.0f} seconds.]"
    if status is None:
        status = "passed" if proc.returncode == 0 else "failed"
    return RunResult(status, proc.returncode if status in ("passed", "failed") else None, stdout, stderr, duration)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and its children (taskkill /T on Windows)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        proc.kill()
