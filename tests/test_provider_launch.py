import base64
from unittest.mock import Mock

import pytest

from enoy import provider_launch


@pytest.mark.parametrize('provider,action,suffix', [
    ('codex', 'login', ' login'), ('claude', 'login', ' auth login'),
    ('codex', 'launch', ''), ('claude', 'launch', ''),
])
def test_launch_fixed_command(monkeypatch, provider, action, suffix):
    monkeypatch.setattr(provider_launch.os, 'name', 'nt')
    finder = provider_launch.codex if provider == 'codex' else provider_launch.claude_code
    monkeypatch.setattr(finder, 'find_executable', lambda _: "C:/User's folder/tool.cmd")
    monkeypatch.setattr(provider_launch.paths, 'claude_workdir', lambda: 'C:/neutral')
    popen = Mock()
    monkeypatch.setattr(provider_launch.subprocess, 'Popen', popen)
    monkeypatch.setattr(provider_launch.subprocess, 'CREATE_NEW_CONSOLE', 16, raising=False)
    assert provider_launch.launch(provider, {}, action)['opened']
    args = popen.call_args.args[0]
    assert base64.b64decode(args[-1]).decode('utf-16-le') == "& 'C:/User''s folder/tool.cmd'" + suffix
    assert popen.call_args.kwargs['cwd'] == 'C:/neutral'
    assert popen.call_args.kwargs['creationflags'] == 16


@pytest.mark.parametrize('provider,settings,action', [
    ('other', {}, 'login'), ('codex', {}, 'arbitrary-command'),
    ('claude', {'enabled': False}, 'launch'),
])
def test_reject_invalid_launch(provider, settings, action):
    with pytest.raises(ValueError):
        provider_launch.launch(provider, settings, action)


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(provider_launch.codex, 'find_executable', lambda _: None)
    with pytest.raises(ValueError, match='not found'):
        provider_launch.launch('codex', {}, 'launch')
