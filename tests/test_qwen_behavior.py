"""How Qwen is briefed and how it gets files: act-first instructions, file lookups, context-window fit."""

from fusion_ai import context as ctx
from fusion_ai.backends.base import CLAUDE, LOCAL
from fusion_ai.orchestrator import MAX_READ_ROUNDS
from fusion_ai.project import Workspace
from fusion_ai.routing import classify

ADD_MULTIPLY = """Added multiply.
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


def open_project(orc, path):
    return orc.open_project(str(path))["id"]


def system_of(request):
    return request.messages[0].content


def user_of(request):
    return request.messages[-1].content


# Classification -----------------------------------------------------------------

def test_change_requests_in_everyday_words_are_edits():
    for text in ("rebuild the header of this application so the window header is transparent",
                 "redesign the settings page", "tweak the colours of the sidebar", "hide the title bar",
                 "center the login box", "gör knappen blå", "ta bort den gamla loggan", "bygg om menyn"):
        assert classify(text).kind == "edit", text


def test_questions_about_changes_stay_questions():
    for text in ("how can I improve the header?", "why is the title bar white?", "explain how the header is drawn"):
        assert classify(text).kind == "ask", text


# Prompt ------------------------------------------------------------------------

def test_instructions_tell_qwen_to_act_not_ask():
    for text in (ctx.BASE_SYSTEM, ctx.GENERAL_SYSTEM):
        assert "pick the most likely" in " ".join(text.split())
    assert "Never ask the user to paste files" in " ".join(ctx.BASE_SYSTEM.split())
    assert "which file you need" not in ctx.BASE_SYSTEM


def test_system_prompt_lists_files_and_offers_lookups(project):
    ws = Workspace(project)
    pack = ctx.ContextPack()
    system = ctx.local_system_prompt(ws, pack, "edit", list_chars=2000, reads_left=1)
    assert "Files in the project:" in system and "calculator.py" in system and "src/shared/: Inventory.luau" in system
    assert ".env" not in system  # Secrets files are never listed.
    assert "<<<READ path/to/file.ext>>>" in system
    last = ctx.local_system_prompt(ws, pack, "edit", list_chars=2000, reads_left=0)
    assert "<<<READ" not in last and ctx.NO_MORE_READS in last
    assert "Files in the project" not in ctx.local_system_prompt(ws, pack, "edit")  # Team and Claude prompts unchanged.


def test_file_list_is_cut_to_its_budget(tmp_path):
    for i in range(60):
        (tmp_path / f"module_{i:02}.py").write_text("x = 1\n", encoding="utf-8")
    listing = ctx.file_list(Workspace(tmp_path), 200)
    assert len(listing) < 260 and "module_00.py" in listing
    assert listing.endswith("more files")


def test_empty_folder_is_stated_plainly(tmp_path):
    empty = tmp_path / "annat enoy"
    empty.mkdir()
    system = ctx.local_system_prompt(Workspace(empty), ctx.ContextPack(), "edit", list_chars=2000, reads_left=2)
    assert "is empty" in system and "Open folder" in system and "<<<READ" not in system


def test_read_requests_are_parsed_and_stripped():
    text = "I need these.\n<<<READ ui/app.js>>>\n  <<<READ `ui/styles.css`>>>\n<<<READ ui/app.js>>>\n"
    assert ctx.read_requests(text) == ["ui/app.js", "ui/styles.css"]
    assert ctx.strip_reads(text) == "I need these."


# Context window ------------------------------------------------------------------

def test_prompt_budget_leaves_room_for_the_answer():
    assert ctx.prompt_budget(4096, 2048) == (4096 - 1365) * ctx.CHARS_PER_TOKEN
    assert ctx.prompt_budget(16384, None) == (16384 - 5461) * ctx.CHARS_PER_TOKEN
    assert ctx.prompt_budget(4096, 600) == (4096 - 600) * ctx.CHARS_PER_TOKEN


def test_fit_files_cuts_then_skips():
    files = [ctx.FileContext(n, "a" * 3000, "sha", False) for n in ("one.py", "two.py", "three.py")]
    pack = ctx.ContextPack(files=files)
    fitted = ctx.fit_files(pack, 5000)
    assert [f.path for f in fitted.files] == ["one.py", "two.py"]
    assert not fitted.files[0].truncated and fitted.files[1].truncated
    assert "three.py: does not fit the local model's context window" in fitted.skipped
    assert len(pack.files) == 3  # The original pack (also used for Claude) is untouched.


def test_chill_mode_prompt_fits_the_window(orc_factory, project):
    (project / "big.py").write_text("# filler\n" * 4000, encoding="utf-8")
    orc = orc_factory(qwen=["It is filler."])
    orc.backends[LOCAL].settings.update(num_ctx=4096, num_predict=2048)
    task = orc.start_task(open_project(orc, project), "Explain big.py")
    sent = sum(len(m.content) for m in orc.backends[LOCAL].requests[0].messages)
    assert sent <= ctx.prompt_budget(4096, 2048)
    assert orc.db.get_task(task["id"])["status"] == "completed"


# File lookups --------------------------------------------------------------------

def test_qwen_reads_a_file_then_answers(orc_factory, project):
    orc = orc_factory(qwen=["<<<READ tests/test_calculator.py>>>", "The tests cover add and divide."])
    task = orc.start_task(open_project(orc, project), "What do the tests check?")
    reqs = orc.backends[LOCAL].requests
    assert len(reqs) == 2
    assert "=== tests/test_calculator.py ===" in user_of(reqs[1])
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed" and t["local_attempts"] == 1
    msgs = [m for m in orc.db.messages(task["id"]) if m["role"] == "assistant"]
    assert len(msgs) == 1 and "<<<READ" not in msgs[0]["content"]
    assert any(a["kind"] == "context.files_read" for a in orc.db.activity(task["id"]))
    assert orc.backends[CLAUDE].requests == []


def test_lookups_are_bounded(orc_factory, project):
    orc = orc_factory(qwen=["<<<READ README.md>>>"] * (MAX_READ_ROUNDS + 1) + ["Done."])
    task = orc.start_task(open_project(orc, project), "What is this project?")
    reqs = orc.backends[LOCAL].requests
    assert len(reqs) == MAX_READ_ROUNDS + 2  # The reads, one more read that is refused, then the answer.
    assert "<<<READ path" not in system_of(reqs[-1]) and ctx.NO_MORE_READS in system_of(reqs[-1])
    assert "Give your answer now" in user_of(reqs[-1])
    answers = [m["content"] for m in orc.db.messages(task["id"]) if m["role"] == "assistant"]
    assert answers == ["Done."] and orc.db.get_task(task["id"])["status"] == "completed"


def test_endless_lookups_fail_clearly(orc_factory, project):
    orc = orc_factory(qwen=["<<<READ README.md>>>"] * (MAX_READ_ROUNDS + 2))
    orc.backends[CLAUDE].available = False
    task = orc.start_task(open_project(orc, project), "What is this project?")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "failed" and "kept asking for more files" in t["summary"]
    assert not [m for m in orc.db.messages(task["id"]) if m["role"] == "assistant"]


def test_secret_files_are_refused_to_qwen(orc_factory, project):
    orc = orc_factory(qwen=["<<<READ .env>>>", "Ok."])
    orc.start_task(open_project(orc, project), "What settings does the app use?")
    second = user_of(orc.backends[LOCAL].requests[1])
    assert "=== .env ===" not in second and "Files Enoy could not give you: .env" in second


def test_edit_after_reading_a_file(orc_factory, project):
    orc = orc_factory(qwen=["<<<READ calculator.py>>>", ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "rebuild the maths helpers so there is a multiply")
    t = orc.db.get_task(task["id"])
    assert t["kind"] == "edit" and t["status"] == "awaiting_approval"


def test_whole_file_rewrite_of_unseen_file_is_refused(orc_factory, project):
    (project / "notes.txt").write_text("zzz\n" * 50, encoding="utf-8")  # Unrelated to the task, so never shown.
    rewrite = "Rewrote it.\n<<<FILE notes.txt\nprint('gone')\n>>>END\n"
    orc = orc_factory(qwen=[rewrite, rewrite], routing__max_local_attempts=2)
    orc.backends[CLAUDE].available = False
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    retry = user_of(orc.backends[LOCAL].requests[1])
    assert "did not see all of it" in retry
    assert orc.db.get_task(task["id"])["status"] == "failed"
    assert (project / "notes.txt").read_text(encoding="utf-8") == "zzz\n" * 50


def test_empty_project_folder_gets_a_clear_note(orc_factory, tmp_path):
    empty = tmp_path / "annat enoy"
    empty.mkdir()
    orc = orc_factory(qwen=["This folder is empty."])
    task = orc.start_task(open_project(orc, empty), "rebuild the header of my website")
    notes = [e["text"] for e in orc.db.events(task["id"]) if e["type"] == "note"]
    assert any("is empty" in n and "move the chat to that folder" in n for n in notes)
    assert "is empty" in system_of(orc.backends[LOCAL].requests[0])
    t = orc.db.get_task(task["id"])
    assert t["kind"] == "edit" and t["status"] == "completed" and t["local_attempts"] == 1  # Not retried or escalated.
    assert orc.backends[CLAUDE].requests == []


def test_excerpt_shows_lines_around_each_match():
    text = "\n".join(f"line {i}" for i in range(100)) + "\n#topbar { background: white; }\n" + "\n".join(
        f"tail {i}" for i in range(100))
    part = ctx.excerpt(text, "#TOPBAR", 10_000)
    assert part.startswith('[Only the parts around "#TOPBAR" are shown.]')
    assert "#topbar { background: white; }" in part and "line 88" in part and "line 87" not in part
    assert "tail 11" in part and "tail 12" not in part
    assert ctx.excerpt(text, "nowhere", 10_000) is None


def test_qwen_can_search_inside_a_long_file(orc_factory, project):
    css = "\n".join(f".rule{i} {{ color: red; }}" for i in range(2000)) + "\n#topbar { background: white; }\n"
    (project / "styles.css").write_text(css, encoding="utf-8")
    orc = orc_factory(qwen=["<<<READ styles.css | #topbar>>>\n<<<READ styles.css | nowhere>>>", "Found it."])
    orc.backends[LOCAL].settings.update(num_ctx=4096, num_predict=2048)
    orc.start_task(open_project(orc, project), "What colour is the top bar?")
    second = user_of(orc.backends[LOCAL].requests[1])
    assert "#topbar { background: white; }" in second and ".rule1 " not in second
    assert '"nowhere" does not occur in it' in second


def test_lookup_line_missing_a_bracket_still_counts():
    assert ctx.read_requests("<<<READ ui/styles.css | :root {>>\n<<<READ a.py") == ["ui/styles.css | :root {", "a.py"]


def test_guessed_edit_of_unseen_file_gets_a_lookup_hint(orc_factory, project):
    (project / "notes.txt").write_text("zzz\n" * 50, encoding="utf-8")
    guess = "Done.\n<<<EDIT notes.txt\n<<<SEARCH\nnot there\n===\nnew\n>>>REPLACE\n>>>END\n"
    orc = orc_factory(qwen=[guess, "<<<READ notes.txt | zzz>>>", ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    retry = user_of(orc.backends[LOCAL].requests[1])
    assert "without seeing it" in retry and "<<<READ notes.txt | text to find>>>" in retry
    assert "=== notes.txt" in user_of(orc.backends[LOCAL].requests[2])
    assert orc.db.get_task(task["id"])["status"] == "awaiting_approval"
