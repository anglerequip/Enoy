"""Contract tests for removing the local provider rather than hiding its controls."""
from pathlib import Path

from conftest import FakeBackend
from enoy.app import build
from enoy.config import validate
from enoy.backends.base import CODEX, CLAUDE


def test_app_registers_exactly_the_cloud_providers(app_home):
    cfg, warnings = validate({})
    assert not warnings
    app, orc = build(cfg, synchronous=True, save_config=False)
    try:
        assert set(orc.backends) == {CODEX, CLAUDE}
        assert 'ollama' not in cfg and 'memory' not in cfg
        patterns = [pattern.pattern for _, pattern, _ in app.routes]
        assert not any('ollama' in p or '/api/memory' in p for p in patterns)
        package = Path(__file__).resolve().parents[1] / 'enoy'
        # Ollama is back only as an optional, off-by-default Build provider (planning.md, decision 3);
        # chats still use the cloud providers only, and the old chat-wide memory supervisor stays removed.
        assert cfg['build']['providers']['ollama']['enabled'] is False
        assert not (package / 'memory_guard.py').exists()
    finally:
        orc.db.close()


def test_old_chat_provider_override_can_return_to_auto(orc_factory, project):
    orc = orc_factory()
    orc.backends[CODEX] = FakeBackend(CODEX, ['MODE: answer\nContinued using ChatGPT.'])
    pid = orc.open_project(str(project))['id']
    task = orc.db.create_task(pid, 'Old chat', 'Earlier request', 'ask', state={'override':'qwen'})
    orc.db.update_task(task['id'], status='completed', owner='qwen')
    orc.send_message(task['id'], 'Continue this conversation')
    saved = orc.db.get_task(task['id'])
    assert saved['status'] == 'completed' and saved['owner'] == CODEX
    assert saved['state']['override'] is None
