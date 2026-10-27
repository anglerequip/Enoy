"""Images attached to messages: storage, routing to Qwen/Claude, privacy, and the API."""

import base64
import http.client
import json
import struct
import sys
import zlib

import pytest

from conftest import FakeBackend
from enoy import config as config_mod
from enoy import images
from enoy.backends.base import CLAUDE, LOCAL, BackendError, ChatMessage, ChatRequest, ImageData
from enoy.backends.claude_code import ClaudeCodeBackend, _parse_json_output
from enoy.orchestrator import EventBus, Orchestrator
from enoy.server import App, Server
from enoy.storage import Storage


def png(color=(255, 0, 0)) -> bytes:
    """A real 1x1 PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"\x00" + bytes(color)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- storage

def test_store_accepts_png_and_jpeg_by_content():
    a = images.store(png())
    assert a["media_type"] == "image/png" and len(a["id"]) == 64
    assert images.store(png())["id"] == a["id"]  # Same picture, same id, stored once.
    j = images.store(b"\xff\xd8\xff\xe0" + b"0" * 100)
    assert j["media_type"] == "image/jpeg"
    loaded = images.load(a["id"])
    assert loaded.media_type == "image/png" and loaded.data == png()


@pytest.mark.parametrize("data", [b"GIF89a....", b"<svg></svg>", b"MZ\x90\x00 an exe", b""])
def test_store_rejects_other_formats(data):
    with pytest.raises(images.ImageError):
        images.store(data)


def test_store_rejects_oversized():
    with pytest.raises(images.ImageError, match="too large"):
        images.store(b"\x89PNG\r\n\x1a\n" + b"0" * images.MAX_BYTES)


def test_decode_upload_accepts_data_url_and_rejects_garbage():
    b64 = base64.b64encode(png()).decode()
    assert images.decode_upload("data:image/png;base64," + b64) == png()
    assert images.decode_upload(b64) == png()
    with pytest.raises(images.ImageError):
        images.decode_upload("not base64 !!")


def test_ids_are_checked_against_path_tricks():
    good = images.store(png())["id"]
    assert images.valid_ids([good, good]) == [good]
    for bad in ["../../secrets", "a" * 64, good[:-1] + "\\", 12]:
        with pytest.raises(images.ImageError):
            images.valid_ids([bad])
    with pytest.raises(images.ImageError):
        images.load("..\\..\\enoy.db")
    many = [images.store(png((i, 0, 0)))["id"] for i in range(images.MAX_PER_MESSAGE + 1)]
    with pytest.raises(images.ImageError, match="At most"):
        images.valid_ids(many)


# ---------------------------------------------------------------- orchestrator

class VisionBackend(FakeBackend):
    def __init__(self, name, replies=None, vision=True):
        super().__init__(name, replies)
        self.vision = vision

    def capabilities(self, model):
        return ["completion", "vision"] if self.vision else ["completion"]


def make(cfg, tmp_path, qwen=None, claude=None, vision=True):
    backends = {LOCAL: VisionBackend(LOCAL, qwen, vision), CLAUDE: FakeBackend(CLAUDE, claude)}
    return Orchestrator(Storage(":memory:"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=True)


def general_project(orc):
    from enoy import paths
    return orc.open_project(str(paths.general_chat_dir()))["id"]
















# ---------------------------------------------------------------- backends



STREAM_CLI = r'''
import json, os, sys
args = sys.argv[1:]
data = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "w", encoding="utf-8") as fh:
    json.dump({"args": args, "stdin": data}, fh)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "A red square."}]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "A red square.",
                  "session_id": "s-1", "usage": {"input_tokens": 1500, "output_tokens": 5}}))
'''


def test_claude_images_use_stream_json_input(tmp_path, monkeypatch):
    script = tmp_path / "cli.py"
    script.write_text(STREAM_CLI, encoding="utf-8")
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    backend = ClaudeCodeBackend(dict(config_mod.DEFAULTS["claude"]), command_prefix=[sys.executable, str(script)])
    result = backend.chat(ChatRequest(messages=[ChatMessage("system", "SYS"),
                                                ChatMessage("user", "What is this?", [ImageData("image/png", png())])]))
    assert result.text == "A red square." and result.session_id == "s-1"
    call = json.loads(log.read_text(encoding="utf-8"))
    args = call["args"]
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json" and "--verbose" in args
    assert args[args.index("--tools") + 1] == "" and "--safe-mode" in args
    msg = json.loads(call["stdin"])
    assert msg["type"] == "user"
    blocks = msg["message"]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[0]["source"]["data"]) == png()
    assert blocks[-1] == {"type": "text", "text": "What is this?"}


def test_claude_without_images_keeps_plain_text_input(tmp_path):
    backend = ClaudeCodeBackend(dict(config_mod.DEFAULTS["claude"]))
    args = backend.build_args("", None)
    assert "--input-format" not in args and args[:3] == ["-p", "--output-format", "json"]


def test_stream_json_parser_finds_the_result_line():
    out = "\n".join([json.dumps({"type": "system"}), json.dumps({"type": "result", "result": "ok", "subtype": "success"}),
                     json.dumps({"type": "something_after"})])
    assert _parse_json_output(out)["result"] == "ok"


# ---------------------------------------------------------------- API

@pytest.fixture
def server(cfg, tmp_path):
    backends = {LOCAL: VisionBackend(LOCAL, ["A red square."]), CLAUDE: FakeBackend(CLAUDE, available=False)}
    orc = Orchestrator(Storage(":memory:"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=True)
    srv = Server(App(orc, [], save_config=False)).start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{srv.port}"}
    if token:
        headers["X-Enoy-Token"] = srv.app.token
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, (json.loads(data) if "json" in ctype else data), ctype


def test_upload_view_and_send_image_through_api(server):
    data = "data:image/png;base64," + base64.b64encode(png()).decode()
    status, body, _ = call(server, "POST", "/api/images", {"data": data})
    assert status == 200
    img = body["image"]["id"]
    status, raw, ctype = call(server, "GET", f"/api/images/{img}")
    assert status == 200 and ctype == "image/png" and raw == png()
    assert call(server, "GET", f"/api/images/{img}", token=False)[0] == 401
    assert call(server, "GET", f"/api/images/{img}?token={server.app.token}", token=False)[0] == 200
    assert call(server, "GET", "/api/images/..%2F..%2Fenoy.db")[0] == 404
    status, body, _ = call(server, "POST", "/api/images", {"data": base64.b64encode(b"GIF89a....").decode()})
    assert status == 400 and "PNG and JPEG" in body["error"]
    pid = call(server, "POST", "/api/projects/default")[1]["project"]["id"]
    status, body, _ = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "What is this?", "images": [img]})
    assert status == 200
    status, detail, _ = call(server, "GET", f"/api/tasks/{body['task']['id']}")
    assert detail["messages"][0]["images"] == [img] and detail["task"]["status"] == "failed"
