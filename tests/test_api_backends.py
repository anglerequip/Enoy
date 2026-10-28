"""OpenAI-compatible and Anthropic Messages adapters (T2.4, T2.5), against a local fake server."""

import pytest

from enoy.backends.anthropic_api import AnthropicBackend
from enoy.backends.base import BackendError, ChatMessage, ChatRequest, ImageData
from enoy.backends.openai_compat import OpenAICompatBackend
from fakeapi import FakeAPI

SECRET = "sk-SENTINEL-adapter-0123456789"
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                          "required": ["path"]}}}]


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.close()


def openai(api, key=SECRET, **settings):
    return OpenAICompatBackend("deepseek", {"base_url": api.url, "timeout_s": 5, **settings}, "deepseek-flash",
                               key=lambda: key)


def anthropic(api, key=SECRET):
    return AnthropicBackend("anthropic", {"base_url": api.url, "timeout_s": 5, "api_version": "2026-10-21"},
                            "claude-haiku-5-5", key=lambda: key)


# OpenAI-compatible ------------------------------------------------------------------------

def oa_reply(message, usage=None, model="deepseek-flash"):
    return (200, {"model": model, "choices": [{"message": message, "finish_reason": "stop"}],
                  "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}})


def test_openai_chat_sends_auth_model_and_messages(api):
    api.add("/chat/completions", oa_reply({"role": "assistant", "content": "Hi"},
                                          {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 40}))
    r = openai(api).chat(ChatRequest([ChatMessage("system", "Be brief."), ChatMessage("user", "Hello")], num_predict=64))
    req = api.requests[0]
    assert req["headers"]["authorization"] == "Bearer " + SECRET
    assert req["body"]["model"] == "deepseek-flash" and req["body"]["max_tokens"] == 64
    assert req["body"]["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hello"}]
    assert "temperature" not in req["body"]
    assert r.text == "Hi" and r.backend == "deepseek" and r.model == "deepseek-flash"
    assert r.usage == {"input_tokens": 60, "cached_tokens": 40, "output_tokens": 20}


def test_openai_tool_call_round_trip_keeps_provider_message(api):
    assistant = {"role": "assistant", "content": None, "reasoning_content": "Need the file.",
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]}
    api.add("/chat/completions", oa_reply(assistant), oa_reply({"role": "assistant", "content": "Done"}))
    b = openai(api)
    first = b.chat(ChatRequest([ChatMessage("user", "Read a.py")], tools=TOOLS))
    assert first.tool_calls == [{"id": "call_1", "name": "read_file", "arguments": {"path": "a.py"}}]
    assert first.thinking == "Need the file."
    assert api.requests[0]["body"]["tools"] == TOOLS
    history = [ChatMessage("user", "Read a.py"),
               ChatMessage("assistant", "", tool_calls=first.tool_calls, provider_content=first.provider_content),
               ChatMessage("tool", "print(1)", tool_name="read_file", tool_call_id="call_1")]
    b.chat(ChatRequest(history, tools=TOOLS))
    sent = api.requests[1]["body"]["messages"]
    assert sent[1] == assistant  # DeepSeek needs reasoning_content back during tool use.
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"}


def test_openai_bad_tool_arguments_are_reported_not_guessed(api):
    api.add("/chat/completions", oa_reply({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}}]}))
    r = openai(api).chat(ChatRequest([ChatMessage("user", "x")], tools=TOOLS))
    assert r.tool_calls[0]["arguments"] == {} and "not valid JSON" in r.tool_calls[0]["error"]


def test_openai_images_are_data_urls(api):
    api.add("/chat/completions", oa_reply({"role": "assistant", "content": "A cat"}))
    openai(api).chat(ChatRequest([ChatMessage("user", "What?", images=[ImageData("image/png", b"\x89PNG")])]))
    content = api.requests[0]["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "What?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_list_models_and_local_provider_without_key(api):
    api.add("/models", (200, {"data": [{"id": "qwen3:8b"}, {"id": "glm"}]}))
    b = openai(api, key=None, local=True)
    assert b.list_models() == ["qwen3:8b", "glm"]
    assert "authorization" not in api.requests[0]["headers"]
    assert b.status().available


def test_openai_missing_key_fails_before_any_request(api):
    b = openai(api, key=None)
    assert not b.status().available and "key" in b.status().detail.lower()
    with pytest.raises(BackendError) as exc:
        b.chat(ChatRequest([ChatMessage("user", "x")]))
    assert exc.value.kind == "auth" and api.requests == []


def test_openai_empty_choice_is_malformed(api):
    api.add("/chat/completions", (200, {"choices": []}))
    with pytest.raises(BackendError) as exc:
        openai(api).chat(ChatRequest([ChatMessage("user", "x")]))
    assert exc.value.kind == "malformed"


# Anthropic Messages -----------------------------------------------------------------------

def an_reply(content, stop="end_turn", usage=None):
    return (200, {"id": "msg_1", "model": "claude-haiku-5-5", "content": content, "stop_reason": stop,
                  "usage": usage or {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 5}})


def test_anthropic_headers_system_and_usage(api):
    api.add("/v1/messages", an_reply([{"type": "thinking", "thinking": "", "signature": "s"},
                                      {"type": "text", "text": "Hello"}]))
    r = anthropic(api).chat(ChatRequest([ChatMessage("system", "Be brief."), ChatMessage("user", "Hi")], effort="low"))
    req = api.requests[0]
    assert req["headers"]["x-api-key"] == SECRET and req["headers"]["anthropic-version"] == "2026-10-21"
    body = req["body"]
    assert body["system"] == "Be brief." and body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]
    assert body["output_config"] == {"effort": "low"} and body["max_tokens"] == 16000
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(body)
    assert r.text == "Hello" and r.usage == {"input_tokens": 50, "cached_tokens": 5, "output_tokens": 10}


def test_anthropic_tool_use_round_trip(api):
    blocks = [{"type": "thinking", "thinking": "", "signature": "sig"},
              {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
              {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "b.py"}}]
    api.add("/v1/messages", an_reply(blocks, stop="tool_use"), an_reply([{"type": "text", "text": "Done"}]))
    b = anthropic(api)
    first = b.chat(ChatRequest([ChatMessage("user", "Read")], tools=TOOLS))
    assert api.requests[0]["body"]["tools"] == [{"name": "read_file", "description": "Read a file",
                                                 "input_schema": TOOLS[0]["function"]["parameters"]}]
    assert [c["id"] for c in first.tool_calls] == ["toolu_1", "toolu_2"] and first.stop_reason == "tool_use"
    history = [ChatMessage("user", "Read"),
               ChatMessage("assistant", "", tool_calls=first.tool_calls, provider_content=first.provider_content),
               ChatMessage("tool", "A", tool_name="read_file", tool_call_id="toolu_1"),
               ChatMessage("tool", "B", tool_name="read_file", tool_call_id="toolu_2")]
    b.chat(ChatRequest(history, tools=TOOLS))
    sent = api.requests[1]["body"]["messages"]
    assert sent[1] == {"role": "assistant", "content": blocks}  # Thinking blocks go back unchanged.
    assert sent[2] == {"role": "user", "content": [  # All results in one user message.
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "A"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "B"}]}


def test_anthropic_refusal_is_an_error_not_an_answer(api):
    api.add("/v1/messages", an_reply([], stop="refusal"))
    with pytest.raises(BackendError) as exc:
        anthropic(api).chat(ChatRequest([ChatMessage("user", "x")]))
    assert exc.value.kind == "failed" and "declined" in str(exc.value)


def test_anthropic_list_models_follows_pages(api):
    api.add("/v1/models", (200, {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}),
            (200, {"data": [{"id": "b"}], "has_more": False, "last_id": "b"}))
    assert anthropic(api).list_models() == ["a", "b"]
    assert "after_id=a" in api.requests[1]["path"]
