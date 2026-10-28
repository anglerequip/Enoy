"""Provider keys in Windows Credential Manager (the Windows API is replaced by a dict here)."""

import pytest

from enoy.tools import keystore

SECRET = "sk-SENTINEL-keystore-0123456789"


@pytest.fixture
def vault(monkeypatch):
    store = {}
    monkeypatch.setattr(keystore, "_read", lambda target: store.get(target))
    monkeypatch.setattr(keystore, "_write", lambda target, blob: store.__setitem__(target, blob.decode()))
    monkeypatch.setattr(keystore, "_remove", lambda target: store.pop(target, None))
    return store


def test_provider_key_names_are_accepted(vault):
    keystore.put("provider:deepseek", SECRET)
    assert keystore.get("provider:deepseek") == SECRET
    assert vault == {"Enoy/provider:deepseek": SECRET}
    keystore.delete("provider:deepseek")
    assert keystore.get("provider:deepseek") is None


def test_existing_brave_key_still_works(vault):
    keystore.put("brave_search", "brave-key")
    assert keystore.status() == {"brave_search": True}


@pytest.mark.parametrize("name", ["provider:", "provider:Deep Seek", "provider:../x", "provider:" + "a" * 41,
                                  "deepseek", "Enoy/provider:x", "provider:x\n"])
def test_invalid_names_are_refused(vault, name):
    with pytest.raises(ValueError):
        keystore.put(name, SECRET)
    assert keystore.get(name) is None
    assert vault == {}


def test_key_value_never_appears_in_status_or_errors(vault):
    keystore.put("provider:fireworks", SECRET)
    assert SECRET not in repr(keystore.status()) and SECRET not in repr(keystore.provider_status(["fireworks", "x"]))
    assert keystore.provider_status(["fireworks", "x"]) == {"fireworks": True, "x": False}
    with pytest.raises(ValueError) as exc:
        keystore.put("provider:Bad Name", SECRET)
    assert SECRET not in str(exc.value)
    with pytest.raises(ValueError) as exc:
        keystore.put("provider:fireworks", SECRET * 100)
    assert SECRET not in str(exc.value)
