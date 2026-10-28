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


# Build workspace: providers, models and roles (T2.2) ------------------------------------

def build(**over):
    cfg = config_mod.validate({})[0]["build"]
    return {"build": {**cfg, **over}}


LOCAL = {"kind": "openai", "base_url": "http://127.0.0.1:1234/v1"}


def test_build_defaults_have_no_keys_and_only_free_local_providers():
    b = config_mod.validate({})[0]["build"]
    assert set(b["providers"]) == {"ollama"}
    assert not any(p["enabled"] for p in b["providers"].values())
    assert b["providers"]["ollama"]["local"] and b["providers"]["ollama"]["base_url"] == "http://127.0.0.1:11434"
    assert b["roles"] == {"planner": "claude-cli", "implementer": "", "reviewer": "codex-cli"}
    assert b["parallel"] is False
    assert "key" not in json.dumps(b).lower().replace("api_version", "")


def test_old_config_without_build_section_loads_unchanged():
    raw = {"claude": {"model": "my-model"}, "team": {"max_tasks": 4}}
    cfg, warnings = config_mod.validate(raw)
    assert not warnings and cfg["claude"]["model"] == "my-model" and cfg["build"] == config_mod.DEFAULTS["build"]


@pytest.mark.parametrize("field", ["api_key", "key", "secret", "token", "password", "Authorization"])
def test_provider_settings_refuse_secret_fields(field):
    raw = build()
    raw["build"]["providers"]["lmstudio"] = dict(LOCAL, **{field: "sk-123"})
    cfg, warnings = config_mod.validate(raw)
    assert any("Credential Manager" in w for w in warnings)
    assert "lmstudio" not in cfg["build"]["providers"]


def test_local_server_is_accepted_with_defaults_filled_in():
    raw = build()
    raw["build"]["providers"]["custom"] = {"kind": "openai", "base_url": "http://127.0.0.1:8080/v1"}
    cfg, warnings = config_mod.validate(raw)
    assert not warnings and cfg["build"]["providers"]["custom"]["max_concurrency"] == 1
    assert cfg["build"]["providers"]["custom"]["local"] is True


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "http://api.deepseek.com", "https://localhost.example.com",
                                 "http://127.0.0.1@api.example.com/v1", "https://127.0.0.1.example.com/v1"])
def test_providers_off_this_pc_are_removed_because_they_could_cost_money(url):
    raw = build()
    raw["build"]["providers"]["paid"] = {"kind": "openai", "base_url": url, "local": True}  # The flag is not trusted.
    raw["build"]["models"] = raw["build"]["models"] + [{"id": "p", "provider": "paid", "model": "m", "roles": ["implementer"]}]
    raw["build"]["roles"] = {"planner": "claude-cli", "implementer": "p", "reviewer": "codex-cli"}
    cfg, warnings = config_mod.validate(raw)
    assert "paid" not in cfg["build"]["providers"] and "ollama" in cfg["build"]["providers"]
    assert any("does not run on this PC" in w for w in warnings)
    assert all(m["id"] != "p" for m in cfg["build"]["models"]) and cfg["build"]["roles"]["implementer"] == ""
    with pytest.raises(config_mod.ConfigError, match="this PC"):
        config_mod.validate_strict(raw)


def test_old_paid_presets_are_dropped_and_local_settings_kept():
    old = {"build": {"providers": {
        "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com", "enabled": True, "local": False},
        "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "enabled": False, "local": False},
        "ollama": {"kind": "ollama", "base_url": "http://127.0.0.1:11434", "enabled": True, "local": True}}}}
    cfg, warnings = config_mod.validate(old)
    assert set(cfg["build"]["providers"]) == {"ollama"} and cfg["build"]["providers"]["ollama"]["enabled"]
    assert len(warnings) == 2
    cfg, _ = config_mod.validate({"build": {"providers": {"deepseek": old["build"]["providers"]["deepseek"]}}})
    assert set(cfg["build"]["providers"]) == {"ollama"}  # Nothing left: the defaults come back.


def test_roles_pointing_at_unknown_or_unsuitable_models_are_reported():
    raw = build(roles={"planner": "nope", "implementer": "", "reviewer": "codex-cli"})
    cfg, warnings = config_mod.validate(raw)
    assert any("planner" in w and "nope" in w for w in warnings) and cfg["build"]["roles"]["planner"] == ""
    raw = build(models=[{"id": "ds", "provider": "ollama", "model": "qwen3:1.7b", "roles": ["implementer"]}],
                roles={"planner": "ds", "implementer": "ds", "reviewer": ""})
    cfg, warnings = config_mod.validate(raw)
    assert any("planner" in w for w in warnings) and cfg["build"]["roles"] == {"planner": "", "implementer": "ds", "reviewer": ""}


def test_models_must_name_a_known_provider_and_valid_prices():
    raw = build(models=[{"id": "x", "provider": "ghost", "model": "m", "roles": ["implementer"]},
                        {"id": "y", "provider": "ollama", "model": "m", "roles": ["implementer"], "price_in": -1}])
    cfg, warnings = config_mod.validate(raw)
    assert len(warnings) >= 1
    assert all(m["id"] not in ("x", "y") for m in cfg["build"]["models"])
