import json

from enoy.context_usage import context_reading
from enoy.backends.base import CLAUDE, CODEX, ChatResult
from enoy import paths


def test_codex_context_uses_provider_input_tokens_and_known_window():
    reading = context_reading(CODEX, "gpt-6.1-sol", {
        "input_tokens": 105_000, "cached_input_tokens": 20_000, "output_tokens": 500,
    })
    assert reading == {
        "used_tokens": 105_000, "window_tokens": 1_050_000,
        "used_percent": 10.0, "basis": "provider input usage for the latest Enoy request",
    }


def test_claude_context_includes_cached_and_new_input_tokens():
    reading = context_reading(CLAUDE, "claude-opus-5-5", {
        "input_tokens": 1_000, "cache_read_input_tokens": 3_000,
        "cache_creation_input_tokens": 2_000, "output_tokens": 100,
    })
    assert reading["used_tokens"] == 6_000
    assert reading["window_tokens"] == 1_000_000
    assert reading["used_percent"] == 0.6


def test_explicit_provider_context_size_and_percent_take_precedence():
    reading = context_reading(CODEX, "custom-model", {
        "contextWindow": {"usedTokens": 500, "contextWindowSize": 2_000, "usedPercentage": 25},
        "input_tokens": 900,
    })
    assert reading["used_tokens"] == 500
    assert reading["window_tokens"] == 2_000
    assert reading["used_percent"] == 25


def test_unknown_model_does_not_get_a_guessed_capacity():
    assert context_reading(CODEX, "private-custom-model", {"input_tokens": 12}) is None
    assert context_reading(CLAUDE, "private-custom-model", {"input_tokens": 12}) is None


def test_zero_input_is_a_valid_reading():
    reading = context_reading(CLAUDE, "opus", {"input_tokens": 0})
    assert reading and reading["used_percent"] == 0


def test_team_and_model_transmission_usage_can_be_serialized():
    usage = context_reading(CODEX, "gpt-6-luna", {"input_tokens": 2_100})
    result = ChatResult("Done", CODEX, "gpt-6-luna", usage={"input_tokens": 2_100})
    stored = json.loads(json.dumps({**result.usage, "model": result.model, "context": usage}))
    assert stored["context"]["used_tokens"] == 2_100


def test_real_coordinator_persists_context_meter_from_backend_report(orc_factory):
    orc = orc_factory(qwen=[ChatResult(
        "MODE: answer\nHello.", CODEX, "gpt-6-luna", usage={"input_tokens": 2_100})])
    project = orc.open_project(str(paths.general_chat_dir()))
    task = orc.start_task(project["id"], "Hello")
    transmissions = orc.db.transmissions(task["id"])
    usage = json.loads(transmissions[-1]["usage"])
    assert usage["context"] == {
        "used_tokens": 2_100, "window_tokens": 1_050_000,
        "used_percent": 0.2, "basis": "provider input usage for the latest Enoy request",
    }
