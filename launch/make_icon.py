"""Build the Enoy Windows icon (launch/enoy.ico) from enoy/ui/logo.svg.

Microsoft Edge (always present on Windows 10/11) renders the SVG headlessly at
each icon size; the PNGs are packed into an .ico container (PNG entries are
supported since Windows Vista). Pure standard library otherwise.
Usage: python make_icon.py [output.ico]
"""

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "enoy" / "ui" / "logo.svg"
SIZES = (256, 64, 48, 32, 24, 16)
EDGE_PATHS = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
              Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]


def render_png(edge: Path, size: int, work: Path) -> bytes:
    page = work / f"icon{size}.html"
    pad = max(1, round(size * 0.04))
    page.write_text(f'<!doctype html><html><body style="margin:0;background:transparent;overflow:hidden">'
                    f'<img src="{SVG.as_uri()}" style="display:block;margin:{pad}px;width:{size - 2 * pad}px;'
                    f'height:{size - 2 * pad}px"></body></html>', encoding="utf-8")
    out = work / f"icon{size}.png"
    subprocess.run([str(edge), "--headless", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                    f"--window-size={size},{size}", f"--screenshot={out}", f"--user-data-dir={work / 'profile'}",
                    page.as_uri()], check=True, capture_output=True, timeout=60)
    data = out.read_bytes()
    if not data.startswith(b"\x89PNG"):
        raise RuntimeError(f"Edge did not produce a PNG for size {size}")
    return data


def pack_ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs = b"", b""
    offset = 6 + 16 * len(images)
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + entries + blobs


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "launch" / "enoy.ico"
    edge = next((p for p in EDGE_PATHS if p.is_file()), None)
    if edge is None:
        print("Microsoft Edge was not found; cannot render the icon.")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        images = [(s, render_png(edge, s, Path(tmp))) for s in SIZES]
    target.write_bytes(pack_ico(images))
    print(f"Wrote {target} ({target.stat().st_size:,} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
