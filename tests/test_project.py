import os
import sys

import pytest

from enoy.project import PathError, Workspace


def test_tree_excludes_dependencies_and_flags_secrets(project):
    ws = Workspace(project)
    tree = ws.tree()
    paths = []

    def walk(node):
        for child in node.get("children", []):
            if child["type"] == "file":
                paths.append((child["path"], child["sensitive"]))
            else:
                walk(child)

    walk(tree["tree"])
    names = [p for p, _ in paths]
    assert "calculator.py" in names
    assert "src/shared/Inventory.luau" in names
    assert not any(p.startswith("node_modules/") for p in names)
    assert (".env", True) in paths


def test_read_refuses_sensitive_files(project):
    ws = Workspace(project)
    with pytest.raises(PathError):
        ws.read_text(".env")
    text, sha = ws.read_text("calculator.py")
    assert "def divide" in text and len(sha) == 64


def test_index_excludes_sensitive_files(project):
    ws = Workspace(project)
    assert ".env" not in ws.index
    assert all(not p.startswith("node_modules") for p in ws.index)


@pytest.mark.parametrize("bad", [
    "../outside.txt", "src/../../outside.txt", "C:/Windows/win.ini", "C:\\Windows\\win.ini",
    "/etc/passwd", "\\\\server\\share\\x", "calculator.py:stream", "CON", "src/nul.txt", "",
])
def test_path_traversal_is_blocked(project, bad):
    ws = Workspace(project)
    with pytest.raises(PathError):
        ws.resolve(bad)


def test_valid_relative_paths(project):
    ws = Workspace(project)
    assert ws.resolve("src/shared/Inventory.luau").is_file()
    assert ws.resolve("src\\shared\\Inventory.luau").is_file()
    assert ws.resolve("new/dir/file.py") == project.resolve() / "new" / "dir" / "file.py"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_escape_is_blocked(project, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(project / "link"))
    ws = Workspace(project)
    with pytest.raises(PathError):
        ws.resolve("link/secret.txt")
    assert not any(p.startswith("link/") for p in ws.index)


def test_relevant_file_retrieval(project):
    ws = Workspace(project)
    ranked = [p for p, _ in ws.relevant_files("Add a removeItem function to the Inventory module")]
    assert ranked[0] == "src/shared/Inventory.luau"
    ranked = [p for p, _ in ws.relevant_files("divide should return None instead of raising")]
    assert ranked[0] == "calculator.py"
    ranked = [p for p, _ in ws.relevant_files("look at tests/test_calculator.py")]
    assert ranked[0] == "tests/test_calculator.py"


def test_search_names_and_content(project):
    ws = Workspace(project)
    results = ws.search("ZeroDivisionError")
    assert {r["path"] for r in results} >= {"calculator.py", "tests/test_calculator.py"}
    assert not any(r["path"] == ".env" for r in ws.search("API_KEY"))


def test_detect_facts(project):
    facts = Workspace(project).detect()
    assert facts["roblox"] is True
    assert "python -m pytest -q" in facts["test_commands"]


def test_instruction_files(project):
    (project / "ENOY.md").write_text("Always use tabs in Luau.", encoding="utf-8")
    ins = Workspace(project).instructions()
    assert ins[0] == ("ENOY.md", "Always use tabs in Luau.")


def test_missing_project_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        Workspace(tmp_path / "missing")
