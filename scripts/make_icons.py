#!/usr/bin/env python3
"""Create the app icons (180, 192, 512 px) if they do not exist yet. Standard library only."""
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BG = (15, 23, 42)        # deep navy
PAPER = (242, 244, 247)  # light paper
BAR = (31, 79, 216)      # cobalt header
MARK = (255, 226, 90)    # highlighter yellow
INK = (15, 23, 42)


def pixel(x, y, s):
    m = int(s * 0.17)
    if not (m <= x < s - m and m <= y < s - m):
        return BG
    pad = int(s * 0.07)
    head = int(s * 0.13)
    if y < m + head:
        return BAR
    ly = y - (m + head) - int(s * 0.05)
    unit = int(s * 0.07)
    if ly < 0 or unit == 0:
        return PAPER
    k, off = divmod(ly, unit)
    if k % 2 == 0 and k < 10 and off < unit * 0.62 or (k % 2 == 0 and k < 10):
        right = s - m - pad - (int(s * 0.22) if k % 3 == 1 else 0)
        left = m + pad
        if left <= x < right:
            # one highlighted word on the second line
            if k == 2 and left + int(s * 0.12) <= x < left + int(s * 0.34):
                return MARK
            return INK
    return PAPER


def write_png(path, s):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(s):
        raw.append(0)
        for x in range(s):
            raw.extend(pixel(x, y, s))
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


for size in (180, 192, 512):
    p = os.path.join(ROOT, f"icon-{size}.png")
    if not os.path.exists(p):
        write_png(p, size)
        print("created", p)
