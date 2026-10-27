import json

import pytest

from enoy import config as config_mod
from enoy import paths


def test_defaults_are_valid():
    cfg,warnings=config_mod.validate({})
    assert not warnings and 'ollama' not in cfg and 'memory' not in cfg
    assert cfg['codex']['enabled'] and cfg['claude']['enabled']
    config_mod.validate_strict(cfg)


def test_invalid_values_fall_back_to_defaults_with_warnings():
    cfg,warnings=config_mod.validate({'codex':{'timeout_s':0,'enabled':'yes'},'routing':{'max_attempts':True}})
    assert cfg['codex']['timeout_s']==1800 and cfg['codex']['enabled']
    assert cfg['routing']['max_attempts']==2 and len(warnings)==3


def test_validate_strict_raises():
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate_strict({"routing": {"workflow": "chaos"}})


def test_load_missing_and_corrupt_files(tmp_path):
    cfg, warnings = config_mod.load(tmp_path / "nope.json")
    assert warnings == [] and cfg == config_mod.DEFAULTS
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    cfg, warnings = config_mod.load(bad)
    assert cfg == config_mod.DEFAULTS
    assert "Could not read config" in warnings[0]
    assert bad.read_text(encoding="utf-8") == "{not json"  # Left untouched for recovery.


def test_save_backs_up_previous_file():
    cfg, _ = config_mod.validate({})
    config_mod.save(cfg)
    assert not list(paths.config_backups_dir().glob("*.json"))  # Nothing to back up the first time.
    cfg2 = config_mod.merge_update(cfg, {"codex": {"model": "test-model"}})
    config_mod.save(cfg2)
    backups = list(paths.config_backups_dir().glob("*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["codex"]["model"] == cfg["codex"]["model"]
    loaded, warnings = config_mod.load()
    assert loaded["codex"]["model"] == "test-model" and not warnings


def test_save_rejects_invalid_config():
    cfg, _ = config_mod.validate({})
    cfg["codex"]["timeout_s"] = 0
    with pytest.raises(config_mod.ConfigError):
        config_mod.save(cfg)
    assert not paths.config_path().exists()
