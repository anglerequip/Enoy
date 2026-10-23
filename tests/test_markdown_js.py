"""Runs the chat Markdown renderer's tests (tests/js/markdown.test.js) with Node, if installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_markdown_renderer():
    script = Path(__file__).parent / "js" / "markdown.test.js"
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
