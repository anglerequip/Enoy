"""Copy license texts for distributions included in the desktop runtime."""
from importlib.metadata import distribution
from pathlib import Path
import sys

target = Path('dist/Enoy/THIRD-PARTY-NOTICES')
target.mkdir(parents=True, exist_ok=True)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    (target / 'Python-LICENSE.txt').write_bytes(python_license.read_bytes())
for name in ('pywebview', 'bottle', 'clr_loader', 'pythonnet', 'proxy_tools', 'cffi', 'pycparser', 'typing_extensions', 'pyinstaller', 'setuptools', 'packaging'):
    dist = distribution(name)
    folder = target / name
    folder.mkdir(exist_ok=True)
    (folder / 'METADATA.txt').write_text(dist.read_text('METADATA') or '', encoding='utf-8')
    for file in dist.files or []:
        if any(part.lower().startswith(('license', 'copying', 'notice')) for part in file.parts):
            source = Path(dist.locate_file(file))
            if source.is_file():
                (folder / source.name).write_bytes(source.read_bytes())
