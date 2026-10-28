"""planning.md, the archive of approved plans, and CLAUDE.md drafts.

The database is the plan's source of truth. planning.md is a readable copy Enoy writes into the
project: open tasks with their full specification, finished tasks with only their summary, commit
and evidence. Enoy never overwrites a planning.md that someone else wrote or edited without asking.
Approved versions are also archived as read-only JSON files with their SHA-256, for auditing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from . import paths
from .devstore import DevStore, fingerprint
from .patches import _write_atomic
from .project import Workspace
from .redact import redact

MARKER = "<!-- enoy:planning "
PLAN_FILE = "planning.md"


class ArchiveError(RuntimeError):
    """An archived plan is missing, not approved, or does not match its fingerprint."""


# Rendering ---------------------------------------------------------------------------------

def _ml(text: str, indent: str = "    ") -> str:
    return str(text).strip().replace("\n", "\n" + indent)


def _bullets(items: list, empty: str = "- None listed.") -> list[str]:
    return [f"- {_ml(i, '  ')}" for i in items] or [empty]


def _code_list(items: list) -> str:
    return ", ".join(f"`{i}`" for i in items) or "none"


def _open_task(t: dict) -> list[str]:
    out = [f"  - Objective: {_ml(t['objective'])}",
           f"  - Allowed files: {_code_list(t['allowed_files'])}",
           f"  - Test files: {_code_list(t['test_files'])}",
           f"  - Specification: {_ml(t['spec'])}",
           f"  - Inputs and outputs: {_ml(t['inputs_outputs'])}",
           f"  - Edge cases: {'; '.join(t['edge_cases']) or 'none listed'}",
           "  - Tests first:"]
    out += [f"    - `{x['name']}` in `{x['file']}`; fails before the change with: {_ml(x['expect_failure'], '      ')}"
            for x in t["tests"]]
    out += [f"  - Verify: {_code_list(t['verify_commands'])}",
            f"  - Regression: {_code_list(t['regression_commands'])}",
            "  - Acceptance:"]
    out += [f"    - {_ml(a, '      ')}" for a in t["acceptance"]]
    out += [f"  - Done when: {_ml(t['completion'])}",
            f"  - Stop and report if: {'; '.join(t['blockers']) or 'nothing listed'}"]
    return out


def render(store: DevStore, plan_id: int) -> str:
    """planning.md for a plan. The same plan state always gives the same text."""
    plan = store.get_plan(plan_id)
    approved = plan["approved_version"]
    v = store.version(plan_id, approved) if approved else store.versions(plan_id)[-1]
    body = v["body"]
    rows = {t["task_key"]: t for t in store.tasks(plan_id)} if approved else {}
    done = sum(1 for t in rows.values() if t["status"] == "completed")
    status = (f"approved (version {approved}). {done} of {len(body['tasks'])} tasks completed." if approved
              else f"draft (version {v['version']}), waiting for your approval.")
    lines = [f"{MARKER}plan={plan_id} version={v['version']} sha={v['sha'][:12]} -->",
             f"# {plan['title']}", "", f"Goal: {_ml(plan['goal'], '')}", "", f"Status: {status}", "",
             "Enoy writes this file from the plan it keeps. Change the plan in Enoy's Build workspace; "
             "edits made here are not read back.", "",
             "## Summary", "", body.get("summary") or "None given.", "",
             "## Architecture", "", body.get("architecture") or "None given.", "",
             "## Risks", "", *_bullets(body.get("risks") or []), "",
             "## Open questions", "", *_bullets(body.get("questions") or []), "",
             "## Tasks", ""]
    for t in body["tasks"]:
        row = rows.get(t["id"])
        label = row["status"].replace("_", " ") if row else "waiting for approval"
        deps = f"; depends on {', '.join(t['depends_on'])}" if t["depends_on"] else ""
        if row and row["status"] == "completed":
            lines.append(f"- [x] **{t['id']} {t['title']}** (completed)")
            if row["summary"]:
                lines.append(f"  - Summary: {_ml(row['summary'])}")
            lines.append(f"  - Commit: `{row['commit_id']}`")
            if row["evidence"]:
                lines.append("  - Evidence: " + "; ".join(f"{k}: {row['evidence'][k]}" for k in sorted(row["evidence"])))
            continue
        lines.append(f"- [ ] **{t['id']} {t['title']}** ({label}{deps})")
        if row and row["blocker"]:
            lines.append(f"  - Blocker: {_ml(row['blocker'])}")
        lines += _open_task(t)
    if approved:
        lines += ["", f"Approved specification: version {approved}, SHA-256 {v['sha']}. "
                      "The full text is archived by Enoy."]
    return "\n".join(lines) + "\n"


# Archive -----------------------------------------------------------------------------------

def archive_dir(plan_id: int) -> Path:
    return paths.sub_dir("plans") / str(plan_id)


def archive_version(store: DevStore, plan_id: int, version: int) -> Path:
    """Write an approved version as read-only JSON plus its SHA-256. Writing it again only verifies it."""
    v = store.version(plan_id, version)
    if v is None or v["approved"] is None:
        raise ArchiveError("Only approved plan versions are archived.")
    folder = archive_dir(plan_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"v{version}.json"
    if path.exists():
        load_archive(store, plan_id, version)
        return path
    data = {"plan_id": plan_id, "version": version, "sha": v["sha"], "approved": v["approved"],
            "author": v["author"], "comment": v["comment"], "body": v["body"]}
    for target, text in ((path, json.dumps(data, indent=1, ensure_ascii=False)), (path.with_suffix(".sha256"), v["sha"] + "\n")):
        _write_atomic(target, text)
        os.chmod(target, stat.S_IREAD)
    return path


def load_archive(store: DevStore, plan_id: int, version: int) -> dict:
    """The archived plan body, after checking it against its .sha256 file and the database."""
    path = archive_dir(plan_id) / f"v{version}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        recorded = path.with_suffix(".sha256").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"The archive of plan {plan_id} version {version} cannot be read: {exc}") from exc
    v = store.version(plan_id, version)
    expected = v["sha"] if v else None
    if not isinstance(data, dict) or fingerprint(data.get("body")) != recorded or recorded != expected \
            or data.get("sha") != expected:
        raise ArchiveError(f"The archive of plan {plan_id} version {version} does not match its fingerprint. "
                           "It was changed after approval.")
    return data["body"]


# Writing planning.md ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _backup(path: Path, kind: str) -> Path:
    folder = paths.backups_dir() / kind
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000}{path.suffix}"
    shutil.copy2(path, dest)
    return dest


def sync(orc, plan_id: int, confirm: bool = False) -> dict:
    """Write planning.md into the project if that is safe; otherwise return why not and the difference.

    Safe means: the file is absent, or it is exactly what Enoy wrote last time. Anything else (a file
    someone edited, or one Enoy never wrote) is replaced only with confirm=True, after a backup.
    """
    store = DevStore(orc.db)
    plan = store.get_plan(plan_id)
    ws = Workspace(orc.db.get_project(plan["project_id"])["path"])
    target = ws.resolve(PLAN_FILE)
    text = render(store, plan_id)
    key = f"build.planfile.{plan['project_id']}"
    backup = None
    if target.exists():
        current = target.read_bytes()
        if _sha(current) != (orc.db.kv_get(key) or {}).get("sha"):
            old = current.decode("utf-8", "replace")
            reason = "changed" if old.startswith(MARKER) else "not_enoy"
            if not confirm:
                diff = "".join(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                                    "planning.md (now)", "planning.md (Enoy)"))
                return {"written": False, "reason": reason, "diff": diff, "path": target}
            backup = _backup(target, "planning")
    _write_atomic(target, text)
    orc.db.kv_set(key, {"sha": _sha(text.encode("utf-8")), "plan": plan_id})
    orc._act(None, "build.planfile", f"Updated planning.md in {ws.name}" +
             (f" (your previous version is saved in {backup.name})." if backup else "."), "system", "plan")
    return {"written": True, "path": target, "backup": backup}


# CLAUDE.md ------------------------------------------------------------------------------------

INSTRUCTIONS_SYSTEM = """You write a CLAUDE.md instructions file for this repository. You edit nothing.
Inspect the repository with the read-only tools first. Then return the complete file in one ```markdown block.
Cover only what you confirmed in the files: what the project is, how to install, run, test and build it, the folder
layout, coding conventions visible in the code, and things to avoid. Be short and concrete. Never include secrets."""


def draft_instructions(planner, project_id: int) -> str:
    """A CLAUDE.md draft from the planner model, for the user to preview. Nothing is written."""
    model = planner._model()
    ws = planner._workspace(project_id, planner._is_cloud(model))
    from .devplan import planner_context
    text = planner._ask(model, ws, planner_context(ws, "Write a CLAUDE.md instructions file for this project."),
                        system=INSTRUCTIONS_SYSTEM)
    m = re.search(r"```(?:markdown|md)\s*\n(.*?)```", text, re.S)
    return redact(m.group(1) if m else text.strip() + "\n")


def save_instructions(ws: Workspace, text: str, confirm: bool = False, name: str = "CLAUDE.md") -> dict:
    """Write CLAUDE.md. An existing file is replaced only with confirm=True, and is backed up first."""
    target = ws.resolve(name)
    backup = None
    if target.exists():
        if not confirm:
            raise FileExistsError(f"{name} already exists. Confirm to replace it; Enoy keeps a backup.")
        backup = _backup(target, "instructions")
    _write_atomic(target, text)
    return {"path": target, "backup": backup}
