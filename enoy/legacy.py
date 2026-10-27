"""One-time compatibility with the previous application name.

Old names belong only here. Migration copies data and uses SQLite's backup API
so the original install remains recoverable, including any WAL transactions.
"""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile

OLD_DATA_DIR = "FusionAI"
OLD_DATABASE = "fusion.db"
OLD_ENV = "FUSION_AI_HOME"


def _copy_database(source: Path, destination: Path) -> None:
    if not source.is_file() or destination.exists():
        return
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
        with closing(sqlite3.connect(destination)) as new:
            old.backup(new)


def _relocate_project_paths(database: Path, old_home: Path, home: Path) -> None:
    """Keep saved plain chats/spaces attached to their original project IDs."""
    if not database.is_file():
        return
    with closing(sqlite3.connect(database)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
            return
        with db:
            for project_id, path in db.execute("SELECT id, path FROM projects").fetchall():
                try:
                    rel = Path(path).relative_to(old_home)
                except ValueError:
                    continue
                db.execute("UPDATE projects SET path=? WHERE id=?", (str(home / rel), project_id))


def data_home(base: Path) -> Path:
    override = os.environ.get("ENOY_HOME") or os.environ.get(OLD_ENV)
    if override:
        home = Path(override)
        home.mkdir(parents=True, exist_ok=True)
        _copy_database(home / OLD_DATABASE, home / "enoy.db")
        return home
    home = base / "Enoy"
    old_home = base / OLD_DATA_DIR
    if not home.exists() and old_home.is_dir():
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".enoy-migration-", dir=base))
        # Leave a failed staging copy recoverable. Never touch the old install.
        shutil.copytree(old_home, stage, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(OLD_DATABASE, OLD_DATABASE + "-*", "instance.json"))
        _copy_database(old_home / OLD_DATABASE, stage / "enoy.db")
        _relocate_project_paths(stage / "enoy.db", old_home, home)
        for path in (stage / "logs").glob("fusion.log*"):
            path.rename(path.with_name(path.name.replace("fusion", "enoy", 1)))
        stage.rename(home)
    home.mkdir(parents=True, exist_ok=True)
    return home
