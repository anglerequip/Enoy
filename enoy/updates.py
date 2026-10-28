"""User-triggered updates from this project's stable GitHub Releases only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__, paths

REPOSITORY = "anglerequip/Enoy"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ASSET = "Enoy-Setup-x64.exe"
MAX_INSTALLER = 250 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("The release has an unsupported version number.")
    return tuple(int(n) for n in value.removeprefix("v").split("."))


def _open(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": f"Enoy/{__version__}", "Accept": "application/vnd.github+json"}), timeout=30)


def latest_release():
    try:
        with _open(API_URL) as response:
            data = json.loads(response.read(2_000_001))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise ValueError("GitHub did not return a stable release.")
    tag = data.get("tag_name")
    version_tuple(tag)
    assets = {a["name"]: a for a in data.get("assets", []) if isinstance(a, dict) and isinstance(a.get("name"), str)}
    installer, checksum = assets.get(ASSET), assets.get(ASSET + ".sha256")
    if not installer or not checksum:
        raise ValueError("The latest release does not include a complete Windows installer yet.")
    prefix = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    for asset, name in ((installer, ASSET), (checksum, ASSET + ".sha256")):
        if asset.get("browser_download_url") != prefix + name:
            raise ValueError("The update download does not belong to this project's release.")
    size = installer.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_INSTALLER:
        raise ValueError("The update installer has an invalid size.")
    return {"version": tag.removeprefix("v"), "name": str(data.get("name") or f"Enoy {tag}"),
            "notes": str(data.get("body") or ""), "published_at": data.get("published_at"),
            "url": installer["browser_download_url"],
            "checksum_url": checksum["browser_download_url"], "size": size,
            "digest": installer.get("digest"), "release_url": RELEASES_URL + "/tag/" + tag}


def download_release(release, folder, progress=lambda percent: None):
    with _open(release["checksum_url"]) as response:
        checksum = response.read(1025).decode("ascii").strip()
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?" + re.escape(ASSET), checksum)
    if not match:
        raise ValueError("The release checksum is invalid.")
    expected = match.group(1).lower()
    if release.get("digest") and release["digest"] != "sha256:" + expected:
        raise ValueError("GitHub's installer digest and release checksum do not match.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    partial, target = folder / (ASSET + ".part"), folder / ASSET
    digest, total = hashlib.sha256(), 0
    try:
        with _open(release["url"]) as response, partial.open("wb") as output:
            while chunk := response.read(256 * 1024):
                total += len(chunk)
                if total > release["size"] or total > MAX_INSTALLER:
                    raise ValueError("The downloaded installer is larger than expected.")
                digest.update(chunk)
                output.write(chunk)
                progress(round(total / release["size"] * 100))
        if total != release["size"] or digest.hexdigest() != expected:
            raise ValueError("The update failed its integrity check. Nothing was installed.")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def backup_user_data():
    """SQLite backup includes committed WAL data; installation never replaces it."""
    destination = paths.sub_dir("update-backups") / time.strftime("%Y%m%d-%H%M%S")
    destination.mkdir(parents=True, exist_ok=True)
    database = paths.database_path()
    if database.is_file():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(destination / "enoy.db") as target:
                source.backup(target)
    if paths.config_path().is_file():
        shutil.copy2(paths.config_path(), destination / "config.json")


def launch_installer(installer):
    args = [str(installer), "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/ENOYUPDATE=1"]
    if getattr(sys, "frozen", False):
        args.append("/DIR=" + str(Path(sys.executable).parent))
    subprocess.Popen(args, cwd=str(installer.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class UpdateManager:
    def __init__(self, busy=lambda: False, shutdown=lambda: None):
        self.busy, self.shutdown = busy, shutdown
        self.lock = threading.Lock()
        self.worker = None
        self.state = {"status": "idle", "current_version": __version__, "detail": "Check GitHub for a newer stable release.",
                      "release_url": RELEASES_URL}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, install=True):
        if os.name != "nt" and install:
            raise ValueError("Automatic installation is supported on Windows only.")
        with self.lock:
            if self.worker and self.worker.is_alive():
                return dict(self.state)
            if install and self.busy():
                raise ValueError("Wait for running tasks to finish before updating Enoy.")
            self.state.update(status="checking", progress=0, detail="Checking GitHub Releases…")
            self.worker = threading.Thread(target=self._run, args=(install,), daemon=True)
            self.worker.start()
            return dict(self.state)

    def _run(self, install):
        try:
            release = latest_release()
            if release is None:
                self._set(status="unpublished", detail="No desktop release has been published yet.")
                return
            if version_tuple(release["version"]) <= version_tuple(__version__):
                self._set(status="current", detail="You're using the latest stable version.")
                return
            self._set(latest_version=release["version"], release_url=release["release_url"])
            if not install:
                self._set(status="available", detail="A newer version is available.")
                return
            self._set(status="downloading", detail="Downloading the update…")
            installer = download_release(release, paths.sub_dir("updates") / release["version"],
                                         lambda percent: self._set(progress=percent))
            self._set(status="installing", detail="Preparing to install the update…")
            if self.busy():
                self._set(status="available", detail="Update downloaded. Finish running tasks and click Check for updates again to install.")
                return
            backup_user_data()
            launch_installer(installer)
            self._set(status="installing", detail="Installing the update. Enoy will restart; your chats and settings are kept.")
            time.sleep(1)
            self.shutdown()
        except Exception:
            # Do not echo remote response bodies or local credential paths into the UI.
            self._set(status="error", detail="Could not update Enoy. Check your internet connection and the release downloads, then try again.")
