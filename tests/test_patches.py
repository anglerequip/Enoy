import pytest

from fusion_ai import patches
from fusion_ai.project import Workspace, sha256_bytes

EDIT_OUTPUT = """I added a multiply function.

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

<<<RUN python -m pytest -q>>>
"""


def test_parse_blocks():
    text = EDIT_OUTPUT + "\n<<<FILE notes/todo.md\n```markdown\n# Todo\n```\n>>>END\n<<<DELETE old.txt>>>\n"
    result = patches.parse(text)
    assert [c.action for c in result.changes] == ["edit", "write", "delete"]
    assert result.changes[1].content == "# Todo\n"  # Code fence stripped.
    assert result.commands == ["python -m pytest -q"]
    assert result.explanation == "I added a multiply function."
    assert result.errors == []


def test_parse_reports_truncated_blocks():
    result = patches.parse("<<<FILE a.py\nprint(1)\n")
    assert result.changes == [] and "missing its >>>END" in result.errors[0]


def test_validate_edit_and_diff(project):
    ws = Workspace(project)
    result = patches.validate(ws, patches.parse(EDIT_OUTPUT).changes)
    assert result.ok, result.errors
    f = result.files[0]
    assert f.action == "modify" and f.path == "calculator.py"
    assert "+def multiply(a, b):" in f.diff and f.diff.startswith("--- a/calculator.py")


@pytest.mark.parametrize("output,message", [
    ("<<<EDIT calculator.py\n<<<SEARCH\nnot in file\n===\nx\n>>>REPLACE\n>>>END", "not found"),
    ("<<<FILE ../evil.py\nx = 1\n>>>END", "leaves the project"),
    ("<<<FILE .env\nA=1\n>>>END", "credentials"),
    ("<<<FILE broken.py\ndef f(:\n>>>END", "syntax error"),
    ("<<<FILE data.json\n{bad json}\n>>>END", "invalid JSON"),
    ("<<<FILE config.py\nAPI_KEY = '[REDACTED]'\n>>>END", "placeholders"),
    ("<<<DELETE missing.py>>>", "does not exist"),
    ("<<<EDIT new.py\n<<<SEARCH\na\n===\nb\n>>>REPLACE\n>>>END", "does not exist"),
    ("Just some prose with no blocks.", "no file changes"),
])
def test_validation_rejects_bad_proposals(project, output, message):
    ws = Workspace(project)
    parsed = patches.parse(output)
    result = patches.validate(ws, parsed.changes)
    assert not result.ok
    assert any(message in e for e in result.errors), result.errors


def test_ambiguous_search_is_rejected(project):
    (project / "dup.py").write_bytes(b"x = 1\nx = 1\n")
    ws = Workspace(project)
    out = "<<<EDIT dup.py\n<<<SEARCH\nx = 1\n===\nx = 2\n>>>REPLACE\n>>>END"
    result = patches.validate(ws, patches.parse(out).changes)
    assert any("appears 2 times" in e for e in result.errors)


def test_search_tolerates_trailing_whitespace(project):
    (project / "ws.py").write_bytes(b"a = 1   \nb = 2\n")
    ws = Workspace(project)
    out = "<<<EDIT ws.py\n<<<SEARCH\na = 1\nb = 2\n===\na = 10\nb = 20\n>>>REPLACE\n>>>END"
    result = patches.validate(ws, patches.parse(out).changes)
    assert result.ok and result.files[0].new_content == "a = 10\nb = 20\n"


def test_stale_file_detected(project):
    ws = Workspace(project)
    result = patches.validate(ws, patches.parse(EDIT_OUTPUT).changes, seen_hashes={"calculator.py": "0" * 64})
    assert any("changed after the model read it" in e for e in result.errors)


def test_luau_bracket_check():
    assert patches.luau_bracket_check("local t = { a = (1 + 2) }\n-- ) comment\nlocal s = \"(\"\n") is None
    assert "unclosed" in patches.luau_bracket_check("local t = {\n")
    assert patches.luau_bracket_check("local s = [[ ( ]]\n--[[ { ]]\n") is None


def test_apply_writes_backup_and_revert_restores(project, tmp_path):
    ws = Workspace(project)
    original = (project / "calculator.py").read_bytes()
    result = patches.validate(ws, patches.parse(EDIT_OUTPUT).changes)
    files = [f.as_dict() for f in result.files]
    backups = patches.apply_files(ws, files, tmp_path / "bk")
    assert "def multiply" in (project / "calculator.py").read_text(encoding="utf-8")
    assert len(backups) == 1
    from pathlib import Path
    assert Path(backups[0]).read_bytes() == original
    patches.revert_files(ws, files)
    assert (project / "calculator.py").read_bytes() == original


def test_apply_refuses_when_file_changed_concurrently(project, tmp_path):
    ws = Workspace(project)
    files = [f.as_dict() for f in patches.validate(ws, patches.parse(EDIT_OUTPUT).changes).files]
    (project / "calculator.py").write_text("# edited by the user meanwhile\n", encoding="utf-8")
    with pytest.raises(patches.ConflictError):
        patches.apply_files(ws, files, tmp_path / "bk")
    assert (project / "calculator.py").read_text(encoding="utf-8") == "# edited by the user meanwhile\n"


def test_apply_is_all_or_nothing(project, tmp_path):
    ws = Workspace(project)
    out = EDIT_OUTPUT + "\n<<<FILE README.md\nnew readme\n>>>END\n"
    files = [f.as_dict() for f in patches.validate(ws, patches.parse(out).changes).files]
    (project / "README.md").write_text("changed meanwhile\n", encoding="utf-8")
    with pytest.raises(patches.ConflictError):
        patches.apply_files(ws, files, tmp_path / "bk")
    assert "multiply" not in (project / "calculator.py").read_text(encoding="utf-8")


def test_revert_refuses_after_later_user_edit(project, tmp_path):
    ws = Workspace(project)
    files = [f.as_dict() for f in patches.validate(ws, patches.parse(EDIT_OUTPUT).changes).files]
    patches.apply_files(ws, files, tmp_path / "bk")
    (project / "calculator.py").write_text("# user kept editing\n", encoding="utf-8")
    with pytest.raises(patches.ConflictError):
        patches.revert_files(ws, files)


def test_create_and_delete(project, tmp_path):
    ws = Workspace(project)
    out = "<<<FILE src/util.py\nVALUE = 1\n>>>END\n<<<DELETE README.md>>>"
    result = patches.validate(ws, patches.parse(out).changes)
    assert [f.action for f in result.files] == ["create", "delete"]
    files = [f.as_dict() for f in result.files]
    patches.apply_files(ws, files, tmp_path / "bk")
    assert (project / "src" / "util.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (project / "README.md").exists()
    patches.revert_files(ws, files)
    assert not (project / "src" / "util.py").exists()
    assert (project / "README.md").exists()


def test_crlf_files_keep_crlf(project, tmp_path):
    (project / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    ws = Workspace(project)
    out = "<<<EDIT win.py\n<<<SEARCH\nb = 2\n===\nb = 3\n>>>REPLACE\n>>>END"
    files = [f.as_dict() for f in patches.validate(ws, patches.parse(out).changes).files]
    patches.apply_files(ws, files, tmp_path / "bk")
    data = (project / "win.py").read_bytes()
    assert data == b"a = 1\r\nb = 3\r\n"
    assert sha256_bytes(data) == files[0]["new_hash"]


def test_edit_pair_without_separator_is_accepted():
    """Qwen's common layout: SEARCH lines, >>>REPLACE, new lines, >>>END (no === line)."""
    out = ("Adding multiply.\n<<<EDIT calc.py\n<<<SEARCH\ndef a():\n    pass\n>>>REPLACE\ndef a():\n    pass\n\n\n"
           "def b():\n    pass\n<<<SEARCH\nx = 1\n>>>REPLACE\nx = 2\n>>>END\n")
    r = patches.parse(out)
    assert not r.errors
    (change,) = r.changes
    assert [(e.search, e.replace) for e in change.edits] == [
        ("def a():\n    pass", "def a():\n    pass\n\n\ndef b():\n    pass"), ("x = 1", "x = 2")]


def test_qwen_layout_slips_are_read_correctly():
    """===REPLACE as separator, and a block left open before the next block starts."""
    out = ("Both.\n<<<EDIT a.py\n<<<SEARCH\nx = 1\n===REPLACE\nx = 2\n>>>END\n"
           "<<<EDIT b.py\n<<<SEARCH\ny = 1\n===\ny = 2\n>>>REPLACE\n"
           "<<<EDIT c.py\n<<<SEARCH\nz = 1\n===\nz = 2\n>>>REPLACE\n>>>END\n")
    r = patches.parse(out)
    assert not r.errors
    assert [(c.path, [(e.search, e.replace) for e in c.edits]) for c in r.changes] == [
        ("a.py", [("x = 1", "x = 2")]), ("b.py", [("y = 1", "y = 2")]), ("c.py", [("z = 1", "z = 2")])]


def test_cut_off_edit_is_still_an_error():
    r = patches.parse("<<<EDIT a.py\n<<<SEARCH\nx = 1\n===\nx = 2\n")
    assert r.errors and not r.changes
    r = patches.parse("<<<EDIT a.py\n<<<SEARCH\nx = 1\n")
    assert r.errors and not r.changes
    r = patches.parse("<<<EDIT a.py\n<<<SEARCH\nTitle\n=====\n===\nNew\n>>>REPLACE\n>>>END\n")
    assert r.changes[0].edits[0].search == "Title\n====="  # A long ===== line is content, not a separator.


def test_several_edit_blocks_for_one_file_apply_in_order(project):
    ws = Workspace(project)
    out = ("<<<EDIT calculator.py\n<<<SEARCH\ndef add(a, b):\n===\nimport math\n\n\ndef add(a, b):\n>>>END\n"
           "<<<EDIT ./calculator.py\n<<<SEARCH\ndef subtract(a, b):\n===\ndef subtract2(a, b):\n>>>END\n")
    parsed = patches.parse(out)
    assert len(parsed.changes) == 2
    result = patches.validate(ws, parsed.changes)
    assert result.errors == [] and len(result.files) == 1
    new = result.files[0].new_content
    assert "import math\n\n\ndef add(a, b):" in new and "def subtract2(a, b):" in new
    write_twice = [patches.ProposedChange("write", "x.py", "a = 1\n"), patches.ProposedChange("write", "x.py", "a = 2\n")]
    assert any("more than once" in e for e in patches.validate(ws, write_twice).errors)
