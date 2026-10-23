import json

import pytest

from fusion_ai import config as config_mod
from fusion_ai import paths


def test_defaults_are_valid():
    cfg, warnings = config_mod.validate({})
    assert warnings == []
    assert cfg["ollama"]["model"] == "qwen3.5:9b"
    assert cfg["routing"]["mode"] == "auto"  # Smart router by default.
    assert cfg["routing"]["target_local_share"] == 70 and cfg["routing"]["aggressiveness"] == "balanced"
    assert cfg["privacy"]["cloud_policy"] == "auto"
    config_mod.validate_strict(cfg)


def test_invalid_values_fall_back_to_defaults_with_warnings():
    cfg, warnings = config_mod.validate({
        "ollama": {"num_ctx": 100, "endpoint": "ftp://x", "think": "yes", "model": "llama3:8b"},
        "routing": {"mode": "random", "max_local_attempts": True},
        "bogus": {},
    })
    assert cfg["ollama"]["num_ctx"] == config_mod.DEFAULTS["ollama"]["num_ctx"]
    assert cfg["ollama"]["endpoint"] == config_mod.DEFAULTS["ollama"]["endpoint"]
    assert cfg["ollama"]["think"] is False
    assert cfg["ollama"]["model"] == "llama3:8b"  # Valid values are kept.
    assert cfg["routing"]["mode"] == "auto"  # Smart router by default.
    assert cfg["routing"]["target_local_share"] == 70 and cfg["routing"]["aggressiveness"] == "balanced"
    assert cfg["privacy"]["cloud_policy"] == "auto"
    assert cfg["routing"]["max_local_attempts"] == 2  # bool is not accepted as int
    assert len(warnings) == 6


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
    cfg2 = config_mod.merge_update(cfg, {"routing": {"mode": "manual"}})
    config_mod.save(cfg2)
    backups = list(paths.config_backups_dir().glob("*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["routing"]["mode"] == "auto"
    loaded, warnings = config_mod.load()
    assert loaded["routing"]["mode"] == "manual" and not warnings


def test_save_rejects_invalid_config():
    cfg, _ = config_mod.validate({})
    cfg["ollama"]["num_ctx"] = 1
    with pytest.raises(config_mod.ConfigError):
        config_mod.save(cfg)
    assert not paths.config_path().exists()
