# -*- coding: utf-8 -*-
"""把品牌图标的深蓝底（#0e306d 一族）重映射为品牌蓝，产出 assets/icon-blue.png。
原图 assets/icon.png 保持不变（仍是 favicon / README 用图）。
做法：只挑「深蓝底」像素（b 明显大于 g 且 g 大于 r 且整体偏暗），
按像素亮度比例映射到目标蓝，逐像素缩放 → 底纹的细微颗粒得以保留；
像素画本身的青色主体、黑色描边一律不动。
"""
import struct
import zlib
import os
from collections import Counter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "assets", "icon.png")
DST = os.path.join(BASE_DIR, "assets", "icon-blue.png")
BASE = (11, 87, 208)          # #0b57d0 —— Google Blue 700，与青绿主体对比度优于 #4285f4
NAVY_REF_LUM = 0.2126 * 14 + 0.7152 * 48 + 0.0722 * 109   # 原深蓝底亮度 ≈ 45.2


def read_png(path):
    d = open(path, "rb").read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    i = 8
    idat = b""
    while i < len(d):
        ln = struct.unpack(">I", d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        data = d[i + 8:i + 8 + ln]
        if typ == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", data[:10])
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
        i += 12 + ln
    assert bd == 8 and ct in (4, 6), "只支持 8 位灰度+alpha / RGBA"
    ch = {4: 2, 6: 4}[ct]
    raw = zlib.decompress(idat)
    stride = w * ch
    prev = bytearray(stride)
    rows = []
    pos = 0
    for _ in range(h):
        f = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        if f == 1:
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                line[x] = (line[x] + (a if (pa <= pb and pa <= pc) else (b if pb <= pc else c))) & 255
        rows.append(bytearray(line))
        prev = line
    return w, h, ch, rows


def write_png(path, w, h, ch, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ct = {2: 4, 4: 6}[ch]
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    out = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(out)


def is_navy(p):
    r, g, b = p[0], p[1], p[2]
    return (b > g + 20) and (g > r) and (b < 165) and (g < 125) and (r < 85)


def main():
    w, h, ch, rows = read_png(SRC)
    changed = 0
    kept = Counter()
    for y in range(h):
        row = rows[y]
        for x in range(w):
            o = x * ch
            p = row[o:o + ch]
            if is_navy(p):
                lum = 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]
                t = max(0.55, min(1.6, lum / NAVY_REF_LUM))
                for k in range(3):
                    row[o + k] = max(0, min(255, int(round(BASE[k] * t))))
                changed += 1
            else:
                kept[(p[0], p[1], p[2])] += 1
    write_png(DST, w, h, ch, rows)

    # 复核：新图的底色族应该落在品牌蓝附近
    w2, h2, ch2, rows2 = read_png(DST)
    cnt = Counter()
    for y in range(0, h2, 4):
        row = rows2[y]
        for x in range(0, w2, 4):
            cnt[(row[x * ch2], row[x * ch2 + 1], row[x * ch2 + 2])] += 1
    top = cnt.most_common(1)[0]
    print("源图 %dx%d，改写底色像素 %d / %d（%.1f%%）"
          % (w, h, changed, w * h, changed * 100.0 / (w * h)))
    print("新图最常见颜色 = #%02x%02x%02x  命中 %d 个采样点" % (top[0] + (top[1],)))
    print("原图仍保留：", os.path.isfile(SRC), "| 新文件字节数 =", os.path.getsize(DST))
    print("保留未改动的颜色（前 5）：")
    for c, n in kept.most_common(5):
        print("   #%02x%02x%02x  x%d" % (c[0], c[1], c[2], n))


main()
