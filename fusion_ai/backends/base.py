"""Backend interface shared by the local (Ollama) and Claude adapters."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

LOCAL = "qwen"
CLAUDE = "claude"


class BackendError(RuntimeError):
    """A backend call failed. `kind` lets the orchestrator decide what to do.

    kinds: unavailable (service down / not installed / not signed in),
    model_missing, timeout, cancelled, malformed, limit (usage limit reached),
    failed (anything else).
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class ImageData:
    media_type: str   # image/png | image/jpeg
    data: bytes

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass
class ChatMessage:
    role: str      # system | user | assistant
    content: str
    images: list[ImageData] = field(default_factory=list)   # Only on user messages.


@dataclass
class ChatRequest:
    messages: list[ChatMessage]
    purpose: str = "chat"              # chat | edit | review | plan | summary | classify
    json_schema: dict | None = None    # Ask for structured output when supported.
    cancel: threading.Event = field(default_factory=threading.Event)
    on_token: Callable[[str], None] | None = None
    on_thinking: Callable[[str], None] | None = None
    resume_session: str | None = None  # Claude Code session to continue, if any.
    num_ctx: int | None = None         # Local model context size for this request (memory mode may lower it).
    keep_alive: str | int | None = None  # Local model: how long it stays loaded after this request.


@dataclass
class ChatResult:
    text: str
    backend: str
    model: str
    thinking: str = ""
    usage: dict = field(default_factory=dict)   # Only values the backend reported.
    session_id: str | None = None
    duration_s: float = 0.0


@dataclass
class BackendStatus:
    backend: str
    available: bool
    detail: str
    model: str = ""
    version: str = ""
    verified: bool = False   # A real request has succeeded.
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Backend(Protocol):
    name: str

    def status(self) -> BackendStatus: ...

    def chat(self, request: ChatRequest) -> ChatResult: ...
