#!/usr/bin/env python3
"""Decode AJ ImageArray cache files into PNG frames (pure Python, no packages).

usage:
  python decode_imagearray.py INPUT [--colors C1 C2 C3 C4 | --palette palette.json]
                              [--out DIR] [--scale 2] [-r] [--force]
                              [--no-shadow] [--keep-duplicate-frames] [--any-version]

INPUT is one file or a folder of cache files (zlib-compressed or already
decompressed; files that are not ImageArrays are reported and skipped).

COLOURS
  The files store palette INDEXES, not colours. The game recolours them at
  runtime (LayerBitmap.refreshLayerPalette -> PaletteHelper.BuildAnimalPalette)
  from FOUR base colours, so the same file looks different for each player.
  Give those four as hex RGB, WITHOUT a leading # (PowerShell treats # as a comment):
      --colors d9a05b f4e3c1 8a5a2b 333333
  Without --colors, four demo colours are used. --palette FILE overrides everything
  with an explicit list of 25 colours (index 0 unused).

OUTPUT
  OUT/<source file name>/frameNN_xX_yY.png     one folder per source file
  OUT/manifest.json                            what was done, and why anything was skipped

DUPLICATE CHECKS (recorded in manifest.json and kept between runs)
  * a source file whose decoded content was already processed is skipped
    (so re-running on the same folder only does new files);
  * a frame whose pixels AND position match a PNG already in OUT is not written
    again (listed as duplicate_of in the manifest). --keep-duplicate-frames
    writes every frame anyway.
  * --force starts over: everything is decoded and written again (use it after
    changing colours). Old files in OUT are not deleted.

RAW (--raw [gray|rainbow])
  No avatar colours at all. Each frame is written as an 8-bit INDEXED PNG whose pixels are the
  exact stored values (palette_index + 25 * t); its palette only gives them a grey step (or a hue
  per index with rainbow) and the alpha of t. Open it in any editor that edits indexed palettes
  to recolour without losing information, or read the pixel values with an image library
  (e.g. Pillow: Image.open(f).tobytes()). Shading masks are written next to it as *_shade.png
  (greyscale, darkness = s << sm); --no-shadow skips them. Not shading-applied, not scaled by
  anything but --scale. Use a new --out folder: the manifest remembers the mode.

FORMAT (from com.sbi.graphics.ImageArray / BitmapByteArray / LayerBitmap.paintFrame)
  AMF3 object {v: 117967104, f: [frame, ...]}
  image layer:    frame = {x, y, w, h, b: ByteArray [, s: ByteArray, sm: int]}
                  (x, y, w, h are in HALF-resolution pixels), or null for an empty image
  sequence layer: frame = an integer. This is the "layer 0" file: f[animation frame]
                  = the image index to show. It has no pixels; it is listed in the manifest.
  b is run-length encoded palette values:
     byte with bit 7 set -> value = byte & 0x7F, next byte = n, run of n + 1 pixels
     byte with bit 7 clear -> a single pixel of that value
  pixel value = palette_index + 25 * t
     palette_index 1..24 (0 = transparent), t = number of transparent source
     sub-pixels (0..3), which gives alpha 255, 192, 128, 64
  s (optional) is a shading mask, same size, same encoding: where s != 0 the
     pixel underneath is darkened by ((s << sm) + 24) / 255 (capped at 1).
"""
import argparse, hashlib, json, os, struct, sys, zlib

VERSION = 117967104
MAX_SOURCE_BYTES = 64 * 1024 * 1024
ALPHA = (255, 192, 128, 64)  # alpha for t = 0..3 (PaletteHelper.setColorGradient)
DEFAULT_COLORS = ("d9a05b", "f4e3c1", "8a5a2b", "333333")


# ---------------------------------------------------------------- AMF3 reader
class AMF3:
    def __init__(self, data):
        self.d, self.i = data, 0
        self.strs, self.objs, self.traits = [], [], []

    def byte(self):
        v = self.d[self.i]
        self.i += 1
        return v

    def u29(self):
        n = 0
        for _ in range(3):
            c = self.byte()
            if c & 0x80:
                n = (n << 7) | (c & 0x7F)
            else:
                return (n << 7) | c
        return (n << 8) | self.byte()

    def string(self):
        r = self.u29()
        if not r & 1:
            return self.strs[r >> 1]
        n = r >> 1
        if n == 0:
            return ""
        s = self.d[self.i:self.i + n].decode("utf8", "replace")
        self.i += n
        self.strs.append(s)
        return s

    def value(self):
        t = self.byte()
        if t in (0, 1):
            return None
        if t == 2:
            return False
        if t == 3:
            return True
        if t == 4:
            n = self.u29()
            return n - (1 << 29) if n & (1 << 28) else n
        if t == 5:
            v = struct.unpack(">d", self.d[self.i:self.i + 8])[0]
            self.i += 8
            return v
        if t == 6:
            return self.string()
        if t == 8:
            r = self.u29()
            if not r & 1:
                return self.objs[r >> 1]
            self.i += 8
            self.objs.append(None)
            return None
        if t == 9:
            r = self.u29()
            if not r & 1:
                return self.objs[r >> 1]
            lst = []
            self.objs.append(lst)
            while self.string() != "":
                self.value()
            for _ in range(r >> 1):
                lst.append(self.value())
            return lst
        if t == 10:
            r = self.u29()
            if not r & 1:
                return self.objs[r >> 1]
            if not r & 2:
                names, dyn = self.traits[r >> 2]
            else:
                dyn, n = bool(r & 8), r >> 4
                self.string()  # class name
                names = [self.string() for _ in range(n)]
                self.traits.append((names, dyn))
            o = {}
            self.objs.append(o)
            for k in names:
                o[k] = self.value()
            if dyn:
                while True:
                    k = self.string()
                    if k == "":
                        break
                    o[k] = self.value()
            return o
        if t == 12:
            r = self.u29()
            if not r & 1:
                return self.objs[r >> 1]
            n = r >> 1
            v = bytes(self.d[self.i:self.i + n])
            self.i += n
            self.objs.append(v)
            return v
        raise ValueError("unsupported AMF3 type %d at byte %d" % (t, self.i - 1))


# ------------------------------------------------------------------- palette
def parse_hex(s):
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError("colour %r is not RRGGBB" % s)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _gradient(pal, rgb, base, count):
    """PaletteHelper.setColorGradient: `count` shades, brightest at base+count-1."""
    r, g, b = rgb
    sr, sg, sb = r // (count + 2), g // (count + 2), b // (count + 2)
    i = base + count - 1
    for _ in range(count):
        pal[i] = (r, g, b)
        r, g, b = r - sr, g - sg, b - sb
        i -= 1


def _mix(a, b):
    return tuple((x + y) // 2 for x, y in zip(a, b))


def build_animal_palette(c1, c2, c3, c4):
    """PaletteHelper.BuildAnimalPalette: 24 colours (index 0 unused) from 4 base colours."""
    pal = [(0, 0, 0)] * 25
    for rgb, base, count in ((c1, 1, 3), (c2, 4, 3), (c3, 7, 3), (c4, 10, 3),
                             (_mix(c1, c2), 13, 2), (_mix(c2, c3), 15, 2), (_mix(c1, c3), 17, 2),
                             (_mix(c1, c4), 19, 2), (_mix(c2, c4), 21, 2), (_mix(c3, c4), 23, 2)):
        _gradient(pal, rgb, base, count)
    return pal


def build_palette(palette_path, colors):
    """returns (25 RGB tuples, label); the label goes in the manifest to catch colour changes"""
    if palette_path:
        text = open(palette_path, "rb").read()
        raw = json.loads(text)
        if len(raw) < 25:
            sys.exit("palette needs at least 25 entries, got %d" % len(raw))
        pal = []
        for c in raw[:25]:
            if isinstance(c, str):
                c = int(c.lstrip("#"), 16)
            pal.append(((c >> 16) & 255, (c >> 8) & 255, c & 255))
        return pal, "file:" + hashlib.sha256(text).hexdigest()[:16]
    cols = [parse_hex(c) for c in (colors or DEFAULT_COLORS)]
    return build_animal_palette(*cols), "colors:" + ",".join("%02x%02x%02x" % c for c in cols)


# ------------------------------------------------------------------- decoding
def unrle(b):
    out, i = [], 0
    while i < len(b):
        x = b[i]
        if x & 0x80:
            out.extend([x & 0x7F] * (b[i + 1] + 1))
            i += 2
        else:
            out.append(x)
            i += 1
    return out


def png_bytes(w, h, rgba):
    rows = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def unrle_fit(b, n):
    """unrle, padded with transparent pixels to n. The packer drops one pixel from runs of
    257, 513, ... (BitmapByteArray.processPackImage) and the game just stops early, so a
    stream one or a few pixels short is normal, not an error."""
    px = unrle(b)
    if len(px) > n:
        raise ValueError("decoded %d pixels, expected %d" % (len(px), n))
    return px + [0] * (n - len(px))


def render(frame, palette, scale, shadow=True):
    """returns (W, H, rgba bytes, had_shadow)"""
    w, h = frame["w"], frame["h"]
    px = unrle_fit(frame["b"], w * h)
    base = [None] * (w * h)
    for i, v in enumerate(px):
        idx, t = v % 25, v // 25
        if v == 0 or idx == 0 or t > 3:
            continue
        r, g, b = palette[idx]
        base[i] = [r, g, b, ALPHA[t]]
    had_shadow = False
    if shadow and frame.get("s"):
        spx = unrle_fit(frame["s"], w * h)
        if len(spx) == w * h:
            had_shadow = True
            sm = int(frame.get("sm") or 3)
            for i, v in enumerate(spx):
                if v and base[i]:
                    f = (v << sm) + 24
                    f = 1.0 if f > 255 else f / 255
                    base[i][0], base[i][1], base[i][2] = int(base[i][0] * f), int(base[i][1] * f), int(base[i][2] * f)
    W, H = w * scale, h * scale
    out = bytearray(W * H * 4)
    for y in range(h):
        for x in range(w):
            p = base[y * w + x]
            if p is None:
                continue
            px4 = bytes(p)
            for dy in range(scale):
                o = ((y * scale + dy) * W + x * scale) * 4
                for dx in range(scale):
                    out[o:o + 4] = px4
                    o += 4
    return W, H, out, had_shadow


def raw_palette(style):
    """100 entries for pixel value v = palette_index + 25 * t: (r, g, b, alpha). No avatar
    colours: grey steps (default) or a hue per index, so the indexes can be told apart."""
    import colorsys
    pal = [(0, 0, 0, 0)]
    for v in range(1, 100):
        idx, t = v % 25, v // 25
        if idx == 0:
            pal.append((0, 0, 0, 0))
        elif style == "rainbow":
            r, g, b = colorsys.hsv_to_rgb((idx - 1) / 24.0 * 0.85, 0.85, 1.0 if idx % 2 else 0.75)
            pal.append((int(r * 255), int(g * 255), int(b * 255), ALPHA[t]))
        else:
            g = 20 + idx * 10  # 30..260 -> index 24 is white-ish
            pal.append((min(g, 255),) * 3 + (ALPHA[t],))
    return pal


def indexed_png(w, h, values, scale, pal):
    """8-bit indexed PNG: each pixel is the original value v, the palette gives it a grey/hue and
    the transparency step. Edit the palette (PLTE / tRNS) to recolour; the pixels stay exact."""
    W = w * scale
    rows = bytearray()
    for y in range(h):
        row = bytearray()
        for x in range(w):
            v = values[y * w + x]
            row += bytes([v if v < len(pal) else 0]) * scale
        for _ in range(scale):
            rows += b"\x00" + row

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    plte = b"".join(bytes(c[:3]) for c in pal)
    trns = bytes(c[3] for c in pal)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, h * scale, 8, 3, 0, 0, 0))
            + chunk(b"PLTE", plte) + chunk(b"tRNS", trns) + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def render_raw(frame, scale, style, shadow=True):
    """returns (W, H, png bytes, had_shadow); the shading mask goes in a second PNG (see main)"""
    w, h = frame["w"], frame["h"]
    px = unrle_fit(frame["b"], w * h)
    return w * scale, h * scale, indexed_png(w, h, px, scale, raw_palette(style)), bool(frame.get("s"))


def raw_shadow_png(frame, scale):
    """the shading mask as an 8-bit greyscale PNG: darkness = min(255, s << sm); None if absent"""
    if not frame.get("s"):
        return None
    w, h = frame["w"], frame["h"]
    sm = int(frame.get("sm") or 3)
    sp = unrle_fit(frame["s"], w * h)
    W = w * scale
    rows = bytearray()
    for y in range(h):
        row = bytearray()
        for x in range(w):
            row += bytes([min(255, sp[y * w + x] << sm)]) * scale
        for _ in range(scale):
            rows += b"\x00" + row

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, h * scale, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def read_source(path, any_version):
    """returns (sha256 of the decoded bytes, parsed object); raises ValueError if it is not an ImageArray"""
    if os.path.getsize(path) > MAX_SOURCE_BYTES:
        raise ValueError("file too large")
    raw = open(path, "rb").read()
    try:
        data = zlib.decompress(raw)
    except zlib.error:
        data = raw  # already decompressed
    try:
        obj = AMF3(data).value()
    except (ValueError, IndexError, struct.error) as e:
        raise ValueError("not AMF3 data (%s)" % e)
    if isinstance(obj, dict) and isinstance(obj.get("f"), bytes):
        obj["f"] = list(obj["f"])  # an uncompressed sequence file stores f as a ByteArray
    if not isinstance(obj, dict) or not isinstance(obj.get("f"), list):
        raise ValueError("not an ImageArray (no frame list)")
    if obj.get("v") != VERSION and not any_version:
        raise ValueError("version %r, expected %d (use --any-version to try anyway)" % (obj.get("v"), VERSION))
    return hashlib.sha256(data).hexdigest(), obj


# ------------------------------------------------------------------- batch
def list_inputs(root, out_dir, recursive):
    if os.path.isfile(root):
        return [(os.path.basename(root), root)]
    found = []
    out_abs = os.path.abspath(out_dir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if os.path.abspath(os.path.join(dirpath, d)) != out_abs)
        for name in sorted(filenames):
            if name == "manifest.json":
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "__")
            found.append((rel, full))
        if not recursive:
            break
    return found


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="a cache file, or a folder of them")
    ap.add_argument("--colors", nargs=4, metavar=("C1", "C2", "C3", "C4"),
                    help="the four base colours as RRGGBB hex, without #")
    ap.add_argument("--palette", help="explicit list of 25 colours (JSON); overrides --colors")
    ap.add_argument("--out", default="frames")
    ap.add_argument("--raw", nargs="?", const="gray", choices=["gray", "rainbow"],
                    help="no avatar colours: indexed PNGs holding the exact stored values (see RAW). "
                         "Use a fresh --out folder.")
    ap.add_argument("--scale", type=int, default=2, help="2 = size before the packer halved it (frames are stored at half size)")
    ap.add_argument("-r", "--recursive", action="store_true", help="also read sub-folders")
    ap.add_argument("--force", action="store_true", help="ignore the manifest and redo everything")
    ap.add_argument("--no-shadow", action="store_true", help="ignore the shading mask (s) in frames")
    ap.add_argument("--keep-duplicate-frames", action="store_true", help="write identical frames again")
    ap.add_argument("--any-version", action="store_true", help="try files whose version field differs")
    return ap


def main():
    a = make_parser().parse_args()

    try:
        pal, pal_label = build_palette(a.palette, a.colors)
    except ValueError as e:
        sys.exit(str(e))
    if a.raw:
        pal_label = "raw:" + a.raw
    elif not a.palette and not a.colors:
        print("note: using demo colours; pass --colors C1 C2 C3 C4 for the real ones\n")
    os.makedirs(a.out, exist_ok=True)
    mpath = os.path.join(a.out, "manifest.json")
    label = pal_label + (",noshadow" if a.no_shadow else "")
    manifest = {"palette": label, "scale": a.scale, "sources": {}, "content_index": {}, "frame_index": {}}
    if os.path.exists(mpath) and not a.force:
        manifest = json.load(open(mpath))
        if manifest.get("palette") != label or manifest.get("scale") != a.scale:
            sys.exit("%s was made with %r, scale %r; you asked for %r, %r.\n"
                     "Use --force to redo everything, or a different --out folder."
                     % (mpath, manifest.get("palette"), manifest.get("scale"), label, a.scale))

    inputs = list_inputs(a.input, a.out, a.recursive)
    if not inputs:
        sys.exit("no files found in %s" % a.input)
    counts = {"decoded": 0, "already done": 0, "duplicate source": 0, "sequence files": 0, "errors": 0,
              "frames written": 0, "duplicate frames": 0, "empty frames": 0}

    try:
        for name, full in inputs:
            try:
                sha, obj = read_source(full, a.any_version)
            except (ValueError, OSError) as e:
                manifest["sources"][name] = {"status": "error", "error": str(e)}
                counts["errors"] += 1
                print("skip  %s: %s" % (name, e))
                continue
            first = manifest["content_index"].get(sha)
            if first is not None and not a.force:
                if first == name:
                    counts["already done"] += 1
                    print("done  %s (already in manifest)" % name)
                else:
                    manifest["sources"][name] = {"status": "duplicate", "sha256": sha, "duplicate_of": first}
                    counts["duplicate source"] += 1
                    print("dupe  %s (same content as %s)" % (name, first))
                continue

            frames = obj["f"]
            if frames and all(isinstance(f, int) and not isinstance(f, bool) or f is None for f in frames) \
                    and any(isinstance(f, int) for f in frames):
                manifest["sources"][name] = {"status": "ok", "kind": "sequence", "sha256": sha, "sequence": frames}
                manifest["content_index"][sha] = name
                counts["sequence files"] += 1
                print("seq   %s: animation order of %d steps (no pixels); see manifest" % (name, len(frames)))
                continue

            folder = os.path.join(a.out, name)
            os.makedirs(folder, exist_ok=True)
            entry = {"status": "ok", "sha256": sha, "frames": []}
            for n, fr in enumerate(frames):
                rec = {"index": n}
                if fr is None:
                    rec["kind"] = "empty"
                    counts["empty frames"] += 1
                elif not isinstance(fr, dict):
                    rec["kind"] = "error"
                    rec["error"] = "unexpected frame type %s" % type(fr).__name__
                    counts["errors"] += 1
                else:
                    try:
                        if a.raw:
                            W, H, rawpng, had_shadow = render_raw(fr, a.scale, a.raw)
                        else:
                            W, H, rgba, had_shadow = render(fr, pal, a.scale, not a.no_shadow)
                    except (ValueError, KeyError, TypeError, IndexError) as e:
                        rec["kind"] = "error"
                        rec["error"] = str(e)
                        counts["errors"] += 1
                        entry["frames"].append(rec)
                        print("  frame %d of %s: %s" % (n, name, e))
                        continue
                    data = rawpng if a.raw else png_bytes(W, H, rgba)
                    fsha = hashlib.sha256(("%d,%d:" % (fr["x"], fr["y"])).encode() + data).hexdigest()
                    rel = "%s/frame%02d_x%d_y%d.png" % (name, n, fr["x"] * 2, fr["y"] * 2)
                    rec.update(kind="image", x=fr["x"] * 2, y=fr["y"] * 2, w=W, h=H, sha256=fsha, shadow=had_shadow)
                    same = manifest["frame_index"].get(fsha)
                    if same and not a.keep_duplicate_frames and os.path.exists(os.path.join(a.out, same)):
                        rec["duplicate_of"] = same
                        counts["duplicate frames"] += 1
                    else:
                        with open(os.path.join(a.out, rel), "wb") as f:
                            f.write(data)
                        if a.raw and not a.no_shadow:
                            sh = raw_shadow_png(fr, a.scale)
                            if sh:
                                with open(os.path.join(a.out, rel[:-4] + "_shade.png"), "wb") as f:
                                    f.write(sh)
                        rec["file"] = rel
                        manifest["frame_index"].setdefault(fsha, rel)
                        counts["frames written"] += 1
                entry["frames"].append(rec)
            manifest["sources"][name] = entry
            manifest["content_index"][sha] = name
            counts["decoded"] += 1
            written = sum(1 for r in entry["frames"] if "file" in r)
            print("ok    %s: %d frames, %d written" % (name, len(entry["frames"]), written))
            if not written:
                try:
                    os.rmdir(folder)  # nothing was written into it
                except OSError:
                    pass
    finally:
        with open(mpath, "w") as f:
            json.dump(manifest, f, indent=1)

    print("\nsummary: " + ", ".join("%s %d" % (k, v) for k, v in counts.items()))
    print("manifest:", mpath)


if __name__ == "__main__":
    main()
