"""Local HTTP API and static UI, bound to 127.0.0.1 only.

Every /api request must carry the per-launch token (header X-Fusion-Token, or
?token= for the event stream) and a localhost Host header. This stops web pages
in a normal browser from driving the app (CSRF / DNS rebinding).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__, activity, artifacts as art_mod, config as config_mod, diagnostics, hardware, images, knowledge, paths
from . import memory_guard
from . import pccontrol as pc
from .backends.base import CLAUDE, LOCAL, BackendError, ChatMessage, ChatRequest
from .orchestrator import Orchestrator, TaskError
from .project import PathError
from .redact import redact

log = logging.getLogger("fusion.server")
UI_DIR = Path(__file__).parent / "ui"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    """Shared state for request handlers."""

    def __init__(self, orchestrator: Orchestrator, cfg_warnings: list[str],
                 folder_picker: Callable[[], str | None] | None = None, save_config: bool = True):
        self.orc = orchestrator
        self.cfg_warnings = cfg_warnings
        self.token = secrets.token_urlsafe(24)
        self.folder_picker = folder_picker
        self.file_saver: Callable[[str], str | None] | None = None  # Native "Save as" dialog, when there is a window.
        self.save_config = save_config
        self.last_seen = time.time()
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        self._register()

    def route(self, method: str, pattern: str):
        def deco(fn):
            self.routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    # Route table ------------------------------------------------------------------

    def _register(self) -> None:
        r = self.route
        orc = self.orc
        db = orc.db

        @r("GET", "/api/status")
        def status(q, body):
            local = orc.backends[LOCAL].status().as_dict()
            available, detail = orc.claude_available(refresh=q.get("refresh") == "1")
            claude = orc.backends[CLAUDE].status().as_dict() if q.get("refresh") == "1" else {
                "backend": CLAUDE, "available": available, "detail": detail,
                "verified": orc.backends[CLAUDE].verified,
            }
            claude["model_setting"] = orc.cfg["claude"]["model"]
            return {"version": __version__, "local": local, "claude": claude,
                    "routing": orc.cfg["routing"], "config_warnings": self.cfg_warnings,
                    "privacy_never": orc.cfg["privacy"]["cloud_policy"] == "never",
                    "team_default": orc.cfg["team"]["default_new_chats"],
                    "claude_last_verified": db.kv_get("claude_last_verified")}

        @r("GET", "/api/config")
        def get_config(q, body):
            return {"config": orc.cfg, "defaults": config_mod.DEFAULTS,
                    "routing_modes": config_mod.ROUTING_MODES, "workflows": config_mod.WORKFLOWS,
                    "aggressiveness": config_mod.AGGRESSIVENESS}

        @r("POST", "/api/config")
        def set_config(q, body):
            merged = config_mod.merge_update(orc.cfg, body.get("config", {}))
            # Permission profile and memory mode change only through their own endpoints,
            # which ask for confirmation and log the change in Activity.
            if merged["pc"]["profile"] != orc.cfg["pc"]["profile"] or merged["memory"]["mode"] != orc.cfg["memory"]["mode"]:
                raise ApiError(400, "Change the PC profile and memory mode in Settings → Memory & PC.")
            try:
                cfg = config_mod.validate_strict(merged)
            except config_mod.ConfigError as exc:
                raise ApiError(400, str(exc)) from exc
            if self.save_config:
                config_mod.save(cfg)
            orc.update_config(cfg)
            self.cfg_warnings = []
            return {"config": cfg}

        # Memory modes and PC access
        def save(cfg):
            cfg = config_mod.validate_strict(cfg)
            if self.save_config:
                config_mod.save(cfg)
            orc.update_config(cfg)
            return cfg

        @r("GET", "/api/memory")
        def memory_status(q, body):
            if orc.memory is None:
                cfg = orc.cfg["memory"]
                return {"available": False, "mode": cfg["mode"], "label": memory_guard.LABEL[cfg["mode"]],
                        "ceiling_mib": memory_guard.MODES[cfg["mode"]],
                        "modes": {k: {"label": memory_guard.LABEL[k], "ceiling_mib": v} for k, v in memory_guard.MODES.items()},
                        "detail": "The memory supervisor is not running (development mode or not Windows)."}
            if q.get("refresh"):
                orc.memory.tick()
            local = orc.backends.get(LOCAL)
            model = (local.settings.get("model") if local is not None else None) or None
            ctx = int(local.settings.get("num_ctx") or 0) if local is not None else 0
            return {"available": True, **orc.memory.status(model, ctx or None)}

        @r("POST", "/api/memory/mode")
        def memory_mode(q, body):
            mode = body.get("mode")
            if mode not in memory_guard.MODES:
                raise ApiError(400, "Unknown memory mode.")
            old = orc.cfg["memory"]["mode"]
            if mode == old:
                return {"config": orc.cfg}
            cfg = save(config_mod.merge_update(orc.cfg, {"memory": {"mode": mode}}))
            orc._act(None, "memory.mode_changed", f"Memory mode changed from {memory_guard.LABEL[old]} to "
                     f"{memory_guard.LABEL[mode]} ({memory_guard.MODES[mode]:,} MiB).", "user", outcome=mode)
            if orc.memory is not None:
                orc.memory.tick()  # Apply right away (unloads if the new limit is already exceeded).
            return {"config": cfg}

        @r("POST", "/api/memory/forget")
        def memory_forget(q, body):
            if orc.memory is None:
                raise ApiError(409, "The memory supervisor is not running.")
            n = orc.memory.forget()
            orc._act(None, "memory.measurements_cleared", f"Cleared {n} stored memory measurement(s); estimates start "
                     "again from the model file size.", "user")
            return {"cleared": n}

        @r("GET", "/api/hardware")
        def hardware_inventory(q, body):
            try:
                pc.check(orc.cfg, "hardware.read")
            except pc.PermissionDenied as exc:
                raise ApiError(403, str(exc)) from exc
            orc._act(None, "pc.hardware_read", "Hardware inventory read (Settings).", "user", outcome="read")
            return {"inventory": hardware.inventory(orc, max_age_s=0 if q.get("refresh") else 30)}

        @r("GET", "/api/pc")
        def pc_summary(q, body):
            return pc.summary(orc.cfg)

        @r("POST", "/api/pc/profile")
        def pc_profile(q, body):
            profile = body.get("profile")
            if profile not in pc.PROFILES:
                raise ApiError(400, "Unknown profile.")
            old = orc.cfg["pc"]["profile"]
            order = list(pc.PROFILES)
            if order.index(profile) > order.index(old) and body.get("confirm") is not True:
                raise ApiError(400, "Giving Enoy more access needs your confirmation.")
            cfg = save(config_mod.merge_update(orc.cfg, {"pc": {"profile": profile}}))
            orc._act(None, "pc.profile_changed", f"PC access changed from {pc.PROFILES[old][0]} to {pc.PROFILES[profile][0]}.",
                     "user", outcome=profile)
            return {"config": cfg, **pc.summary(cfg)}

        @r("POST", "/api/pc/apps")
        def pc_apps(q, body):
            apps = body.get("apps")
            if not isinstance(apps, list) or len(apps) > 50:
                raise ApiError(400, "Send a list of up to 50 programs.")
            clean = []
            for a in apps:
                name, path_ = str((a or {}).get("name", "")).strip()[:60], str((a or {}).get("path", "")).strip().strip('"')
                if not name or not path_:
                    raise ApiError(400, "Each program needs a name and a path.")
                if not path_.lower().endswith(".exe"):
                    raise ApiError(400, f"{name}: only .exe programs can be approved (no scripts or shortcuts).")
                clean.append({"name": name, "path": path_})
            cfg = save(config_mod.merge_update(orc.cfg, {"pc": {"approved_apps": clean}}))
            orc._act(None, "pc.apps_changed", f"Approved programs: {', '.join(a['name'] for a in clean) or 'none'}.", "user")
            return {"config": cfg, **pc.summary(cfg)}

        @r("GET", "/api/pc/windows")
        def pc_windows(q, body):
            try:
                pc.check(orc.cfg, "windows.observe")
            except pc.PermissionDenied as exc:
                raise ApiError(403, str(exc)) from exc
            return {"windows": [w.as_dict() for w in pc.list_windows()]}

        @r("GET", "/api/ollama/models")
        def ollama_models(q, body):
            try:
                return {"models": orc.backends[LOCAL].list_models()}
            except BackendError as exc:
                raise ApiError(503, str(exc)) from exc

        @r("POST", "/api/ollama/test")
        def ollama_test(q, body):
            return self._test_backend(LOCAL)

        @r("POST", "/api/claude/test")
        def claude_test(q, body):
            return self._test_backend(CLAUDE)

        @r("GET", "/api/diagnostics")
        def diag(q, body):
            return diagnostics.collect(orc, self.cfg_warnings)

        # Projects
        @r("GET", "/api/projects")
        def projects(q, body):
            general = str(Path(os.path.realpath(paths.general_chat_dir())))
            if q.get("all"):
                rows = db.list_projects()  # Projects page: every project with counts, no content.
            else:
                rows = [p for p in db.recent_projects(40) if p["last_opened"] > 0 and not p.get("archived")]
            for p in rows:
                p["is_default"] = p["path"] == general
            return {"projects": rows}

        @r("POST", "/api/projects")
        def create_project(q, body):
            return {"project": orc.create_space(body.get("name", ""), body.get("description", ""),
                                                body.get("instructions", ""))}

        @r("POST", r"/api/projects/(\d+)/update")
        def update_project(q, body, pid):
            fields = {k: body[k] for k in ("name", "description", "instructions") if isinstance(body.get(k), str)}
            return {"project": orc.update_project(int(pid), **fields)}

        @r("POST", r"/api/projects/(\d+)/delete")
        def delete_project(q, body, pid):
            if body.get("confirm") is not True:
                raise ApiError(400, "Deleting a project needs confirmation.")
            return {"result": orc.delete_project(int(pid))}

        @r("GET", r"/api/projects/(\d+)/files")
        def project_files(q, body, pid):
            self._project(pid)
            return {"files": db.project_files(int(pid)), "limit_bytes": knowledge.MAX_FILE_BYTES,
                    "extensions": sorted(knowledge.TEXT_EXTENSIONS)}

        @r("POST", r"/api/projects/(\d+)/files")
        def add_project_file(q, body, pid):
            self._project(pid)
            try:
                data = images.decode_upload(body.get("data", ""))
            except images.ImageError as exc:
                raise ApiError(400, "The file data is not valid.") from exc
            try:
                f, created = orc.add_project_file(int(pid), body.get("name", ""), data)
            except knowledge.KnowledgeError as exc:
                raise ApiError(400, str(exc)) from exc
            return {"file": f, "created": created}

        @r("GET", r"/api/projects/(\d+)/files/(\d+)")
        def project_file(q, body, pid, fid):
            self._project(pid)
            f = db.project_file(int(pid), int(fid))
            if not f:
                raise ApiError(404, "File not found in this project.")
            return {"file": f}

        @r("POST", r"/api/projects/(\d+)/files/(\d+)/private")
        def project_file_private(q, body, pid, fid):
            self._project(pid)
            if not db.project_file(int(pid), int(fid), with_content=False):
                raise ApiError(404, "File not found in this project.")
            db.set_project_file_private(int(pid), int(fid), bool(body.get("on")))
            return {"file": db.project_file(int(pid), int(fid), with_content=False)}

        @r("POST", r"/api/projects/(\d+)/files/(\d+)/delete")
        def delete_project_file(q, body, pid, fid):
            self._project(pid)
            if not db.delete_project_file(int(pid), int(fid)):
                raise ApiError(404, "File not found in this project.")
            return {"ok": True}

        # Artifacts
        @r("GET", "/api/artifacts")
        def artifacts(q, body):
            pid = int(q["project_id"]) if q.get("project_id", "").isdigit() else None
            return {"artifacts": db.list_artifacts(pid, (q.get("q") or "")[:200])}

        @r("POST", "/api/artifacts")
        def create_artifact(q, body):
            type_, content, language = art_mod.check(body.get("type", "markdown"), body.get("content", ""), body.get("language", ""))
            return {"artifact": db.create_artifact(art_mod.clean_title(body.get("title", "")), type_, content, language,
                                                   project_id=self._optional_project(body.get("project_id")))}

        @r("POST", "/api/artifacts/from-message")
        def artifact_from_message(q, body):
            # Copies the saved answer exactly. No model call.
            mid = int(body.get("message_id", 0))
            msg = db.get_message(mid)
            if not msg or msg["role"] != "assistant":
                raise ApiError(404, "Message not found.")
            block = body.get("block")
            draft = art_mod.from_message(msg["content"], int(block) if isinstance(block, int) else None)
            existing = db.find_artifact_from_message(mid, draft["content"])
            if existing:
                return {"artifact": existing, "created": False}
            task = db.get_task(msg["task_id"])
            project = db.get_project(task["project_id"]) if task else None
            pid = project["id"] if project and not orc.is_default_project(project) else None
            title = art_mod.clean_title(body.get("title") or draft["title"])
            return {"artifact": db.create_artifact(title, draft["type"], draft["content"], draft["language"], project_id=pid,
                                                   task_id=msg["task_id"], message_id=mid, note="Saved from chat"),
                    "created": True}

        @r("GET", r"/api/artifacts/(\d+)")
        def artifact(q, body, aid):
            return {"artifact": self._artifact(aid)}

        @r("POST", r"/api/artifacts/(\d+)/update")
        def update_artifact(q, body, aid):
            art = self._artifact(aid)
            if not isinstance(body.get("base_version"), int):
                raise ApiError(400, "base_version is required.")
            fields = {}
            if "title" in body:
                fields["title"] = art_mod.clean_title(body["title"])
            type_, content, language = art_mod.check(body.get("type", art["type"]), body.get("content", art["content"]),
                                                     body.get("language", art["language"]))
            fields.update(type=type_, content=content, language=language)
            try:
                return {"artifact": db.update_artifact(art["id"], body["base_version"],
                                                       note=str(body.get("note") or "Edited")[:art_mod.MAX_NOTE], **fields)}
            except art_mod.Conflict as exc:
                raise ApiError(409, str(exc)) from exc

        @r("GET", r"/api/artifacts/(\d+)/versions")
        def artifact_versions(q, body, aid):
            art = self._artifact(aid)
            return {"versions": db.artifact_versions(art["id"])}

        @r("GET", r"/api/artifacts/(\d+)/versions/(\d+)")
        def artifact_version(q, body, aid, ver):
            art = self._artifact(aid)
            v = db.artifact_version(art["id"], int(ver))
            if not v:
                raise ApiError(404, "Version not found.")
            return {"version": v}

        @r("POST", r"/api/artifacts/(\d+)/restore")
        def restore_artifact(q, body, aid):
            art = self._artifact(aid)
            v = db.artifact_version(art["id"], int(body.get("version", 0)))
            if not v:
                raise ApiError(404, "Version not found.")
            try:
                # Restoring adds a new version; later versions stay recoverable.
                return {"artifact": db.update_artifact(art["id"], int(body.get("base_version", -1)),
                                                       note=f"Restored version {v['version']}", title=v["title"],
                                                       type=v["type"], language=v["language"], content=v["content"])}
            except art_mod.Conflict as exc:
                raise ApiError(409, str(exc)) from exc

        @r("POST", r"/api/artifacts/(\d+)/project")
        def artifact_project(q, body, aid):
            art = self._artifact(aid)
            db.set_artifact_project(art["id"], self._optional_project(body.get("project_id")))
            return {"artifact": db.get_artifact(art["id"])}

        @r("POST", r"/api/artifacts/(\d+)/delete")
        def delete_artifact(q, body, aid):
            art = self._artifact(aid)
            if body.get("confirm") is not True:
                raise ApiError(400, "Deleting an artifact needs confirmation.")
            db.delete_artifact(art["id"])
            return {"ok": True}

        @r("POST", r"/api/artifacts/(\d+)/export")
        def export_artifact(q, body, aid):
            art = self._artifact(aid)
            name, mime, data = art_mod.export(art)
            if self.file_saver and not body.get("inline"):
                target = self.file_saver(name)
                if not target:
                    return {"saved": None}
                try:
                    Path(target).write_bytes(data)
                except OSError as exc:
                    raise ApiError(500, f"Could not save the file: {exc}") from exc
                return {"saved": str(target)}
            # No native dialog (browser or dev mode): the page downloads it.
            return {"filename": name, "mime": mime, "content": data.decode("utf-8")}

        @r("POST", r"/api/tasks/(\d+)/move")
        def move_task(q, body, tid):
            return {"task": orc.move_task(int(tid), int(body.get("project_id", 0)))}

        @r("POST", r"/api/tasks/(\d+)/rename")
        def rename_task(q, body, tid):
            return {"task": orc.rename_task(int(tid), body.get("title", ""))}

        @r("POST", r"/api/tasks/(\d+)/delete")
        def delete_task(q, body, tid):
            if body.get("confirm") is not True:
                raise ApiError(400, "Deleting a chat needs confirmation.")
            orc.delete_task(int(tid))
            return {"deleted": int(tid)}

        @r("POST", "/api/projects/open")
        def open_project(q, body):
            path = (body.get("path") or "").strip().strip('"')
            if not path:
                raise ApiError(400, "Choose a folder.")
            try:
                return {"project": orc.open_project(path)}
            except FileNotFoundError as exc:
                raise ApiError(404, str(exc)) from exc

        @r("POST", "/api/projects/default")
        def default_project(q, body):
            # Plain chat with no project: an empty folder in the app's data directory.
            return {"project": orc.open_project(str(paths.general_chat_dir()))}

        @r("POST", "/api/projects/pick")
        def pick(q, body):
            if not self.folder_picker:
                raise ApiError(501, "No folder dialog is available; type the folder path instead.")
            path = self.folder_picker()
            if not path:
                return {"project": None}
            return {"project": orc.open_project(path)}

        @r("POST", r"/api/projects/(\d+)/forget")
        def forget(q, body, pid):
            db.forget_project(int(pid))
            return {"ok": True}

        @r("GET", r"/api/projects/(\d+)")
        def project(q, body, pid):
            p = self._project(pid)
            ws = orc.workspace(p["id"])
            ws.invalidate()
            p["is_default"] = p["path"] == str(Path(os.path.realpath(paths.general_chat_dir())))
            return {"project": p, "facts": ws.detect()}

        @r("GET", r"/api/projects/(\d+)/tree")
        def tree(q, body, pid):
            p = self._project(pid)
            ws = orc.workspace(p["id"])
            ws.invalidate()
            return ws.tree()

        @r("GET", r"/api/projects/(\d+)/file")
        def file(q, body, pid):
            p = self._project(pid)
            ws = orc.workspace(p["id"])
            try:
                text, sha = ws.read_text(q.get("path", ""))
            except PathError as exc:
                raise ApiError(403, str(exc)) from exc
            except FileNotFoundError as exc:
                raise ApiError(404, f"File not found: {exc}") from exc
            return {"path": q.get("path"), "content": text, "sha": sha}

        @r("GET", r"/api/projects/(\d+)/search")
        def search(q, body, pid):
            p = self._project(pid)
            return {"results": orc.workspace(p["id"]).search(q.get("q", ""))}

        @r("POST", r"/api/projects/(\d+)/summarize")
        def summarize(q, body, pid):
            p = self._project(pid)
            return {"task": orc.summarize_project(p["id"])}

        # Tasks
        @r("GET", "/api/tasks")
        def tasks(q, body):
            pid = int(q["project_id"]) if q.get("project_id") else None
            return {"tasks": db.list_tasks(pid, int(q.get("limit", 100)))}

        @r("POST", "/api/tasks")
        def new_task(q, body):
            self._project(body.get("project_id"))
            kind = body.get("kind") if body.get("kind") in ("ask", "edit") else None
            backend = body.get("backend") if body.get("backend") in (LOCAL, CLAUDE) else None
            return {"task": orc.start_task(int(body["project_id"]), body.get("objective", ""), kind, backend,
                                           [f for f in body.get("files", []) if isinstance(f, str)],
                                           local_only=bool(body.get("local_only")),
                                           team=body.get("team") if isinstance(body.get("team"), bool) else None,
                                           images=body.get("images") or [])}

        @r("POST", "/api/images")
        def upload_image(q, body):
            try:
                return {"image": images.store(images.decode_upload(body.get("data", "")))}
            except images.ImageError as exc:
                raise ApiError(400, str(exc)) from exc

        @r("POST", r"/api/tasks/(\d+)/team")
        def task_team(q, body, tid):
            return {"task": orc.set_team(int(tid), bool(body.get("on")))}

        @r("GET", r"/api/tasks/(\d+)/team")
        def task_team_view(q, body, tid):
            return orc.team_view(int(tid))

        @r("POST", r"/api/tasks/(\d+)/local-only")
        def task_local_only(q, body, tid):
            return {"task": orc.set_task_local_only(int(tid), bool(body.get("on")))}

        @r("POST", r"/api/projects/(\d+)/local-only")
        def project_local_only(q, body, pid):
            return {"project": orc.set_project_local_only(int(pid), bool(body.get("on")))}

        @r("GET", "/api/routing/stats")
        def routing_stats(q, body):
            return orc.routing_stats()

        @r("GET", r"/api/tasks/(\d+)")
        def task(q, body, tid):
            t = db.get_task(int(tid))
            if not t:
                raise ApiError(404, "Task not found")
            return {
                "task": t,
                "messages": db.messages(t["id"]),
                "events": db.events(t["id"]),
                "changesets": db.list_changesets(t["id"]),
                "runs": db.list_command_runs(t["project_id"], t["id"]),
                "transmissions": [{k: v for k, v in x.items() if k != "content"} for x in db.transmissions(t["id"])],
            }

        @r("GET", r"/api/tasks/(\d+)/brief")
        def brief(q, body, tid):
            return {"brief": orc.continuation_brief(int(tid))}

        @r("GET", r"/api/transmissions/(\d+)")
        def transmission(q, body, xid):
            rows = [x for x in db.transmissions(None, None, 1000) if x["id"] == int(xid)]
            if not rows:
                raise ApiError(404, "Not found")
            return {"transmission": rows[0]}

        @r("POST", r"/api/tasks/(\d+)/message")
        def message(q, body, tid):
            backend = body.get("backend") if body.get("backend") in (LOCAL, CLAUDE) else None
            files = [f for f in body.get("files", []) if isinstance(f, str)]
            return {"task": orc.send_message(int(tid), body.get("text", ""), backend, files, body.get("images") or [])}

        for action, fn in (
            ("cancel", lambda tid: orc.cancel(tid)),
            ("resume", lambda tid: orc.resume(tid)),
            ("ask-claude", lambda tid: orc.ask_claude(tid)),
            ("claude/approve", lambda tid: orc.approve_claude(tid)),
            ("claude/decline", lambda tid: orc.decline_claude(tid)),
            ("handoff", lambda tid: orc.handoff_to_claude_code(tid)),
        ):
            self.routes.append(("POST", re.compile(rf"^/api/tasks/(\d+)/{action}$"),
                                (lambda f: lambda q, body, tid: {"result": f(int(tid))})(fn)))

        @r("POST", r"/api/tasks/(\d+)/fix")
        def fix(q, body, tid):
            return {"task": orc.fix_failure(int(tid), int(body["run_id"]))}

        @r("GET", r"/api/tasks/(\d+)/claude-preview")
        def claude_preview(q, body, tid):
            t = db.get_task(int(tid))
            return {"pending": (t or {}).get("state", {}).get("pending_claude")}

        # Change sets
        @r("GET", "/api/changesets")
        def changesets(q, body):
            pid = int(q["project_id"]) if q.get("project_id") else None
            return {"changesets": db.list_changesets(None, q.get("status") or None, pid)}

        @r("POST", r"/api/changesets/(\d+)/approve")
        def approve(q, body, cid):
            return {"changeset": orc.approve_changeset(int(cid))}

        @r("POST", r"/api/changesets/(\d+)/reject")
        def reject(q, body, cid):
            return {"changeset": orc.reject_changeset(int(cid), body.get("reason", ""))}

        @r("POST", r"/api/changesets/(\d+)/revert")
        def revert(q, body, cid):
            return {"changeset": orc.revert_changeset(int(cid))}

        # Commands
        @r("POST", "/api/commands/check")
        def check(q, body):
            return {"policy": orc.check_command(body.get("command", ""))}

        @r("POST", "/api/commands/run")
        def run(q, body):
            self._project(body.get("project_id"))
            task_id = int(body["task_id"]) if body.get("task_id") else None
            return orc.run_command(int(body["project_id"]), body.get("command", ""), bool(body.get("approved")), task_id)

        @r("POST", r"/api/commands/(\d+)/cancel")
        def cancel_cmd(q, body, rid):
            orc.cancel_command(int(rid))
            return {"ok": True}

        @r("GET", "/api/commands")
        def runs(q, body):
            return {"runs": db.list_command_runs(int(q["project_id"]))}

        @r("GET", "/api/transmissions")
        def transmissions(q, body):
            rows = db.transmissions(None, q.get("backend") or None, 200)
            return {"transmissions": [{k: v for k, v in x.items() if k != "content"} for x in rows]}

        # Activity panel. Built from stored state only; never calls a model.
        @r("GET", "/api/activity")
        def activity_snapshot(q, body):
            def num(key):
                return int(q[key]) if q.get(key, "").isdigit() else None
            task = -1 if q.get("task") == "focus" else num("task")
            return activity.snapshot(orc, num("focus"), task, min(num("limit") or 150, 500),
                                     refresh=q.get("refresh") == "1")

        @r("GET", "/api/activity/log")
        def activity_log(q, body):
            t = db.get_task(int(q.get("task_id", 0) or 0))
            if not t:
                raise ApiError(404, "Task not found")
            runs = []
            for run in db.list_command_runs(t["project_id"], t["id"], 10):
                out = (run["stdout"] + "\n" + run["stderr"]).strip().splitlines()
                runs.append({k: run[k] for k in ("id", "command", "status", "exit_code", "duration_s", "created")}
                            | {"tail": "\n".join(out[-40:])})
            log_file = paths.logs_dir() / "fusion.log"
            tail = ""
            if log_file.exists():
                with open(log_file, encoding="utf-8", errors="replace") as fh:
                    tail = redact("".join(fh.readlines()[-40:]))
            return {"task": t["title"], "activity": list(reversed(db.activity(t["id"], 1000))),
                    "steps": db.steps(t["id"]), "runs": runs, "app_log": tail}

        @r("POST", "/api/dispatch")
        def dispatch_pause(q, body):
            return orc.set_dispatch_paused(bool(body.get("paused")))

        @r("POST", "/api/activity/explain")
        def explain(q, body):
            # Optional and user-triggered: one local Qwen request. Claude is never used here.
            return orc.explain_workflow(int(body.get("task_id", 0)))

        @r("POST", "/api/heartbeat")
        def heartbeat(q, body):
            self.last_seen = time.time()
            return {"ok": True}

    def _project(self, pid: Any) -> dict:
        try:
            p = self.orc.db.get_project(int(pid))
        except (TypeError, ValueError):
            p = None
        if not p or p.get("archived"):
            raise ApiError(404, "Project not found. Open a project first.")
        return p

    def _optional_project(self, pid: Any) -> int | None:
        if pid in (None, "", 0):
            return None
        p = self._project(pid)
        if self.orc.is_default_project(p):
            return None
        return p["id"]

    def _artifact(self, aid: Any) -> dict:
        try:
            a = self.orc.db.get_artifact(int(aid))
        except (TypeError, ValueError):
            a = None
        if not a:
            raise ApiError(404, "Artifact not found. It may have been deleted.")
        return a

    def _test_backend(self, backend_name: str) -> dict:
        """A real, tiny request. Only ever triggered by the user's button press."""
        backend = self.orc.backends[backend_name]
        start = time.time()
        try:
            result = backend.chat(ChatRequest(messages=[
                ChatMessage("system", "You are a connection test."),
                ChatMessage("user", "Reply with exactly the word: READY"),
            ], purpose="test"))
        except BackendError as exc:
            return {"ok": False, "kind": exc.kind, "detail": str(exc), "seconds": round(time.time() - start, 1)}
        if backend_name == CLAUDE:
            self.orc.db.kv_set("claude_last_verified", time.time())
            self.orc._claude_status = None
        return {"ok": True, "reply": result.text.strip()[:200], "seconds": round(time.time() - start, 1),
                "usage": result.usage, "model": result.model}

    # Dispatch -----------------------------------------------------------------------

    def dispatch(self, method: str, path: str, query: dict, body: dict) -> Any:
        for m, pattern, fn in self.routes:
            if m != method:
                continue
            match = pattern.match(path)
            if match:
                return fn(query, body, *match.groups())
        raise ApiError(404, f"No such endpoint: {method} {path}")


def make_handler(app: App):
    allowed_hosts: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        server_version = "FusionAI"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # Route access logs to our logger, quietly.
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _host_ok(self) -> bool:
            if not allowed_hosts:
                port = self.server.server_address[1]
                allowed_hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})
            return self.headers.get("Host", "") in allowed_hosts

        def _send(self, status: int, payload: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            if not self._host_ok():
                self._json(403, {"error": "Bad host"})
                return
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if not url.path.startswith("/api/"):
                self._static(url.path)
                return
            token = self.headers.get("X-Fusion-Token") or query.get("token")
            if not token or not secrets.compare_digest(token, app.token):
                self._json(401, {"error": "Missing or wrong token"})
                return
            if url.path == "/api/events":
                self._events()
                return
            if method == "GET" and url.path.startswith("/api/images/"):
                try:
                    img = images.load(url.path[len("/api/images/"):])
                except images.ImageError:
                    self._json(404, {"error": "Image not found"})
                    return
                self._send(200, img.data, img.media_type, {"Cache-Control": "private, max-age=86400"})
                return
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 5_000_000:
                    self._json(413, {"error": "Request too large"})
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    self._json(400, {"error": "Body is not JSON"})
                    return
            try:
                result = app.dispatch(method, url.path, query, body)
                self._json(200, result if result is not None else {"ok": True})
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (TaskError, PathError, FileNotFoundError, KeyError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # Report, never crash the server.
                log.exception("API error on %s %s", method, url.path)
                self._json(500, {"error": f"Internal error: {exc}"})

        def _static(self, path: str) -> None:
            if path in ("", "/"):
                html = (UI_DIR / "index.html").read_text(encoding="utf-8")
                html = html.replace("__FUSION_TOKEN__", app.token)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                           {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                       "img-src 'self' data:; connect-src 'self'"})
                return
            rel = path.lstrip("/")
            if rel.startswith("static/"):
                target = (UI_DIR / rel[len("static/"):]).resolve()
                if UI_DIR.resolve() in target.parents and target.is_file():
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    if target.suffix == ".js":
                        ctype = "text/javascript"
                    self._send(200, target.read_bytes(), ctype)
                    return
            self._json(404, {"error": "Not found"})

        def _events(self) -> None:
            q = app.orc.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=15)
                        data = json.dumps(event, default=str)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass
            finally:
                app.orc.bus.unsubscribe(q)

    return Handler


class Server:
    def __init__(self, app: App, port: int = 0):
        self.app = app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
