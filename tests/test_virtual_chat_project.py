from pathlib import Path


def test_imported_msix_chat_folder_is_not_a_code_workspace(orc_factory):
    orc=orc_factory()
    old=Path.home()/'AppData'/'Local'/'Packages'/'OpenAI.Codex_example'/'LocalCache'/'Local'/'Enoy'/'general-chat'
    assert orc.is_default_project({'path':str(old)})


def test_regular_folder_named_general_chat_is_not_reclassified(orc_factory, tmp_path):
    orc=orc_factory()
    assert not orc.is_default_project({'path':str(tmp_path/'my-project'/'general-chat')})
