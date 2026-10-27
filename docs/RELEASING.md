# Windows releases

`Enoy-Setup-x64.exe` includes Python, the desktop runtime, UI files, and a separate
console helper for stdio MCP. It never includes local app data, provider credentials,
project files or the build machine's virtual environment.

On Windows with Python 3.12 and Inno Setup 6.7.3:

```powershell
python -m pip install -r requirements-build.txt -r requirements-dev.txt
python -m pytest -q
./launch/build-release.ps1 -Iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
python launch/smoke-release.py
```

The output is the installer and `Enoy-Setup-x64.exe.sha256` under `dist/`.
Only packaged app files are copied into the installer. Dependency notices are
included under `THIRD-PARTY-NOTICES`.

For a new release, update `enoy/__init__.py`, `pyproject.toml` and the fallback
version in `launch/installer.iss`, commit, then push a matching `vX.Y.Z` tag.
The Windows release workflow runs the tests, builds and smoke-tests the package,
and publishes both assets. A manual workflow run produces a downloadable artifact
without publishing a release. The updater accepts stable numeric versions only,
requires the exact asset names, and verifies release checksums before execution.

Run installer smoke checks in an isolated folder with
`/VERYSILENT /NOICONS /TASKS="" /ENOYTEST=1 /DIR="C:\path\to\test-install"`.
That test flag suppresses uninstaller registration and shortcuts. Never use it for
normal installs. A clean Windows VM should also be used to verify first-time
provider installation and sign-in when changing dependencies.

Updates preserve app data and create a SQLite backup, including committed WAL data.
If an update fails to launch, reinstall a known-good release and preserve the data
directory; restore a pre-update database backup if a future schema change requires
it. Release assets are not Authenticode-signed yet. Checksums protect against corrupt
downloads; they do not replace publisher signing or protect a compromised GitHub
publisher account.
