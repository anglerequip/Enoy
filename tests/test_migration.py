import sqlite3

from enoy.legacy import OLD_DATA_DIR, OLD_DATABASE, OLD_ENV, data_home


def test_migration_preserves_database_assets_and_original(tmp_path, monkeypatch):
    monkeypatch.delenv("ENOY_HOME", raising=False)
    monkeypatch.delenv(OLD_ENV, raising=False)
    old = tmp_path / OLD_DATA_DIR
    (old / "images").mkdir(parents=True)
    (old / "images" / "saved.png").write_bytes(b"saved image")
    (old / "config.json").write_text('{"claude":{"model":"my-model"}}')
    (old / "instance.json").write_text('{"pid":123}')
    with sqlite3.connect(old / OLD_DATABASE) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE chats (message TEXT)")
        db.execute("INSERT INTO chats VALUES ('Keep this chat')")
        db.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, path TEXT UNIQUE)")
        db.execute("INSERT INTO projects VALUES (1, ?)", (str(old / "general-chat"),))
        db.execute("INSERT INTO projects VALUES (2, ?)", (str(tmp_path / "external-code"),))
        db.commit()
        home = data_home(tmp_path)
    assert home == tmp_path / "Enoy"
    with sqlite3.connect(home / "enoy.db") as db:
        assert db.execute("SELECT message FROM chats").fetchone()[0] == "Keep this chat"
        assert db.execute("SELECT path FROM projects WHERE id=1").fetchone()[0] == str(home / "general-chat")
        assert db.execute("SELECT path FROM projects WHERE id=2").fetchone()[0] == str(tmp_path / "external-code")
    assert (home / "images" / "saved.png").read_bytes() == b"saved image"
    assert (home / "config.json").read_text() == (old / "config.json").read_text()
    assert (old / OLD_DATABASE).is_file()
    assert not (home / OLD_DATABASE).exists() and not (home / "instance.json").exists()
    (home / "config.json").write_text("new settings")
    assert data_home(tmp_path) == home
    assert (home / "config.json").read_text() == "new settings"


def test_enoy_home_override_wins_over_legacy_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ENOY_HOME", str(tmp_path / "new"))
    monkeypatch.setenv(OLD_ENV, str(tmp_path / "old"))
    assert data_home(tmp_path) == tmp_path / "new"
    assert not (tmp_path / "old").exists()


def test_portable_legacy_database_is_imported_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.delenv("ENOY_HOME", raising=False)
    home = tmp_path / "portable"
    home.mkdir()
    monkeypatch.setenv(OLD_ENV, str(home))
    with sqlite3.connect(home / OLD_DATABASE) as db:
        db.execute("CREATE TABLE chats (id INTEGER)")
        db.execute("INSERT INTO chats VALUES (1)")
    assert data_home(tmp_path) == home
    with sqlite3.connect(home / "enoy.db") as db:
        db.execute("INSERT INTO chats VALUES (2)")
    data_home(tmp_path)
    with sqlite3.connect(home / "enoy.db") as db:
        assert db.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 2
