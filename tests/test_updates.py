import hashlib
import io
import json
import sys
import urllib.error

import pytest

from enoy import updates, runtime


def release_data(version='9.0.0', payload=b'installer'):
    prefix = f'https://github.com/{updates.REPOSITORY}/releases/download/v{version}/'
    return {'tag_name': 'v' + version, 'assets': [
        {'name': updates.ASSET, 'size': len(payload), 'browser_download_url': prefix + updates.ASSET,
         'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()},
        {'name': updates.ASSET + '.sha256', 'browser_download_url': prefix + updates.ASSET + '.sha256'}]}


def test_release_requires_stable_version_and_project_assets(monkeypatch):
    data = release_data()
    monkeypatch.setattr(updates, '_open', lambda url: io.BytesIO(json.dumps(data).encode()))
    assert updates.latest_release()['version'] == '9.0.0'
    data['assets'][0]['browser_download_url'] = 'https://example.com/installer.exe'
    with pytest.raises(ValueError, match='does not belong'):
        updates.latest_release()
    data['prerelease'] = True
    with pytest.raises(ValueError, match='stable'):
        updates.latest_release()


def test_no_published_release(monkeypatch):
    def missing(url):
        raise urllib.error.HTTPError(url, 404, 'Not found', {}, None)
    monkeypatch.setattr(updates, '_open', missing)
    assert updates.latest_release() is None


@pytest.mark.parametrize('version', ['v1.2.3-rc1', '../1.2.3', '1.2', None, 'v1.2.3/other'])
def test_bad_version(version):
    with pytest.raises(ValueError):
        updates.version_tuple(version)


@pytest.mark.parametrize('corrupt', [False, True])
def test_download_integrity_and_cleanup(tmp_path, monkeypatch, corrupt):
    payload = b'new installer'
    metadata = release_data(payload=payload)
    monkeypatch.setattr(updates, '_open', lambda url: io.BytesIO(json.dumps(metadata).encode()))
    release = updates.latest_release()
    checksum = (hashlib.sha256(payload).hexdigest() + '  ' + updates.ASSET).encode()
    monkeypatch.setattr(updates, '_open', lambda url: io.BytesIO(checksum if url.endswith('.sha256') else b'x' * len(payload) if corrupt else payload))
    if corrupt:
        with pytest.raises(ValueError, match='integrity'):
            updates.download_release(release, tmp_path)
        assert not (tmp_path / updates.ASSET).exists()
    else:
        assert updates.download_release(release, tmp_path).read_bytes() == payload
    assert not (tmp_path / (updates.ASSET + '.part')).exists()


def test_update_installs_only_when_idle_and_backs_up_first(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(updates, 'latest_release', lambda: {'version': '9.0.0', 'release_url': updates.RELEASES_URL})
    monkeypatch.setattr(updates, 'download_release', lambda *a: tmp_path / 'installer.exe')
    monkeypatch.setattr(updates, 'backup_user_data', lambda: events.append('backup'))
    monkeypatch.setattr(updates, 'launch_installer', lambda path: events.append('install'))
    monkeypatch.setattr(updates.time, 'sleep', lambda _: None)
    manager = updates.UpdateManager(shutdown=lambda: events.append('shutdown'))
    manager._run(True)
    assert events == ['backup', 'install', 'shutdown']
    assert manager.snapshot()['status'] == 'installing'
    events.clear()
    manager = updates.UpdateManager(busy=lambda: True)
    with pytest.raises(ValueError, match='running tasks'):
        manager.start()
    manager._run(True)
    assert manager.snapshot()['status'] == 'available'
    assert events == []


def test_current_and_network_failure_never_install(monkeypatch):
    monkeypatch.setattr(updates, 'latest_release', lambda: {'version': updates.__version__})
    manager = updates.UpdateManager()
    manager._run(True)
    assert manager.snapshot()['status'] == 'current'
    def failure():
        raise OSError('Offline')
    monkeypatch.setattr(updates, 'latest_release', failure)
    manager._run(True)
    assert manager.snapshot()['status'] == 'error'


def test_packaged_tools_have_console_launcher(monkeypatch, tmp_path):
    assert runtime.module_command('enoy.tools.mcp_server') == [sys.executable, '-m', 'enoy.tools.mcp_server']
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'Enoy.exe'))
    assert runtime.module_command('enoy.blender_bridge') == [str(tmp_path / 'Enoy-tools.exe'), '--module', 'enoy.blender_bridge']
    with pytest.raises(ValueError):
        runtime.module_command('arbitrary.module')


def test_update_backup_includes_sqlite_data(app_home):
    import sqlite3
    from enoy import paths
    with sqlite3.connect(paths.database_path()) as db:
        db.execute('CREATE TABLE marker (value TEXT)')
        db.execute("INSERT INTO marker VALUES ('saved chat')")
    paths.config_path().write_text('{"my_setting":true}', encoding='utf-8')
    updates.backup_user_data()
    backup = next((app_home / 'update-backups').iterdir())
    with sqlite3.connect(backup / 'enoy.db') as db:
        assert db.execute('SELECT value FROM marker').fetchone()[0] == 'saved chat'
    assert json.loads((backup / 'config.json').read_text()) == {'my_setting': True}
