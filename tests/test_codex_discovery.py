import os

import pytest

from enoy.backends.base import BackendError
from enoy.backends.codex import CodexBackend, find_executable


def test_desktop_finds_bundled_codex_without_shell_path(tmp_path, monkeypatch):
    monkeypatch.setattr('enoy.backends.codex.shutil.which', lambda _: None)
    monkeypatch.setattr('enoy.backends.codex.Path.home', lambda: tmp_path / 'home')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    base = tmp_path / 'local' / 'OpenAI' / 'Codex' / 'bin'
    old, current = base / 'old' / 'codex.exe', base / 'current' / 'codex.exe'
    for index, path in enumerate((old, current)):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'fixture')
        os.utime(path, (100 + index, 100 + index))
    assert find_executable() == str(current)
    assert CodexBackend({})._base_cmd() == [str(current)]


def test_path_and_explicit_configuration_take_precedence(tmp_path, monkeypatch):
    configured = tmp_path / 'selected.exe'
    configured.write_bytes(b'fixture')
    monkeypatch.setattr('enoy.backends.codex.shutil.which', lambda _: 'path-codex')
    assert find_executable() == 'path-codex'
    assert find_executable(str(configured)) == str(configured)
    with pytest.raises(BackendError, match='Configured Codex executable'):
        CodexBackend({'executable': str(tmp_path / 'missing.exe')})._base_cmd()
