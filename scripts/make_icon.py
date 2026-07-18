#!/usr/bin/env python3
"""Generate the PLite icon: a calm card glyph on the dark canvas.

Pure Python PNG writer (zlib + struct, no dependencies), then sips +
iconutil turn it into a proper .icns. Colors are DESIGN.md tokens.
"""

import struct
import subprocess
import sys
import zlib
from pathlib import Path

S = 1024
BG = (0x0F, 0x11, 0x15, 255)        # --bg
PANEL = (0x17, 0x1A, 0x21, 255)     # --panel
LINE = (0x25, 0x2A, 0x34, 255)      # --line
TEAL = (0x4F, 0xD6, 0xBE, 255)      # active/positive
AMBER = (0xE0, 0xAF, 0x68, 255)     # next step
TEXT = (0xE6, 0xE8, 0xEC, 255)
DIM = (0x8B, 0x93, 0xA1, 255)


def rounded(px, x0, y0, x1, y1, r, color):
    for y in range(max(0, y0), min(S, y1)):
        for x in range(max(0, x0), min(S, x1)):
            dx = max(x0 + r - x, 0, x - (x1 - 1 - r))
            dy = max(y0 + r - y, 0, y - (y1 - 1 - r))
            if dx * dx + dy * dy <= r * r:
                px[y][x] = color


def main(out_png):
    px = [[(0, 0, 0, 0)] * S for _ in range(S)]

    # macOS icon grid: content within ~10% margins, big rounded square
    rounded(px, 100, 100, 924, 924, 232, BG)
    # the card, slightly raised
    rounded(px, 240, 260, 784, 764, 56, PANEL)
    rounded(px, 240, 260, 784, 268, 4, LINE)          # top hairline
    # headline bar (text primary)
    rounded(px, 300, 360, 660, 404, 20, TEXT)
    # two secondary lines
    rounded(px, 300, 452, 724, 484, 16, DIM)
    rounded(px, 300, 516, 600, 548, 16, DIM)
    # the next-step line, amber - the one thing to do now
    rounded(px, 300, 610, 690, 654, 20, AMBER)
    # the teal thread dot, top-right of the card
    rounded(px, 668, 316, 736, 384, 34, TEAL)

    raw = b"".join(
        b"\x00" + b"".join(struct.pack("4B", *p) for p in row)
        for row in px)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    Path(out_png).write_bytes(png)
    print(f"wrote {out_png}")


def make_icns(png, icns):
    iconset = Path("/tmp/plite.iconset")
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 64, 128, 256, 512, 1024):
        for scale, suffix in ((1, ""), (2, "@2x")):
            target = size * scale
            if target > 1024:
                continue
            name = f"icon_{size}x{size}{suffix}.png"
            subprocess.run(["sips", "-z", str(target), str(target),
                            png, "--out", str(iconset / name)],
                           capture_output=True, check=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset),
                    "-o", icns], check=True)
    print(f"wrote {icns}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/plite_icon.png"
    icns = sys.argv[2] if len(sys.argv) > 2 else "/tmp/PLite.icns"
    main(out)
    make_icns(out, icns)
