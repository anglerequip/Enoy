"""End-to-end orchestration with scripted fake backends (no model or cloud calls)."""

import sys
import time

from enoy.backends.base import CLAUDE, LOCAL, BackendError
from enoy.orchestrator import Orchestrator
from enoy.storage import Storage

ADD_MULTIPLY = """Adding multiply.
<<<EDIT calculator.py
<<<SEARCH
def subtract(a, b):
    return a - b
===
def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b
>>>REPLACE
>>>END
"""

BREAK_SUBTRACT = """<<<EDIT calculator.py
<<<SEARCH
    return a - b
===
    return a + b
>>>REPLACE
>>>END
"""

HARMLESS_COMMENT = """<<<EDIT calculator.py
<<<SEARCH
\"\"\"Small arithmetic helpers.\"\"\"
===
\"\"\"Small arithmetic helpers (checked).\"\"\"
>>>REPLACE
>>>END
"""

FIX_SUBTRACT = """<<<EDIT calculator.py
<<<SEARCH
def subtract(a, b):
    return a + b
===
def subtract(a, b):
    return a - b
>>>REPLACE
>>>END
"""

PYTEST = f'"{sys.executable}" -m pytest -q'


def open_project(orc, project):
    return orc.open_project(str(project))["id"]
