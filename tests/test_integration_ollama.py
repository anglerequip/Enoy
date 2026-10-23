"""Real local integration tests against Ollama. Skipped when Ollama or the model is unavailable.

Run only these with:  python -m pytest -m integration -s
No Claude calls are made here.
"""

import time

import pytest

from fusion_ai import config as config_mod
from fusion_ai.backends.base import CLAUDE, LOCAL, ChatMessage, ChatRequest
from fusion_ai.backends.ollama import OllamaBackend
from fusion_ai.orchestrator import EventBus, Orchestrator
from fusion_ai.storage import Storage

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def qwen():
    settings = dict(config_mod.DEFAULTS["ollama"])
    backend = OllamaBackend(settings)
    status = backend.status()
    if not status.available:
        pytest.skip(f"Ollama/Qwen not available: {status.detail}")
    return backend


def test_qwen_answers(qwen):
    start = time.monotonic()
    result = qwen.chat(ChatRequest(messages=[ChatMessage("user", "Reply with exactly the word READY and nothing else.")]))
    print(f"\nQwen reply {result.text!r} in {time.monotonic() - start:.1f} s, usage {result.usage}")
    assert "READY" in result.text.upper()
    loaded = qwen.status().extra.get("loaded")
    print(f"Loaded model memory: {loaded}")


def test_qwen_proposes_a_valid_change_through_the_orchestrator(qwen, project, tmp_path):
    """The real model must produce an edit that passes Enoy's validation."""
    cfg, _ = config_mod.validate({})
    cfg["routing"]["workflow"] = "local_only"
    cfg["tools"]["agent"] = False  # The classic flow: one proposal, then your approval (agent mode: next test).

    class NoClaude:
        name = CLAUDE
        verified = False
        settings = cfg["claude"]

        def status(self):
            from fusion_ai.backends.base import BackendStatus
            return BackendStatus(CLAUDE, False, "disabled for this test")

        def chat(self, request):
            raise AssertionError("Claude must not be called in integration tests")

    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: qwen, CLAUDE: NoClaude()}, EventBus(),
                       backups_dir=tmp_path / "bk", synchronous=True)
    pid = orc.open_project(str(project))["id"]
    start = time.monotonic()
    task = orc.start_task(pid, "Add a multiply(a, b) function to calculator.py that returns a * b.", kind="edit")
    task = orc.db.get_task(task["id"])
    print(f"\nTask finished as {task['status']} after {task['local_attempts']} local attempt(s) in {time.monotonic() - start:.1f} s")
    for e in orc.db.events(task["id"]):
        print(f"  [{e['type']}] {e['text'][:200]}")
    assert task["status"] == "awaiting_approval", task["summary"]
    cs = orc.db.list_changesets(task["id"], "pending")[0]
    print(cs["files"][0]["diff"])
    orc.approve_changeset(cs["id"])
    import importlib.util
    spec = importlib.util.spec_from_file_location("calc", project / "calculator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.multiply(6, 7) == 42
    assert module.subtract(5, 3) == 2  # Existing behaviour intact.


def test_qwen_agent_changes_and_checks_the_project(qwen, project, tmp_path):
    """Agent mode with the real model: it edits the files itself and runs the real tests."""
    from fusion_ai.backends.base import BackendStatus
    cfg, _ = config_mod.validate({})
    cfg["routing"]["workflow"] = "local_only"
    cfg["tools"]["agent"] = True

    class NoClaude:
        name = CLAUDE
        verified = False
        settings = cfg["claude"]

        def status(self):
            return BackendStatus(CLAUDE, False, "disabled for this test")

        def chat(self, request):
            raise AssertionError("Claude must not be called in integration tests")

    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: qwen, CLAUDE: NoClaude()}, EventBus(),
                       backups_dir=tmp_path / "bk", synchronous=True)
    pid = orc.open_project(str(project))["id"]
    start = time.monotonic()
    task = orc.start_task(pid, "Add a multiply(a, b) function to calculator.py that returns a * b, "
                               "add a test for it, and run the tests.", backend=LOCAL)
    task = orc.db.get_task(task["id"])
    calls = [a["text"].split("→ ")[-1][:60] for a in reversed(orc.db.activity(task["id"])) if a["kind"] == "tool.started"]
    calls = [c.encode("ascii", "replace").decode() for c in calls]  # The Windows console may not print every character.
    print(f"\nAgent finished as {task['status']} in {time.monotonic() - start:.1f} s; tools: {calls}")
    assert task["status"] == "completed", task["summary"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("calc", project / "calculator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.multiply(6, 7) == 42 and module.subtract(5, 3) == 2
    assert orc.db.list_changesets(task["id"], "applied"), "the agent's edits are one undoable change set"
