"""The Build workspace as the UI sees it: one object behind the /api/build routes.

It joins the pieces (planner, store, git, runner, providers, planning.md) and runs slow work (planning,
task runs) as background jobs, one at a time per plan. Job results and errors are kept in the database
so the page can show them after a reload. With a synchronous orchestrator (tests) jobs run inline.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import gitinfo, paths, planfile, privacy
from .config import CLI_PROVIDERS
from .devgate import review_task
from .devplan import Planner, PlanError, ask_model, validate_plan
from .devrunner import BuildRunner
from .devstore import DevStore, TransitionError
from .gitwork import GitError, GitWork, init_repo, is_repo
from .project import INSTRUCTION_FILES, Workspace


class BuildError(ValueError):
    """A request the Build workspace refuses, with a reason for the user."""


class BuildService:
    def __init__(self, orc, providers):
        self.orc, self.providers = orc, providers
        self.store = DevStore(orc.db)
        self.planner = Planner(orc, providers, self.store)
        self._threads: dict[int, threading.Thread] = {}
        self._runners: dict[int, BuildRunner] = {}
        self._lock = threading.Lock()

    # Jobs ------------------------------------------------------------------------------------------

    def job(self, plan_id: int) -> dict:
        state = self.orc.db.kv_get(f"build.job.{plan_id}") or {"kind": "", "error": "", "result": None}
        t = self._threads.get(plan_id)
        return state | {"busy": bool(t and t.is_alive())}

    def _spawn(self, plan_id: int, kind: str, fn) -> dict:
        with self._lock:
            if self.job(plan_id)["busy"]:
                raise BuildError("This plan is already working on something. Wait for it or press Stop.")
            self.orc.db.kv_set(f"build.job.{plan_id}", {"kind": kind, "error": "", "result": None,
                                                        "started": time.time(), "finished": None})

            def work():
                error, result = "", None
                try:
                    result = fn()
                except (PlanError, BuildError, TransitionError, GitError, RuntimeError, ValueError) as exc:
                    error = str(exc) + ("" if not getattr(exc, "errors", None) else
                                        " " + "; ".join(exc.errors[:8]))
                except Exception as exc:  # Shown on the page instead of disappearing in a thread.
                    error = f"{type(exc).__name__}: {exc}"
                self.orc.db.kv_set(f"build.job.{plan_id}", {"kind": kind, "error": error, "result": result,
                                                            "finished": time.time()})
                self.orc._act(None, "build.job", f"Plan {plan_id}: {kind} " + ("failed: " + error if error else "finished."),
                              "system", "build", outcome="failed" if error else "")

            if self.orc.synchronous:
                work()
            else:
                t = threading.Thread(target=work, daemon=True, name=f"build-{plan_id}-{kind}")
                self._threads[plan_id] = t
                t.start()
        return self.job(plan_id)

    # Projects --------------------------------------------------------------------------------------

    def projects(self) -> list[dict]:
        rows = []
        for p in self.orc.db.list_projects():
            if p.get("kind", "folder") != "folder" or p.get("archived") or not Path(p["path"]).is_dir():
                continue
            if os.path.normcase(os.path.realpath(p["path"])) == os.path.normcase(os.path.realpath(paths.general_chat_dir())):
                continue
            root = Path(p["path"])
            repo = is_repo(root)
            branch = ""
            if repo:
                try:
                    branch = GitWork(root).current_branch()
                except GitError:
                    branch = ""
            ws = Workspace(root)
            rows.append({"id": p["id"], "name": p["name"], "path": p["path"], "git": repo, "branch": branch,
                         "uncommitted": len(gitinfo.changed_files(root) or []) if repo else 0,
                         "instructions": [n for n in INSTRUCTION_FILES if (root / n).is_file()],
                         "planning_md": (root / planfile.PLAN_FILE).is_file(),
                         "test_commands": ws.detect()["test_commands"] if len(ws.index) < 20000 else [],
                         "local_only": bool(p.get("local_only")), "plans": len(self.store.plans(p["id"]))})
        return rows

    def git_init(self, project_id: int, confirm: bool) -> None:
        if not confirm:
            raise BuildError("Creating a Git repository needs your confirmation.")
        init_repo(self.orc.db.get_project(project_id)["path"], confirm=True)
        self.orc._act(None, "build.git", "Created a Git repository with a first commit (secrets left out).", "user", "build")

    def draft_instructions(self, project_id: int) -> dict:
        """A CLAUDE.md draft from the planner model, for preview. Nothing is written."""
        root = Path(self.orc.db.get_project(project_id)["path"])
        return {"text": planfile.draft_instructions(self.planner, project_id), "exists": (root / "CLAUDE.md").exists()}

    def save_instructions(self, project_id: int, text: str, confirm: bool) -> dict:
        if not text.strip():
            raise BuildError("The instructions are empty.")
        try:
            result = planfile.save_instructions(Workspace(self.orc.db.get_project(project_id)["path"]), text, confirm)
        except FileExistsError as exc:
            raise BuildError(str(exc)) from None
        self.orc._act(None, "build.instructions", "Saved CLAUDE.md" +
                      (f" (previous version backed up as {result['backup'].name})." if result["backup"] else "."),
                      "user", "build")
        return {"saved": True, "backup": str(result["backup"]) if result["backup"] else None}

    # Roles -----------------------------------------------------------------------------------------

    def _role(self, role: str) -> dict:
        mid = self.orc.cfg["build"]["roles"].get(role) or ""
        model = next((m for m in self.orc.cfg["build"]["models"] if m["id"] == mid), None)
        if model is None:
            return {"id": "", "provider": "", "local": False, "ok": False, "reason": f"No {role} model is chosen."}
        pid = model["provider"]
        if pid in CLI_PROVIDERS:
            backend = self.orc.backends.get("claude" if pid == "claude_cli" else "codex")
            st = backend.status() if backend else None
            ok = bool(st and st.available)
            return {"id": mid, "provider": pid, "model": model.get("model", ""), "local": False, "ok": ok,
                    "reason": "" if ok else (st.detail if st else "Not installed.")}
        st = self.providers.status(pid)
        ok = st["state"] in ("ready", "verified")
        reasons = {"disabled": "The provider is turned off.", "no_key": "No API key saved.",
                   "no_connection": "No connection.", "key_refused": "The provider refused the key."}
        return {"id": mid, "provider": pid, "model": model.get("model", ""), "local": st["local"], "ok": ok,
                "reason": "" if ok else reasons.get(st["state"], st["state"])}

    def roles(self) -> dict:
        return {r: self._role(r) for r in ("planner", "implementer", "reviewer")}

    # Plans -----------------------------------------------------------------------------------------

    def plans(self, project_id: int) -> list[dict]:
        out = []
        for p in self.store.plans(project_id):
            tasks = self.store.tasks(p["id"]) if p["approved_version"] else []
            out.append(p | {"tasks": len(tasks), "done": sum(t["status"] == "completed" for t in tasks),
                            "versions": len(self.store.versions(p["id"])), "job": self.job(p["id"])})
        return out

    def _plan(self, plan_id: int) -> dict:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise BuildError(f"Unknown plan {plan_id}.")
        return plan

    def detail(self, plan_id: int) -> dict:
        plan = self._plan(plan_id)
        project = self.orc.db.get_project(plan["project_id"])
        versions = self.store.versions(plan_id)
        warnings = []
        if versions:
            _, warnings = validate_plan(versions[-1]["body"], Workspace(project["path"]),
                                        self.orc.cfg["permissions"]["command_allowlist"])
        tasks = []
        for t in self.store.tasks(plan_id) if plan["approved_version"] else []:
            tasks.append(t | {"runs": self.store.runs(t["id"]), "frozen": self.store.frozen(t["id"])})
        changed = []
        if plan["integration_branch"] and plan["base_commit"]:
            try:
                changed = GitWork(project["path"]).diff_names(plan["base_commit"], plan["integration_branch"])
            except GitError:
                changed = []
        return {"plan": plan, "project": {"id": project["id"], "name": project["name"], "path": project["path"]},
                "versions": versions, "warnings": warnings, "tasks": tasks, "job": self.job(plan_id),
                "roles": self.roles(), "changed_files": changed,
                "planning_md": planfile.render(self.store, plan_id) if versions else "",
                "parallel": bool(self.orc.cfg["build"]["parallel"])}

    def start_plan(self, project_id: int, goal: str) -> dict:
        plan = self.planner.start(project_id, goal)
        self._spawn(plan["id"], "planning", lambda: {"version": self.planner.draft(plan["id"])["version"]})
        return self.store.get_plan(plan["id"])

    def revise(self, plan_id: int, comment: str) -> dict:
        if not comment.strip():
            raise BuildError("Say what should change.")
        self._plan(plan_id)
        return self._spawn(plan_id, "revising", lambda: {"version": self.planner.revise(plan_id, comment)["version"]})

    def approve(self, plan_id: int, version: int) -> dict:
        return self.planner.approve(plan_id, version)

    def sync_planning(self, plan_id: int, confirm: bool) -> dict:
        result = planfile.sync(self.orc, self._plan(plan_id)["id"], confirm=confirm)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in result.items()}

    # Running -----------------------------------------------------------------------------------------

    def _runner(self, plan: dict) -> BuildRunner:
        build = self.orc.cfg["build"]
        project = self.orc.db.get_project(plan["project_id"])
        impl_id = build["roles"].get("implementer") or ""
        impl = next((m for m in build["models"] if m["id"] == impl_id), None)
        if impl is None:
            raise BuildError("Choose an implementer model in Settings → Providers → Roles first.")
        if impl["provider"] in CLI_PROVIDERS:
            raise BuildError("Implementers must be API or local models: Enoy has to run them through its own "
                             "limited tools, which the signed-in Claude and Codex apps do not use.")
        local = self.orc.cfg["build"]["providers"][impl["provider"]].get("local")
        reason = privacy.local_only_reason(self.orc.cfg, project)
        if reason and not local:
            raise BuildError(f"The implementer runs in the cloud, but {reason}. Choose a local implementer.")
        root = Path(project["path"])
        if not is_repo(root):
            raise BuildError("This folder is not a Git repository. Set up Git for it on the Project tab first.")
        review = None
        rev_id = build["roles"].get("reviewer") or ""
        if rev_id:
            rev = next(m for m in build["models"] if m["id"] == rev_id)
            rev_local = rev["provider"] not in CLI_PROVIDERS and build["providers"][rev["provider"]].get("local")
            if reason and not rev_local:
                raise BuildError(f"The reviewer runs in the cloud, but {reason}. Choose a local reviewer or none.")

            def review(spec, diff, gate, rev=rev):
                ask = lambda system, user: ask_model(self.orc, self.providers, rev, root, system, user, "review", "reviewer")
                return review_task(ask, rev_id, impl_id, spec, diff, gate)

        holder: dict = {}

        def complete_for(task):
            def complete(messages, tools):
                return self.providers.complete(impl_id, messages, max_output=impl.get("max_output") or 8192,
                                               role="implementer", purpose="build", tools=tools,
                                               cancel=holder["runner"].cancel, plan_id=plan["id"],
                                               dev_task_id=task["id"])
            return complete

        online = (lambda: True) if local else (lambda: self.providers.test(impl["provider"])["ok"])
        excluded = list(self.orc.cfg["permissions"]["extra_excluded_globs"])
        if not local:  # Files the user keeps from cloud models stay out of a cloud implementer's worktree.
            excluded += self.orc.cfg["privacy"]["cloud_excluded_globs"]
        runner = BuildRunner(self.store, GitWork(root), complete_for, self.orc.cfg["permissions"]["command_allowlist"],
                             review=review, sync=lambda pid: planfile.sync(self.orc, pid), venv_root=root,
                             online=online, parallel_allowed=bool(build["parallel"]),
                             max_parallel=min(4, self.orc.cfg["team"]["max_parallel"]), provider=impl["provider"],
                             model=impl.get("model", ""), excluded_globs=excluded,
                             log=lambda text: self.orc._act(None, "build.task", f"Plan {plan['id']}: {text}", "system",
                                                            "build"))
        holder["runner"] = runner
        return runner

    def run(self, plan_id: int, mode: str) -> dict:
        plan = self._plan(plan_id)
        if plan["approved_version"] is None:
            raise BuildError("Approve the plan before running it.")
        if mode == "parallel" and not self.orc.cfg["build"]["parallel"]:
            raise BuildError("Parallel runs are off. Turn them on in Settings → Providers once you trust the checks.")
        runner = self._runner(plan)
        self._runners[plan_id] = runner
        runner.resume(plan_id)

        def go():
            if mode == "next":
                ready = runner.ready(plan_id)
                if not ready:
                    return {"status": "idle", "ran": []}
                return {"status": "ran", "ran": [runner.run_task(ready[0]["id"])]}
            report = runner.run_parallel(plan_id) if mode == "parallel" else runner.run_all(plan_id)
            return {"status": report.status, "ran": report.ran, "reason": report.reason}

        if mode not in ("next", "all", "parallel"):
            raise BuildError("Unknown run mode.")
        return self._spawn(plan_id, f"run ({mode})", go)

    def run_task(self, task_id: int) -> dict:
        task = self.store.get_task(task_id)
        if task is None:
            raise BuildError("Unknown task.")
        runner = self._runner(self._plan(task["plan_id"]))
        self._runners[task["plan_id"]] = runner
        return self._spawn(task["plan_id"], f"run {task['task_key']}", lambda: runner.run_task(task_id))

    def stop(self, plan_id: int) -> None:
        runner = self._runners.get(plan_id)
        if runner:
            runner.stop()

    def retry(self, task_id: int) -> dict:
        task = self.store.get_task(task_id)
        if task is None or task["status"] not in ("failed", "blocked", "waiting_network", "interrupted"):
            raise BuildError("Only a failed, blocked, waiting or interrupted task can be retried.")
        return self.store.transition(task_id, "ready", blocker="")

    def merge(self, plan_id: int, confirm: bool) -> dict:
        if not confirm:
            raise BuildError("Merging into your branch changes your files. Confirm it first.")
        plan = self._plan(plan_id)
        result = GitWork(self.orc.db.get_project(plan["project_id"])["path"]).merge_into_current(plan_id)
        self.orc._act(None, "build.merge", f"Merge of plan {plan_id} into your branch: " +
                      ("done (" + result.commit[:12] + ")." if result.ok else "refused: " + result.reason),
                      "user", "build", outcome="" if result.ok else "failed")
        return {"ok": result.ok, "commit": result.commit, "conflicts": result.conflicts, "reason": result.reason}

    def call_detail(self, call_id: int) -> dict:
        row = self.orc.db._one("SELECT id, provider, model, role, purpose, status, chars, content, input_tokens, "
                               "cached_tokens, output_tokens, cost_usd, cost_source, error, created FROM model_calls "
                               "WHERE id = ?", (call_id,))
        if row is None:
            raise BuildError("Unknown request.")
        return row

    # Monitoring ----------------------------------------------------------------------------------------

    def monitor(self) -> dict:
        db = self.orc.db
        runs = db._all("SELECT r.*, t.task_key, t.title, t.plan_id FROM dev_runs r JOIN dev_tasks t ON t.id = r.task_id "
                       "ORDER BY r.id DESC LIMIT 30")
        calls = db._all("SELECT id, provider, model, role, purpose, plan_id, dev_task_id, status, input_tokens, "
                        "cached_tokens, output_tokens, cost_usd, cost_source, reserved_usd, error, created "
                        "FROM model_calls ORDER BY id DESC LIMIT 60")
        attention = db._all("SELECT t.id, t.task_key, t.title, t.status, t.blocker, t.plan_id FROM dev_tasks t "
                            "WHERE t.status IN ('failed', 'blocked', 'waiting_network', 'interrupted') ORDER BY t.id DESC LIMIT 30")
        totals = db._one("SELECT COALESCE(SUM(input_tokens), 0) AS input, COALESCE(SUM(cached_tokens), 0) AS cached, "
                         "COALESCE(SUM(output_tokens), 0) AS output, COUNT(*) AS calls FROM model_calls")
        activity = db._all("SELECT kind, agent, text, outcome, created FROM activity WHERE task_id IS NULL AND "
                           "(kind LIKE 'build.%' OR kind LIKE 'provider.%') ORDER BY id DESC LIMIT 40")
        return {"runs": runs, "calls": calls, "attention": attention, "totals": totals, "activity": activity,
                "spend": {pid: self.providers.spend.totals(pid) for pid in self.orc.cfg["build"]["providers"]}}
