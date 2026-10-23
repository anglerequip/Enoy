"""Locations of Enoy's own data (config, database, backups, logs)."""

from __future__ import annotations

import os
from pathlib import Path


def app_home() -> Path:
    """Directory for all application state.

    FUSION_AI_HOME overrides the default so tests and portable installs can
    keep their data elsewhere.
    """
    override = os.environ.get("FUSION_AI_HOME")
    if override:
        home = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        home = Path(base) / "FusionAI"
    home.mkdir(parents=True, exist_ok=True)
    return home


def sub_dir(name: str) -> Path:
    path = app_home() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_home() / "config.json"


def database_path() -> Path:
    return app_home() / "fusion.db"


def logs_dir() -> Path:
    return sub_dir("logs")


def backups_dir() -> Path:
    """Copies of project files taken before Enoy overwrote them."""
    return sub_dir("file-backups")


def config_backups_dir() -> Path:
    return sub_dir("config-backups")


def images_dir() -> Path:
    """Images attached to chat messages, named by their SHA-256 hash."""
    return sub_dir("images")


def general_chat_dir() -> Path:
    """Empty folder used as the 'project' for plain chats with no project open."""
    return sub_dir("general-chat")


def claude_workdir() -> Path:
    """Neutral, empty working directory for Claude Code calls.

    Enoy passes project context explicitly, so Claude Code is started away
    from the project to stop it reading files Enoy did not choose to send.
    """
    return sub_dir("claude-workdir")
