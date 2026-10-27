"""Runs the chat Markdown renderer's tests (tests/js/markdown.test.js) with Node, if installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize("filename", ["markdown.test.js", "chat-refresh.test.js", "provider-status.test.js", "activity-summary.test.js", "regular-chat.test.js"])
def test_markdown_renderer(filename):
    script = Path(__file__).parent / "js" / filename
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
