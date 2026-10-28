"""SQLite persistence for projects, tasks, events, change sets and runs.

One connection guarded by a lock; the app is single-user and writes are small.
Everything a task needs to resume after a restart lives here.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summary_backend TEXT NOT NULL DEFAULT '',
    last_opened REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    complexity TEXT NOT NULL DEFAULT '',
    workflow TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    routing_reason TEXT NOT NULL DEFAULT '',
    escalation_reason TEXT NOT NULL DEFAULT '',
    local_attempts INTEGER NOT NULL DEFAULT 0,
    claude_calls INTEGER NOT NULL DEFAULT 0,
    test_fix_attempts INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    role TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS changesets (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    backend TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    validation TEXT NOT NULL DEFAULT '{}',
    review TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_changes (
    id INTEGER PRIMARY KEY,
    changeset_id INTEGER NOT NULL REFERENCES changesets(id),
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    old_hash TEXT,
    new_hash TEXT,
    old_content TEXT,
    new_content TEXT,
    diff TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS command_runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER,
    project_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    duration_s REAL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transmissions (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    backend TEXT NOT NULL,
    purpose TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '[]',
    chars INTEGER NOT NULL,
    content TEXT NOT NULL,
    usage TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    title TEXT NOT NULL,
    agent TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    depends_on TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]',
    needs_approval INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id, id);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    task_id INTEGER,
    step_id INTEGER,
    kind TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_task ON activity(task_id, id);
CREATE TABLE IF NOT EXISTS team_tasks (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    plan_key TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    specialty TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '[]',
    files TEXT NOT NULL DEFAULT '[]',
    depends_on TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL DEFAULT '',
    route_reason TEXT NOT NULL DEFAULT '',
    activation_reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '{}',
    changeset_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS team_tasks_task ON team_tasks(task_id, id);
CREATE TABLE IF NOT EXISTS team_messages (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    team_task_id INTEGER,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    type TEXT NOT NULL,
    body TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS team_memory (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    task_id INTEGER,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_files (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha TEXT NOT NULL,
    content TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    UNIQUE(project_id, sha)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    project_id INTEGER,
    task_id INTEGER,
    message_id INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_versions (
    id INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    UNIQUE(artifact_id, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id);

-- Build workspace (planning.md, Phase 1). Plans are versioned; an approved version is the
-- contract the implementers work from, so the database refuses to change it.
CREATE TABLE IF NOT EXISTS dev_plans (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_version INTEGER,
    integration_branch TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dev_plans_project ON dev_plans(project_id, id);
CREATE TABLE IF NOT EXISTS dev_plan_versions (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES dev_plans(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    sha TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    approved REAL,
    created REAL NOT NULL,
    UNIQUE(plan_id, version)
);
CREATE TRIGGER IF NOT EXISTS dev_plan_version_locked_update BEFORE UPDATE ON dev_plan_versions
WHEN OLD.approved IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'An approved plan version cannot be changed.'); END;
CREATE TRIGGER IF NOT EXISTS dev_plan_version_locked_delete BEFORE DELETE ON dev_plan_versions
WHEN OLD.approved IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'An approved plan version cannot be deleted.'); END;
CREATE TABLE IF NOT EXISTS dev_tasks (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES dev_plans(id),
    version INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    title TEXT NOT NULL,
    spec TEXT NOT NULL DEFAULT '{}',
    depends_on TEXT NOT NULL DEFAULT '[]',
    allowed_files TEXT NOT NULL DEFAULT '[]',
    test_files TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claim TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    commit_id TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',
    blocker TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    finished REAL,
    UNIQUE(plan_id, version, task_key)
);
CREATE INDEX IF NOT EXISTS dev_tasks_plan ON dev_tasks(plan_id, status);
CREATE TABLE IF NOT EXISTS dev_runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES dev_tasks(id),
    attempt INTEGER NOT NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    worktree TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    test_commit TEXT NOT NULL DEFAULT '',
    impl_commit TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started REAL NOT NULL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS dev_runs_task ON dev_runs(task_id, id);
-- Fingerprints of a task's approved tests, taken right after the test phase. Never rewritten.
CREATE TABLE IF NOT EXISTS dev_frozen_files (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES dev_tasks(id),
    run_id INTEGER REFERENCES dev_runs(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    frozen REAL NOT NULL,
    UNIQUE(task_id, path)
);
CREATE TRIGGER IF NOT EXISTS dev_frozen_locked_update BEFORE UPDATE ON dev_frozen_files
BEGIN SELECT RAISE(ABORT, 'A frozen test fingerprint cannot be changed.'); END;
CREATE TRIGGER IF NOT EXISTS dev_frozen_locked_delete BEFORE DELETE ON dev_frozen_files
BEGIN SELECT RAISE(ABORT, 'A frozen test fingerprint cannot be deleted.'); END;
-- One row per API model request: tokens reported by the provider and what it cost (spend limits).
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    plan_id INTEGER,
    dev_task_id INTEGER,
    run_id INTEGER,
    status TEXT NOT NULL,
    reserved_usd REAL NOT NULL DEFAULT 0,
    prices TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER,
    cached_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    cost_source TEXT NOT NULL DEFAULT 'unknown',
    error TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS model_calls_provider ON model_calls(provider, created);
"""

# Task statuses.
ACTIVE_STATUSES = ("queued", "running")
STATUSES = (
    "queued", "running", "awaiting_approval", "awaiting_claude", "completed",
    "failed", "cancelled", "interrupted", "paused",
)
TEAM_OPEN = ("planned", "ready", "running", "waiting", "awaiting_review", "paused")
TEAM_STATUSES = TEAM_OPEN + ("completed", "failed", "blocked", "cancelled")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")

# Steps are the units of work inside a task: one model attempt, one check run,
# one approval. Agents: "local", "claude", "system" (the app itself), "user".
STEP_OPEN = ("queued", "working", "waiting", "blocked")
STEP_STATUSES = STEP_OPEN + ("completed", "failed", "cancelled", "skipped", "interrupted")


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._migrate()
        if "history_saved_chars" not in {r["name"] for r in self._all("PRAGMA table_info(transmissions)")}:
            self._exec("ALTER TABLE transmissions ADD COLUMN history_saved_chars INTEGER NOT NULL DEFAULT 0")

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing databases."""
        cols = {r["name"] for r in self._all("PRAGMA table_info(tasks)")}
        for name in ("started", "finished"):
            if name not in cols:
                self._exec(f"ALTER TABLE tasks ADD COLUMN {name} REAL")
        if "local_only" not in {r["name"] for r in self._all("PRAGMA table_info(projects)")}:
            self._exec("ALTER TABLE projects ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
        if "images" not in {r["name"] for r in self._all("PRAGMA table_info(messages)")}:
            self._exec("ALTER TABLE messages ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")
        pcols = {r["name"] for r in self._all("PRAGMA table_info(projects)")}
        for name, ddl in (("description", "TEXT NOT NULL DEFAULT ''"), ("instructions", "TEXT NOT NULL DEFAULT ''"),
                          ("kind", "TEXT NOT NULL DEFAULT 'folder'"), ("archived", "INTEGER NOT NULL DEFAULT 0"),
                          ("created", "REAL"), ("updated", "REAL")):
            if name not in pcols:
                self._exec(f"ALTER TABLE projects ADD COLUMN {name} {ddl}")
        if "spec" not in {r["name"] for r in self._all("PRAGMA table_info(team_tasks)")}:
            self._exec("ALTER TABLE team_tasks ADD COLUMN spec TEXT NOT NULL DEFAULT '{}'")
        self._exec("UPDATE projects SET created = last_opened WHERE created IS NULL")
        self._exec("UPDATE projects SET updated = last_opened WHERE updated IS NULL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextlib.contextmanager
    def _tx(self):
        """All statements inside commit together or not at all."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            return _row(self._db.execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # Projects -----------------------------------------------------------

    def upsert_project(self, path: str, name: str, kind: str = "folder") -> dict:
        """Open (or reopen) a project by its folder. Reopening a removed folder project restores it."""
        now = time.time()
        with self._lock:
            existing = self._one("SELECT * FROM projects WHERE path = ?", (path,))
            if existing:
                self._exec("UPDATE projects SET last_opened = ?, archived = 0 WHERE id = ?", (now, existing["id"]))
            else:
                self._exec(
                    "INSERT INTO projects (path, name, last_opened, kind, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                    (path, name, now, kind, now, now),
                )
            return self._one("SELECT * FROM projects WHERE path = ?", (path,))

    def update_project(self, project_id: int, **fields: Any) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("name", "description", "instructions") and v is not None}
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            self._exec(f"UPDATE projects SET {sets}, updated = ? WHERE id = ?", (*allowed.values(), time.time(), project_id))
        return self.get_project(project_id)

    def list_projects(self) -> list[dict]:
        """Projects for the Projects page, newest activity first, with counts. No content is loaded."""
        return self._all(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS chats, "
            "(SELECT COUNT(*) FROM project_files f WHERE f.project_id = p.id) AS files, "
            "(SELECT COUNT(*) FROM artifacts a WHERE a.project_id = p.id) AS artifacts, "
            "MAX(COALESCE(p.updated, 0), p.last_opened, COALESCE((SELECT MAX(t.updated) FROM tasks t WHERE t.project_id = p.id), 0)) AS activity "
            "FROM projects p WHERE p.archived = 0 AND (p.last_opened > 0 OR p.kind = 'space') ORDER BY activity DESC")

    def space_name_taken(self, name: str, except_id: int | None = None) -> bool:
        row = self._one("SELECT id FROM projects WHERE kind = 'space' AND archived = 0 AND lower(name) = lower(?) "
                        "AND id != ?", (name.strip(), except_id or -1))
        return row is not None

    def archive_project(self, project_id: int) -> None:
        self._exec("UPDATE projects SET archived = 1, updated = ? WHERE id = ?", (time.time(), project_id))

    def move_project_tasks(self, from_id: int, to_id: int) -> int:
        return self._exec("UPDATE tasks SET project_id = ? WHERE project_id = ?", (to_id, from_id)).rowcount

    def move_task(self, task_id: int, project_id: int) -> None:
        self._exec("UPDATE tasks SET project_id = ? WHERE id = ?", (project_id, task_id))

    def delete_task(self, task_id: int) -> bool:
        """Delete a chat and everything that only exists for it.

        Kept: artifacts (unlinked, they outlive chats), learned project notes
        (unlinked) and the record of what was sent to Claude (a privacy audit).
        """
        with self._tx():
            self._exec("UPDATE artifacts SET task_id = NULL, message_id = NULL WHERE task_id = ?", (task_id,))
            self._exec("UPDATE team_memory SET task_id = NULL WHERE task_id = ?", (task_id,))
            self._exec("DELETE FROM file_changes WHERE changeset_id IN (SELECT id FROM changesets WHERE task_id = ?)",
                       (task_id,))
            for table in ("changesets", "messages", "events", "steps", "activity", "team_messages",
                          "team_tasks", "command_runs"):
                self._exec(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
            return self._exec("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0

    # Project knowledge files ---------------------------------------------------

    def add_project_file(self, project_id: int, name: str, content: str, size: int, sha: str) -> tuple[dict, bool]:
        """Returns (file, created). The same document twice in one project is stored once."""
        with self._lock:
            existing = self._one("SELECT id FROM project_files WHERE project_id = ? AND sha = ?", (project_id, sha))
            if existing:
                return self.project_file(project_id, existing["id"], with_content=False), False
            cur = self._exec("INSERT INTO project_files (project_id, name, size, sha, content, created) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (project_id, name, size, sha, content, time.time()))
            return self.project_file(project_id, cur.lastrowid, with_content=False), True

    def project_files(self, project_id: int, with_content: bool = False) -> list[dict]:
        cols = "*" if with_content else "id, project_id, name, size, sha, private, created"
        return self._all(f"SELECT {cols} FROM project_files WHERE project_id = ? ORDER BY created DESC", (project_id,))

    def project_file(self, project_id: int, file_id: int, with_content: bool = True) -> dict | None:
        """Scoped by project: a file id from another project is not found."""
        cols = "*" if with_content else "id, project_id, name, size, sha, private, created"
        return self._one(f"SELECT {cols} FROM project_files WHERE id = ? AND project_id = ?", (file_id, project_id))

    def set_project_file_private(self, project_id: int, file_id: int, on: bool) -> None:
        self._exec("UPDATE project_files SET private = ? WHERE id = ? AND project_id = ?", (int(bool(on)), file_id, project_id))

    def delete_project_file(self, project_id: int, file_id: int) -> bool:
        return self._exec("DELETE FROM project_files WHERE id = ? AND project_id = ?", (file_id, project_id)).rowcount > 0

    def delete_project_files(self, project_id: int) -> int:
        return self._exec("DELETE FROM project_files WHERE project_id = ?", (project_id,)).rowcount

    # Artifacts -----------------------------------------------------------------

    _ART_META = ("a.id, a.title, a.type, a.language, a.project_id, a.task_id, a.message_id, a.version, a.created, "
                 "a.updated, length(a.content) AS chars, p.name AS project_name")

    def create_artifact(self, title: str, type_: str, content: str, language: str = "", project_id: int | None = None,
                        task_id: int | None = None, message_id: int | None = None, note: str = "Created") -> dict:
        now = time.time()
        with self._tx():
            cur = self._exec("INSERT INTO artifacts (title, type, language, content, project_id, task_id, message_id, "
                             "version, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                             (title, type_, language, content, project_id, task_id, message_id, now, now))
            aid = cur.lastrowid
            self._exec("INSERT INTO artifact_versions (artifact_id, version, title, type, language, content, note, created) "
                       "VALUES (?, 1, ?, ?, ?, ?, ?, ?)", (aid, title, type_, language, content, note, now))
        return self.get_artifact(aid)

    def find_artifact_from_message(self, message_id: int, content: str) -> dict | None:
        # Matches the content as first saved, so a later edit does not cause a second copy.
        row = self._one("SELECT a.id FROM artifacts a JOIN artifact_versions v ON v.artifact_id = a.id AND v.version = 1 "
                        "WHERE a.message_id = ? AND v.content = ? ORDER BY a.id LIMIT 1", (message_id, content))
        return self.get_artifact(row["id"]) if row else None

    def get_artifact(self, artifact_id: int) -> dict | None:
        return self._one("SELECT a.*, p.name AS project_name FROM artifacts a LEFT JOIN projects p ON p.id = a.project_id "
                         "WHERE a.id = ?", (artifact_id,))

    def list_artifacts(self, project_id: int | None = None, query: str = "", limit: int = 300) -> list[dict]:
        """Metadata only (no content), newest first."""
        sql = f"SELECT {self._ART_META} FROM artifacts a LEFT JOIN projects p ON p.id = a.project_id WHERE 1=1"
        params: list = []
        if project_id is not None:
            sql += " AND a.project_id = ?"
            params.append(project_id)
        if query:
            sql += " AND (a.title LIKE ? ESCAPE '\\' OR a.content LIKE ? ESCAPE '\\')"
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        sql += " ORDER BY a.updated DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))

    def update_artifact(self, artifact_id: int, base_version: int, note: str = "Edited", **fields: Any) -> dict:
        """Save a new version. Raises artifacts.Conflict if someone saved since base_version."""
        from .artifacts import Conflict
        with self._tx():
            art = self.get_artifact(artifact_id)
            if art is None:
                raise KeyError(f"Artifact {artifact_id} not found")
            if int(base_version) != art["version"]:
                raise Conflict(art)
            changes = {k: v for k, v in fields.items() if k in ("title", "type", "language", "content")
                       and v is not None and v != art[k]}
            if not changes:
                return art
            new = {**{k: art[k] for k in ("title", "type", "language", "content")}, **changes}
            version = art["version"] + 1
            now = time.time()
            self._exec("UPDATE artifacts SET title = ?, type = ?, language = ?, content = ?, version = ?, updated = ? "
                       "WHERE id = ?", (new["title"], new["type"], new["language"], new["content"], version, now, artifact_id))
            self._exec("INSERT INTO artifact_versions (artifact_id, version, title, type, language, content, note, created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (artifact_id, version, new["title"], new["type"], new["language"], new["content"], note, now))
        return self.get_artifact(artifact_id)

    def artifact_versions(self, artifact_id: int) -> list[dict]:
        return self._all("SELECT id, version, title, type, note, created, length(content) AS chars FROM artifact_versions "
                         "WHERE artifact_id = ? ORDER BY version DESC", (artifact_id,))

    def artifact_version(self, artifact_id: int, version: int) -> dict | None:
        return self._one("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = ?", (artifact_id, version))

    def set_artifact_project(self, artifact_id: int, project_id: int | None) -> None:
        self._exec("UPDATE artifacts SET project_id = ?, updated = ? WHERE id = ?", (project_id, time.time(), artifact_id))

    def unlink_project_artifacts(self, project_id: int) -> int:
        return self._exec("UPDATE artifacts SET project_id = NULL WHERE project_id = ?", (project_id,)).rowcount

    def delete_artifact(self, artifact_id: int) -> bool:
        with self._tx():
            self._exec("DELETE FROM artifact_versions WHERE artifact_id = ?", (artifact_id,))
            return self._exec("DELETE FROM artifacts WHERE id = ?", (artifact_id,)).rowcount > 0

    def get_project(self, project_id: int) -> dict | None:
        return self._one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def recent_projects(self, limit: int = 10) -> list[dict]:
        return self._all("SELECT * FROM projects ORDER BY last_opened DESC LIMIT ?", (limit,))

    def set_project_summary(self, project_id: int, summary: str, backend: str) -> None:
        self._exec(
            "UPDATE projects SET summary = ?, summary_backend = ? WHERE id = ?",
            (summary, backend, project_id),
        )

    def set_project_local_only(self, project_id: int, on: bool) -> None:
        self._exec("UPDATE projects SET local_only = ? WHERE id = ?", (int(bool(on)), project_id))

    def forget_project(self, project_id: int) -> None:
        """Remove a project from the recent list. Its task history is kept."""
        self._exec("UPDATE projects SET last_opened = 0 WHERE id = ?", (project_id,))

    # Tasks ----------------------------------------------------------------

    def create_task(self, project_id: int, title: str, objective: str, kind: str, **fields: Any) -> dict:
        now = time.time()
        state = json.dumps(fields.pop("state", {}))
        cur = self._exec(
            "INSERT INTO tasks (project_id, title, objective, kind, status, state, created, updated) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
            (project_id, title, objective, kind, state, now, now),
        )
        task_id = cur.lastrowid
        if fields:
            self.update_task(task_id, **fields)
        return self.get_task(task_id)

    _TASK_FIELDS = {
        "title", "status", "complexity", "workflow", "owner", "routing_reason",
        "escalation_reason", "local_attempts", "claude_calls", "test_fix_attempts",
        "summary", "kind",
    }

    def update_task(self, task_id: int, **fields: Any) -> None:
        sets, params = [], []
        for key, value in fields.items():
            if key == "state":
                sets.append("state = ?")
                params.append(json.dumps(value))
            elif key in self._TASK_FIELDS:
                sets.append(f"{key} = ?")
                params.append(value)
            else:
                raise KeyError(f"Unknown task field {key}")
        now = time.time()
        status = fields.get("status")
        if status == "running":
            sets.append("started = COALESCE(started, ?)")
            params.append(now)
        if status in TERMINAL_STATUSES:
            sets.append("finished = ?")
            params.append(now)
        elif status is not None:
            sets.append("finished = NULL")
        sets.append("updated = ?")
        params.append(now)
        params.append(task_id)
        self._exec(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def get_task(self, task_id: int) -> dict | None:
        task = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task:
            task["state"] = json.loads(task["state"] or "{}")
        return task

    def update_task_state(self, task_id: int, **changes: Any) -> dict:
        """Merge keys into the task's JSON state. List values are appended."""
        with self._lock:
            task = self.get_task(task_id)
            state = task["state"]
            for key, value in changes.items():
                if isinstance(value, list) and isinstance(state.get(key), list):
                    state[key] = state[key] + value
                else:
                    state[key] = value
            self.update_task(task_id, state=state)
            return state

    def list_tasks(self, project_id: int | None = None, limit: int = 50) -> list[dict]:
        if project_id is None:
            rows = self._all("SELECT * FROM tasks ORDER BY updated DESC LIMIT ?", (limit,))
        else:
            rows = self._all(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY updated DESC LIMIT ?",
                (project_id, limit),
            )
        for row in rows:
            row["state"] = json.loads(row["state"] or "{}")
        return rows

    def tasks_with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self._all(f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", statuses)
        for row in rows:
            row["state"] = json.loads(row["state"] or "{}")
        return rows

    def finished_tasks(self, limit: int) -> list[dict]:
        """Most recent tasks that reached a result (completed or failed), newest first.

        Running, waiting, cancelled and interrupted tasks are left out: they have no outcome yet.
        """
        rows = self._all("SELECT * FROM tasks WHERE status IN ('completed', 'failed') "
                         "ORDER BY COALESCE(finished, updated) DESC LIMIT ?", (limit,))
        for row in rows:
            row["state"] = json.loads(row["state"] or "{}")
        return rows

    def task_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}

    def mark_interrupted(self) -> list[int]:
        """Called at startup: tasks that were running when the app stopped.

        Tasks queued while dispatch was paused never started, so they stay queued.
        """
        ids = []
        for task in self.tasks_with_status("queued", "running"):
            if task["status"] == "queued" and task["state"].get("queued_action"):
                continue
            ids.append(task["id"])
            self.update_task(task["id"], status="interrupted")
            self.add_event(task["id"], "status", "The app stopped while this task was running. Use Resume to continue it.")
            for step in self.steps(task["id"]):
                if step["status"] == "working":
                    self.update_step(step["id"], status="interrupted", error="The app stopped while this was running.")
            for tt in self.team_tasks(task["id"]):
                if tt["status"] == "running":
                    self.update_team_task(tt["id"], status="planned",
                                          error="The app stopped while this ran; it runs again when you resume.")
            self.add_activity(task["id"], "task.interrupted", "The app stopped while this task was running.",
                              "system", outcome="interrupted")
        return ids

    # Messages and events -------------------------------------------------------

    def add_message(self, task_id: int, role: str, content: str, backend: str = "",
                    images: list[str] | None = None) -> int:
        cur = self._exec(
            "INSERT INTO messages (task_id, role, backend, content, created, images) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, role, backend, content, time.time(), json.dumps(images or [])),
        )
        return cur.lastrowid

    def get_message(self, message_id: int) -> dict | None:
        return self._one("SELECT * FROM messages WHERE id = ?", (message_id,))

    def messages(self, task_id: int) -> list[dict]:
        rows = self._all("SELECT * FROM messages WHERE task_id = ? ORDER BY id", (task_id,))
        for r in rows:
            r["images"] = json.loads(r.get("images") or "[]")
        return rows

    def add_event(self, task_id: int, type_: str, text: str, backend: str = "") -> dict:
        now = time.time()
        cur = self._exec(
            "INSERT INTO events (task_id, type, text, backend, created) VALUES (?, ?, ?, ?, ?)",
            (task_id, type_, redact(text), backend, now),
        )
        return {"id": cur.lastrowid, "task_id": task_id, "type": type_, "text": redact(text), "backend": backend, "created": now}

    def events(self, task_id: int) -> list[dict]:
        return self._all("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,))

    # Steps and activity -------------------------------------------------------

    def add_step(self, task_id: int, title: str, agent: str, stage: str, reason: str = "",
                 status: str = "working", attempt: int = 0, depends_on: list[int] | None = None,
                 files: list[str] | None = None, needs_approval: bool = False) -> dict:
        now = time.time()
        cur = self._exec(
            "INSERT INTO steps (task_id, title, agent, stage, reason, status, attempt, depends_on, files, "
            "needs_approval, created, started) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, agent, stage, redact(reason), status, attempt, json.dumps(depends_on or []),
             json.dumps(files or []), int(needs_approval), now, now if status == "working" else None),
        )
        return self.get_step(cur.lastrowid)

    _STEP_FIELDS = {"status", "result", "error", "files", "needs_approval", "title"}

    def update_step(self, step_id: int, **fields: Any) -> None:
        sets, params = [], []
        now = time.time()
        for key, value in fields.items():
            if key not in self._STEP_FIELDS:
                raise KeyError(f"Unknown step field {key}")
            if key == "files":
                value = json.dumps(value)
            elif key in ("result", "error"):
                value = redact(value)
            sets.append(f"{key} = ?")
            params.append(value)
        status = fields.get("status")
        if status == "working":
            sets.append("started = COALESCE(started, ?)")
            params.append(now)
        if status is not None and status not in STEP_OPEN:
            sets.append("finished = ?")
            params.append(now)
        params.append(step_id)
        self._exec(f"UPDATE steps SET {', '.join(sets)} WHERE id = ?", tuple(params))

    @staticmethod
    def _step_row(row: dict) -> dict:
        row["depends_on"] = json.loads(row["depends_on"] or "[]")
        row["files"] = json.loads(row["files"] or "[]")
        row["needs_approval"] = bool(row["needs_approval"])
        return row

    def get_step(self, step_id: int) -> dict | None:
        row = self._one("SELECT * FROM steps WHERE id = ?", (step_id,))
        return self._step_row(row) if row else None

    def steps(self, task_id: int) -> list[dict]:
        return [self._step_row(r) for r in self._all("SELECT * FROM steps WHERE task_id = ? ORDER BY id", (task_id,))]

    def open_steps(self, task_id: int | None = None) -> list[dict]:
        marks = ",".join("?" * len(STEP_OPEN))
        sql = f"SELECT s.*, t.title AS task_title FROM steps s JOIN tasks t ON t.id = s.task_id WHERE s.status IN ({marks})"
        params: tuple = STEP_OPEN
        if task_id is not None:
            sql += " AND s.task_id = ?"
            params = params + (task_id,)
        return [self._step_row(r) for r in self._all(sql + " ORDER BY s.id", params)]

    def recent_steps(self, agent: str, limit: int = 5, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT s.*, t.title AS task_title FROM steps s JOIN tasks t ON t.id = s.task_id WHERE s.agent = ?"
        params: tuple = (agent,)
        if statuses:
            sql += f" AND s.status IN ({','.join('?' * len(statuses))})"
            params = params + tuple(statuses)
        return [self._step_row(r) for r in self._all(sql + " ORDER BY s.id DESC LIMIT ?", params + (limit,))]

    def add_activity(self, task_id: int | None, kind: str, text: str, agent: str = "system", stage: str = "",
                     reason: str = "", outcome: str = "", step_id: int | None = None) -> dict:
        now = time.time()
        text, reason = redact(text), redact(reason)
        cur = self._exec(
            "INSERT INTO activity (task_id, step_id, kind, agent, stage, text, reason, outcome, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, step_id, kind, agent, stage, text, reason, outcome, now),
        )
        return {"id": cur.lastrowid, "task_id": task_id, "step_id": step_id, "kind": kind, "agent": agent,
                "stage": stage, "text": text, "reason": reason, "outcome": outcome, "created": now}

    def activity(self, task_id: int | None = None, limit: int = 200, before: int | None = None) -> list[dict]:
        """Newest first."""
        sql = "SELECT a.*, t.title AS task_title FROM activity a LEFT JOIN tasks t ON t.id = a.task_id WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            sql += " AND a.task_id = ?"
            params.append(task_id)
        if before is not None:
            sql += " AND a.id < ?"
            params.append(before)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))

    # Team tasks, messages and shared memory ---------------------------------------

    def add_team_task(self, task_id: int, role: str, title: str, *, plan_key: str = "", specialty: str = "",
                      description: str = "", acceptance: list[str] | None = None, files: list[str] | None = None,
                      depends_on: list[int] | None = None, provider: str = "", route_reason: str = "",
                      activation_reason: str = "", status: str = "planned", spec: dict | None = None) -> dict:
        cur = self._exec(
            "INSERT INTO team_tasks (task_id, plan_key, role, specialty, title, description, acceptance, files, "
            "depends_on, provider, route_reason, activation_reason, status, spec, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, plan_key, role, specialty, redact(title), redact(description), json.dumps(acceptance or []),
             json.dumps(files or []), json.dumps(depends_on or []), provider, redact(route_reason),
             redact(activation_reason), status, redact(json.dumps(spec or {})), time.time()),
        )
        return self.get_team_task(cur.lastrowid)

    _TEAM_FIELDS = {"status", "provider", "route_reason", "attempts", "result", "changeset_id", "error",
                    "depends_on", "files", "description", "title", "spec"}

    def update_team_task(self, team_task_id: int, **fields: Any) -> None:
        sets, params = [], []
        now = time.time()
        for key, value in fields.items():
            if key not in self._TEAM_FIELDS:
                raise KeyError(f"Unknown team task field {key}")
            if key in ("result", "depends_on", "files"):
                value = json.dumps(value)
            elif key == "spec":
                value = redact(json.dumps(value))
            elif key in ("error", "route_reason", "description", "title"):
                value = redact(value)
            sets.append(f"{key} = ?")
            params.append(value)
        status = fields.get("status")
        if status == "running":
            sets.append("started = COALESCE(started, ?)")
            params.append(now)
        if status is not None and status not in TEAM_OPEN:
            sets.append("finished = ?")
            params.append(now)
        params.append(team_task_id)
        self._exec(f"UPDATE team_tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))

    @staticmethod
    def _team_row(row: dict) -> dict:
        for key in ("acceptance", "files", "depends_on"):
            row[key] = json.loads(row[key] or "[]")
        row["result"] = json.loads(row["result"] or "{}")
        row["spec"] = json.loads(row.get("spec") or "{}")
        return row

    def get_team_task(self, team_task_id: int) -> dict | None:
        row = self._one("SELECT * FROM team_tasks WHERE id = ?", (team_task_id,))
        return self._team_row(row) if row else None

    def team_tasks(self, task_id: int) -> list[dict]:
        return [self._team_row(r) for r in self._all("SELECT * FROM team_tasks WHERE task_id = ? ORDER BY id", (task_id,))]

    def add_team_message(self, task_id: int, from_role: str, to_role: str, type_: str, body: str,
                         team_task_id: int | None = None) -> dict:
        now = time.time()
        body = redact(body)[:2000]
        cur = self._exec(
            "INSERT INTO team_messages (task_id, team_task_id, from_role, to_role, type, body, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, team_task_id, from_role, to_role, type_, body, now))
        return {"id": cur.lastrowid, "task_id": task_id, "team_task_id": team_task_id, "from_role": from_role,
                "to_role": to_role, "type": type_, "body": body, "created": now}

    def team_messages(self, task_id: int, limit: int = 200) -> list[dict]:
        return self._all("SELECT * FROM team_messages WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit))

    def add_memory(self, project_id: int, category: str, content: str, verified: bool, source: str,
                   task_id: int | None = None) -> None:
        self._exec("INSERT INTO team_memory (project_id, task_id, category, content, verified, source, created) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (project_id, task_id, category, redact(content)[:4000], int(verified), source, time.time()))

    def memory(self, project_id: int, category: str | None = None, limit: int = 20,
               task_id: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM team_memory WHERE project_id = ?", [project_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        return self._all(sql + " ORDER BY id DESC LIMIT ?", tuple(params + [limit]))

    # Change sets ----------------------------------------------------------------

    def create_changeset(self, task_id: int, backend: str, files: list[dict], validation: dict, note: str = "") -> int:
        now = time.time()
        with self._lock:
            cur = self._exec(
                "INSERT INTO changesets (task_id, backend, status, note, validation, created, updated) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                (task_id, backend, note, json.dumps(validation), now, now),
            )
            cs_id = cur.lastrowid
            for f in files:
                self._exec(
                    "INSERT INTO file_changes (changeset_id, path, action, old_hash, new_hash, old_content, new_content, diff) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cs_id, f["path"], f["action"], f.get("old_hash"), f.get("new_hash"),
                     f.get("old_content"), f.get("new_content"), f.get("diff", "")),
                )
            return cs_id

    def get_changeset(self, cs_id: int) -> dict | None:
        cs = self._one("SELECT * FROM changesets WHERE id = ?", (cs_id,))
        if cs:
            cs["validation"] = json.loads(cs["validation"] or "{}")
            cs["files"] = self._all("SELECT * FROM file_changes WHERE changeset_id = ? ORDER BY id", (cs_id,))
        return cs

    def list_changesets(self, task_id: int | None = None, status: str | None = None, project_id: int | None = None) -> list[dict]:
        sql = "SELECT c.* FROM changesets c JOIN tasks t ON t.id = c.task_id WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            sql += " AND c.task_id = ?"
            params.append(task_id)
        if status is not None:
            sql += " AND c.status = ?"
            params.append(status)
        if project_id is not None:
            sql += " AND t.project_id = ?"
            params.append(project_id)
        sql += " ORDER BY c.id DESC"
        result = []
        for row in self._all(sql, tuple(params)):
            result.append(self.get_changeset(row["id"]))
        return result

    def set_changeset_status(self, cs_id: int, status: str, note: str | None = None) -> None:
        if note is None:
            self._exec("UPDATE changesets SET status = ?, updated = ? WHERE id = ?", (status, time.time(), cs_id))
        else:
            self._exec(
                "UPDATE changesets SET status = ?, note = ?, updated = ? WHERE id = ?",
                (status, note, time.time(), cs_id),
            )

    def set_changeset_review(self, cs_id: int, review: str) -> None:
        self._exec("UPDATE changesets SET review = ?, updated = ? WHERE id = ?", (review, time.time(), cs_id))

    # Commands ----------------------------------------------------------------------

    def add_command_run(self, project_id: int, command: str, task_id: int | None = None) -> int:
        cur = self._exec(
            "INSERT INTO command_runs (task_id, project_id, command, status, created) VALUES (?, ?, ?, 'running', ?)",
            (task_id, project_id, command, time.time()),
        )
        return cur.lastrowid

    def finish_command_run(self, run_id: int, status: str, exit_code: int | None, stdout: str, stderr: str, duration_s: float) -> None:
        self._exec(
            "UPDATE command_runs SET status = ?, exit_code = ?, stdout = ?, stderr = ?, duration_s = ? WHERE id = ?",
            (status, exit_code, redact(stdout), redact(stderr), duration_s, run_id),
        )

    def get_command_run(self, run_id: int) -> dict | None:
        return self._one("SELECT * FROM command_runs WHERE id = ?", (run_id,))

    def list_command_runs(self, project_id: int, task_id: int | None = None, limit: int = 30) -> list[dict]:
        if task_id is None:
            return self._all(
                "SELECT * FROM command_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)
            )
        return self._all(
            "SELECT * FROM command_runs WHERE project_id = ? AND task_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, task_id, limit),
        )

    # Transmissions (what was sent to which backend) -----------------------------------

    def add_transmission(self, task_id: int, backend: str, purpose: str, files: list[dict], content: str) -> int:
        # Compare only conversation text: context selection is a hypothetical
        # full-history baseline, not a measurement of provider billing or caches.
        saved = 0
        marker = "Shared state snapshot (recent records; any omissions are marked):\n"
        if marker in content:
            try:
                snapshot, _ = json.JSONDecoder().raw_decode(content.split(marker, 1)[1])
                sent = sum(len(m.get("content") or "") for m in snapshot["conversation"])
                full = sum(len(m.get("content") or "") for m in self.messages(task_id))
                saved = max(0, full - sent)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        cur = self._exec(
            "INSERT INTO transmissions (task_id, backend, purpose, files, chars, content, created, history_saved_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, backend, purpose, json.dumps(files), len(content), redact(content), time.time(), saved),
        )
        return cur.lastrowid

    def context_savings(self) -> dict:
        rows = self._all("SELECT backend, COUNT(*) AS requests, SUM(history_saved_chars) AS saved_chars "
                         "FROM transmissions WHERE usage != '' AND backend IN ('codex', 'claude') GROUP BY backend")
        for row in rows:
            row["estimated_tokens_saved"] = int(row["saved_chars"] / 3)
        return {"providers": rows, "estimated_tokens_saved": sum(r["estimated_tokens_saved"] for r in rows),
                "method": "Conversation characters omitted per completed Enoy request, divided by 3, compared with sending the full conversation each time. Tracking starts with this update; older requests contribute zero. This is a history-only estimate, not billed tokens, money saved or extra subscription quota. Native tool traffic and provider caching are excluded."}

    def audit_context_savings(self) -> dict:
        """Recount persisted requests independently; no provider call or extra tokens."""
        rows = self._all("SELECT backend, usage, content, history_saved_chars FROM transmissions "
                         "WHERE backend IN ('codex', 'claude')")
        complete = [r for r in rows if r['usage'] != '']
        invalid = [r for r in complete if not isinstance(r['history_saved_chars'], int)
                   or r['history_saved_chars'] < 0]
        marker = "Shared state snapshot (recent records; any omissions are marked):\n"
        for row in complete:
            if row['history_saved_chars'] <= 0:
                continue
            try:
                snapshot, _ = json.JSONDecoder().raw_decode(row['content'].split(marker, 1)[1])
                if not isinstance(snapshot['conversation'], list):
                    invalid.append(row)
            except (IndexError, ValueError, KeyError, TypeError):
                invalid.append(row)
        by_provider = {provider: sum(max(0, r['history_saved_chars'] or 0) for r in complete
                                     if r['backend'] == provider) // 3 for provider in ('codex', 'claude')}
        displayed = self.context_savings()
        applied = self._one("SELECT COUNT(*) AS n FROM changesets WHERE status = 'applied'")['n']
        checks = self._all("SELECT status, COUNT(*) AS n FROM command_runs GROUP BY status")
        matches = not invalid and displayed['estimated_tokens_saved'] == sum(by_provider.values())
        return {'ok': matches, 'completed_requests': len(complete),
                'pending_requests': len(rows) - len(complete), 'invalid_records': len(invalid),
                'applied_change_sets': applied, 'checks': {r['status']: r['n'] for r in checks},
                'estimated_tokens_saved': displayed['estimated_tokens_saved'],
                'detail': ('The saved estimate matches completed request records.' if matches else
                           'The saved estimate did not match its source records. Treat it as unreliable.')
                          if complete else 'No completed model requests are recorded yet; there is nothing to verify.'}

    def set_transmission_usage(self, transmission_id: int, usage: str) -> None:
        self._exec("UPDATE transmissions SET usage = ? WHERE id = ?", (usage, transmission_id))

    def transmissions(self, task_id: int | None = None, backend: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM transmissions WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if backend is not None:
            sql += " AND backend = ?"
            params.append(backend)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._all(sql, tuple(params))
        for row in rows:
            row["files"] = json.loads(row["files"] or "[]")
        return rows

    # Cache and key/value --------------------------------------------------------------

    def cache_get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM cache WHERE key = ?", (key,))
        return row["value"] if row else None

    def cache_put(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO cache (key, value, created) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, created = excluded.created",
            (key, value, time.time()),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
