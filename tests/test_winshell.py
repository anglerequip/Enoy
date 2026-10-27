"""Taskbar identity: the shortcut ID tag and the window icon helper."""

import os
import subprocess

import pytest

from enoy import winshell

windows = pytest.mark.skipif(os.name != "nt", reason="Windows only")


def test_icon_file_exists():
    assert winshell.ICON.is_file()


def test_window_icon_needs_a_window():
    assert winshell.set_window_icon(0) is False


def test_missing_shortcut_is_skipped(tmp_path):
    assert winshell.tag_shortcut(tmp_path / "nope.lnk") is False


@windows
def test_shortcut_gets_app_id(tmp_path):
    lnk = tmp_path / "Enoy.lnk"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{os.environ['WINDIR']}\\notepad.exe';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=30)
    assert winshell.tag_shortcut(lnk) is True
    read = (f"$i=(New-Object -ComObject Shell.Application).NameSpace('{tmp_path}').ParseName('Enoy.lnk');"
            "$i.ExtendedProperty('System.AppUserModel.ID')")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", read], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == winshell.APP_ID
