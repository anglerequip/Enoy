# Build two launchers sharing a runtime: a desktop GUI and a console stdio helper.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path

root = Path(SPECPATH).parent
hidden = collect_submodules('enoy') + ['webview.platforms.winforms', 'webview.platforms.edgechromium']
data = [(str(root / 'enoy' / 'ui'), 'enoy/ui'),
        (str(root / 'launch' / 'enoy.ico'), 'launch'),
        (str(root / 'launch' / 'install-providers.ps1'), 'launch')]
data += collect_data_files('webview')
a = Analysis([str(root / 'launch' / 'desktop_entry.py')], pathex=[str(root)],
             binaries=[], datas=data, hiddenimports=hidden,
             excludes=['pytest', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython'],
             noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Enoy',
          debug=False, strip=False, upx=False, console=False,
          icon=str(root / 'launch' / 'enoy.ico'))
tools = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Enoy-tools',
            debug=False, strip=False, upx=False, console=True,
            icon=str(root / 'launch' / 'enoy.ico'))
coll = COLLECT(gui, tools, a.binaries, a.datas, strip=False, upx=False, name='Enoy')
