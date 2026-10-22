#!/usr/bin/env python3
"""Animated image writers for asset_gui.py (standard library only; Pillow used for GIF when installed).

  apng_encode(frames, w, h, delays_ms, loops=0)       -> bytes   animated PNG: full quality
  gif_encode(frames, w, h, delays_ms, loops=0, transparent=True, pillow=None) -> bytes
        Pillow is used when it can be imported (pillow=None: auto; True / False: force);
        otherwise a built-in encoder is used (256 colours, 1-bit transparency, slower).
  has_pillow()

`frames` is a list of RGBA byte strings, w * h * 4 each. delays_ms is one number or a list.
loops=0 means forever.
"""
import array, collections, io, struct, zlib


def has_pillow():
    try:
        import PIL.Image  # noqa: F401
        return True
    except ImportError:
        return False


def _delays(delays_ms, n):
    if isinstance(delays_ms, (int, float)):
        return [int(delays_ms)] * n
    return [int(d) for d in delays_ms]


# ------------------------------------------------------------------ APNG
def _chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def apng_encode(frames, w, h, delays_ms, loops=0):
    n = len(frames)
    delays = _delays(delays_ms, n)
    out = [b"\x89PNG\r\n\x1a\n", _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)),
           _chunk(b"acTL", struct.pack(">II", n, loops))]
    seq = 0
    stride = w * 4
    for i, fb in enumerate(frames):
        out.append(_chunk(b"fcTL", struct.pack(">IIIIIHHBB", seq, w, h, 0, 0, max(1, delays[i]), 1000, 0, 0)))
        seq += 1
        raw = b"".join(b"\x00" + fb[y * stride:(y + 1) * stride] for y in range(h))
        z = zlib.compress(raw, 6)
        if i == 0:
            out.append(_chunk(b"IDAT", z))
        else:
            out.append(_chunk(b"fdAT", struct.pack(">I", seq) + z))
            seq += 1
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


# ------------------------------------------------------------------ GIF, built in
def _median_cut(counts, n):
    """counts: {(r,g,b): weight}. returns ({colour: palette index}, [palette (r,g,b)...])"""
    boxes = [list(counts.items())]
    while len(boxes) < n:
        best, bi = None, -1
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            spans = [max(c[0][k] for c in box) - min(c[0][k] for c in box) for k in range(3)]
            score = max(spans) * sum(c[1] for c in box) ** 0.5
            if best is None or score > best[0]:
                best, bi = (score, spans.index(max(spans))), i
        if bi < 0:
            break
        box = boxes.pop(bi)
        ch = best[1]
        box.sort(key=lambda c: c[0][ch])
        half, acc = sum(c[1] for c in box) / 2, 0
        cut = 1
        for j, c in enumerate(box):
            acc += c[1]
            if acc >= half:
                cut = min(max(j + 1, 1), len(box) - 1)
                break
        boxes += [box[:cut], box[cut:]]
    pal, index = [], {}
    for k, box in enumerate(boxes):
        tot = sum(c[1] for c in box)
        pal.append(tuple(int(sum(c[0][i] * c[1] for c in box) / tot + 0.5) for i in range(3)))
        for c in box:
            index[c[0]] = k
    return index, pal


def _lzw(data, min_code=8):
    clear, eoi = 1 << min_code, (1 << min_code) + 1
    out, cur, nbits = bytearray(), 0, 0
    size, nxt, table = min_code + 1, eoi + 1, {}

    cur |= clear << nbits
    nbits += size
    prefix = data[0]
    for b in data[1:]:
        key = (prefix << 8) | b
        c = table.get(key)
        if c is not None:
            prefix = c
            continue
        cur |= prefix << nbits
        nbits += size
        while nbits >= 8:
            out.append(cur & 255)
            cur >>= 8
            nbits -= 8
        if nxt < 4096:
            table[key] = nxt
            if nxt == (1 << size) and size < 12:
                size += 1
            nxt += 1
        else:
            cur |= clear << nbits
            nbits += size
            while nbits >= 8:
                out.append(cur & 255)
                cur >>= 8
                nbits -= 8
            table.clear()
            size, nxt = min_code + 1, eoi + 1
        prefix = b
    cur |= prefix << nbits
    nbits += size
    cur |= eoi << nbits
    nbits += size
    while nbits > 0:
        out.append(cur & 255)
        cur >>= 8
        nbits -= 8
    return bytes(out)


def _blocks(data):
    return b"".join(bytes([len(data[i:i + 255])]) + data[i:i + 255] for i in range(0, len(data), 255)) + b"\x00"


def _gif_builtin(frames, w, h, delays, loops, transparent):
    # one pass: the colour of every opaque-enough pixel, over all frames
    packed = []
    counts = collections.Counter()
    for fb in frames:
        px = array.array("I")
        px.frombytes(fb)
        if px.itemsize != 4:
            raise RuntimeError("unexpected integer size")
        # little-endian machine: R is the low byte; big-endian: swap
        keys = []
        for v in px:
            a = (v >> 24) if _LITTLE else (v & 255)
            if transparent and a < 128:
                keys.append(-1)
            else:
                keys.append((v & 0xFFFFFF) if _LITTLE else (v >> 8))
        packed.append(keys)
        counts.update(keys)
    counts.pop(-1, None)

    def rgb(k):
        return (k & 255, (k >> 8) & 255, (k >> 16) & 255) if _LITTLE else ((k >> 16) & 255, (k >> 8) & 255, k & 255)

    limit = 255 if transparent else 256
    colours = {rgb(k): n for k, n in counts.items()}
    if len(colours) <= limit:
        pal = list(colours)
        index = {c: i for i, c in enumerate(pal)}
    else:
        index, pal = _median_cut(colours, limit)
    tidx = 255 if transparent else 0
    lut = {k: index[rgb(k)] for k in counts}
    lut[-1] = tidx
    gct = b"".join(bytes(c) for c in pal) + b"\x00\x00\x00" * (256 - len(pal))

    out = [b"GIF89a", struct.pack("<HHBBB", w, h, 0xF7, 0, 0), gct,
           b"\x21\xFF\x0BNETSCAPE2.0\x03\x01" + struct.pack("<H", loops) + b"\x00"]
    for keys, delay in zip(packed, delays):
        flags = (2 << 2) | (1 if transparent else 0)  # restore to background; transparency flag
        out.append(b"\x21\xF9\x04" + bytes([flags]) + struct.pack("<H", max(2, round(delay / 10))) + bytes([tidx]) + b"\x00")
        out.append(b"\x2C" + struct.pack("<HHHHB", 0, 0, w, h, 0))
        out.append(b"\x08" + _blocks(_lzw(bytes(lut[k] for k in keys))))
    out.append(b"\x3B")
    return b"".join(out)


_LITTLE = struct.pack("<I", 1) == struct.pack("=I", 1)


# ------------------------------------------------------------------ GIF, with Pillow
def _gif_pillow(frames, w, h, delays, loops, transparent):
    from PIL import Image
    rgbs, alphas = [], []
    for fb in frames:
        im = Image.frombytes("RGBA", (w, h), fb)
        rgbs.append(im.convert("RGB"))
        alphas.append(im.getchannel("A"))
    # one palette for the whole animation: quantize all frames stacked in a single picture
    sheet = Image.new("RGB", (w, h * len(rgbs)))
    for i, im in enumerate(rgbs):
        sheet.paste(im, (0, i * h))
    ref = sheet.quantize(colors=255 if transparent else 256, dither=0)
    out = []
    for rgb, alpha in zip(rgbs, alphas):
        p = rgb.quantize(palette=ref, dither=0)
        if transparent:
            p.paste(255, alpha.point(lambda a: 255 if a < 128 else 0))
        out.append(p)
    buf = io.BytesIO()
    kw = {"transparency": 255} if transparent else {}
    out[0].save(buf, "GIF", save_all=True, append_images=out[1:], duration=delays, loop=loops,
                disposal=2, optimize=False, **kw)
    return buf.getvalue()


def gif_encode(frames, w, h, delays_ms, loops=0, transparent=True, pillow=None):
    delays = _delays(delays_ms, len(frames))
    use = has_pillow() if pillow is None else pillow
    if use:
        try:
            return _gif_pillow(frames, w, h, delays, loops, transparent)
        except ImportError:
            pass
    return _gif_builtin(frames, w, h, delays, loops, transparent)
