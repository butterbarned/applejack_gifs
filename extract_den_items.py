#!/usr/bin/env python3
"""Turn downloaded den item files (items/<hash>) into .swf files and PNG pictures.

Needs mirror_assets.py next to it, a cache made by `mirror_assets.py --types defs,den`, and,
for the PNGs, JPEXS Free Flash Decompiler (ffdec) and Java. Without ffdec the .swf files are
still written (--no-render) and can be opened in any SWF tool.

usage:
  python extract_den_items.py                      everything in the cache
  python extract_den_items.py --item 617 --item 5  only these den item ids
  python extract_den_items.py --no-render          just unwrap the .swf files
  python extract_den_items.py --ffdec "C:\\Program Files (x86)\\FFDec\\ffdec-cli.exe" --jobs 4
  python extract_den_items.py --ffdec "java -jar /path/ffdec.jar"

options: --cache DIR (default cache)  --build N (1830)  --out DIR (default den-out)
         --ffdec CMD   how to run ffdec (default: ffdec on PATH, else ffdec-cli.jar in tools/ffdec)
         --jobs N      parallel ffdec processes (default 4; each is a Java process)
         --no-crop     keep ffdec's full-stage canvas instead of trimming to the drawing
         --limit N     stop after N files (for a trial)
         --skip-existing  leave items that already have a picture alone (to continue an interrupted run)
         --batch N     draw and save N items at a time, with progress and a time estimate (use with --skip-existing
                       for long runs: stop with Ctrl+C and start the same command again to carry on)

FILES (the game: loader/DenItemHelper, DenItemServer; den/DenItem.getIconId)
  items/<hash(str(defId << 16 | category))> is an AMF3 object {v, ba, cx, cy, so, r}:
  ba is a (zlib) SWF; cx, cy, so and r are placement values the den editor uses (centre offset,
  sort offset, ...); they are copied to meta.json untouched, their exact meaning is not decoded.
  Category 1 is the icon / in-world picture of most items; 2 and 3 are other views.
  den item ids and names come from defPack 1030.

OUTPUT  OUT/swf/<defId>_c<cat>_<name>.swf   OUT/png/<defId>_c<cat>_<name>[_fNN].png
        OUT/meta.json  {file: {defId, category, name, cx, cy, so, r, frames}}
"""
import argparse, json, os, shutil, struct, subprocess, sys, tempfile, time, zlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mirror_assets as M

MAX_CAT = 8


# ------------------------------------------------------------------ png crop
def read_png(data):
    """-> (w, h, rgba bytes) for 8-bit RGB / RGBA PNGs, else None"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i, idat, w = 8, b"", None
    while i < len(data):
        n, tag = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        i += 12 + n
    if w is None or depth != 8 or ctype not in (2, 6) or interlace:
        return None
    bpp = 4 if ctype == 6 else 3
    raw = zlib.decompress(idat)
    stride = w * bpp
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = row[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                row[x] = (row[x] + a) & 255
            elif f == 2:
                row[x] = (row[x] + b) & 255
            elif f == 3:
                row[x] = (row[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[x] = (row[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += row
        prev = row
    if bpp == 3:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            rgba[i * 4:i * 4 + 3] = out[i * 3:i * 3 + 3]
            rgba[i * 4 + 3] = 255
        return w, h, bytes(rgba)
    return w, h, bytes(out)


def png_bytes(w, h, rgba):
    rows = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def crop_group(datas):
    """crop the PNG frames of one item to the box that holds every frame's drawing (so animation
    frames stay the same size and line up). Returns the new PNG bytes, or the inputs unchanged
    if they cannot be read or are empty. Uses Pillow when installed (fast), else plain Python."""
    try:
        import io
        from PIL import Image
        ims = [Image.open(io.BytesIO(d)).convert("RGBA") for d in datas]
        boxes = [b for b in (im.getchannel("A").getbbox() for im in ims) if b]
        if not boxes or len({im.size for im in ims}) != 1:
            return datas
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        out = []
        for im in ims:
            buf = io.BytesIO()
            im.crop(box).save(buf, "PNG")
            out.append(buf.getvalue())
        return out
    except ImportError:
        pass
    imgs = [read_png(d) for d in datas]
    if not imgs or any(i is None for i in imgs) or len({(i[0], i[1]) for i in imgs}) != 1:
        return datas
    w, h = imgs[0][0], imgs[0][1]
    x0, y0, x1, y1 = w, h, 0, 0
    for _, _, px in imgs:
        alpha = px[3::4]
        cols = [x for x in range(w) if any(alpha[x::w])]
        if not cols:
            continue
        rows = [y for y in range(h) if any(alpha[y * w:(y + 1) * w])]
        x0, x1, y0, y1 = min(x0, cols[0]), max(x1, cols[-1] + 1), min(y0, rows[0]), max(y1, rows[-1] + 1)
    if x1 <= x0:
        return datas
    return [png_bytes(x1 - x0, y1 - y0, b"".join(px[(y * w + x0) * 4:(y * w + x1) * 4] for y in range(y0, y1)))
            for _, _, px in imgs]


# ------------------------------------------------------------------ ffdec
def strip_background(swf):
    """a copy of the SWF without its SetBackgroundColor tag, so ffdec draws on transparency
    instead of the movie's background colour. Only used for rendering; the .swf files that are
    kept are exactly what the CDN sent. Returns the input unchanged if it cannot be parsed."""
    try:
        if swf[:3] == b"CWS":
            body = zlib.decompress(swf[8:])
        elif swf[:3] == b"FWS":
            body = swf[8:]
        else:
            return swf
        nbits = body[0] >> 3
        i = head = (5 + 4 * nbits + 7) // 8 + 4  # frame rect, frame rate, frame count
        out = bytearray(body[:head])
        while i < len(body):
            h = struct.unpack("<H", body[i:i + 2])[0]
            tag, ln, hl = h >> 6, h & 63, 2
            if ln == 63:
                ln, hl = struct.unpack("<I", body[i + 2:i + 6])[0], 6
            end = i + hl + ln
            if tag != 9:
                out += body[i:end]
            i = end
            if tag == 0:
                break
        return b"FWS" + swf[3:4] + struct.pack("<I", 8 + len(out)) + bytes(out)
    except (struct.error, zlib.error, IndexError):
        return swf


def find_ffdec(arg):
    if arg:
        if os.path.isfile(arg):  # a path, possibly with spaces (C:\\Program Files\\FFDec\\ffdec-cli.exe)
            return ["java", "-jar", arg] if arg.lower().endswith(".jar") else [arg]
        return arg.split()
    if shutil.which("ffdec"):
        return ["ffdec"]
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "tools", "ffdec", "ffdec-cli.jar"), os.path.join(here, "ffdec", "ffdec-cli.jar"),
              os.path.join(here, "ffdec.jar")):
        if os.path.exists(p):
            return ["java", "-jar", p]
    return None


def render_chunk(cmd, swf_dir, out_dir):
    # headless: no window system is needed (a server has none) and nothing pops up on a desktop either
    env = dict(os.environ)
    if "java.awt.headless" not in env.get("JAVA_TOOL_OPTIONS", ""):
        env["JAVA_TOOL_OPTIONS"] = (env.get("JAVA_TOOL_OPTIONS", "") + " -Djava.awt.headless=true").strip()
    r = subprocess.run(cmd + ["-format", "frame:png", "-export", "frame", out_dir, swf_dir],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    return r.returncode


def save_group(task):
    pairs, crop = task
    datas = []
    for src, _ in pairs:
        with open(src, "rb") as f:
            datas.append(f.read())
    if crop:
        datas = crop_group(datas)
    for (_, dst), data in zip(pairs, datas):
        with open(dst, "wb") as f:
            f.write(data)
    return len(pairs)


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--build", default="1830")
    ap.add_argument("--out", default="den-out")
    ap.add_argument("--item", type=int, action="append", default=[])
    ap.add_argument("--ffdec")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-crop", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--batch", type=int, default=0)
    return ap


def main():
    run(make_parser().parse_args())


def render_batch(a, cmd, batch, swf_dir, png_dir, meta):
    """draw these items (base names) with ffdec and save the trimmed pictures; returns how many PNG frames were written"""
    work = tempfile.mkdtemp(prefix="den-", dir=a.out)
    try:
        jobs = max(1, min(a.jobs, len(batch)))
        chunks = []
        for k in range(jobs):
            d = os.path.join(work, "in%d" % k)
            os.makedirs(d)
            chunks.append((d, os.path.join(work, "out%d" % k)))
        for i, base in enumerate(batch):
            src = os.path.join(swf_dir, base + ".swf")
            dst = os.path.join(chunks[i % jobs][0], base + ".swf")
            with open(src, "rb") as f_in, open(dst, "wb") as f_out:
                f_out.write(strip_background(f_in.read()))
        with ThreadPoolExecutor(jobs) as ex:
            codes = list(ex.map(lambda c: render_chunk(cmd, c[0], c[1]), chunks))
        if any(codes):
            print("warning: ffdec exited with %s" % codes)
        tasks = []
        for _, outd in chunks:
            if not os.path.isdir(outd):
                continue
            for entry in sorted(os.listdir(outd)):
                base = entry[:-4] if entry.endswith(".swf") else entry
                frames = sorted((f for f in os.listdir(os.path.join(outd, entry)) if f.endswith(".png")),
                                key=lambda f: int(os.path.splitext(f)[0]))
                if base in meta:
                    meta[base]["frames"] = len(frames)
                pairs = []
                for n, fn in enumerate(frames, 1):
                    name = base + (".png" if len(frames) == 1 else "_f%02d.png" % n)
                    pairs.append((os.path.join(outd, entry, fn), os.path.join(png_dir, name)))
                tasks.append((pairs, not a.no_crop))
        with ProcessPoolExecutor(os.cpu_count() or 1) as ex:
            return sum(ex.map(save_group, tasks, chunksize=8))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_meta(a, meta):
    """meta.json is merged, not replaced: a run with --item must not wipe what earlier runs wrote down"""
    meta_path = os.path.join(a.out, "meta.json")
    try:
        with open(meta_path) as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    old.update(meta)
    tmp = meta_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(old, f, indent=1)
    os.replace(tmp, meta_path)


def run(a):
    """unwrap (and, unless a.no_render, render) den items. `a` is what make_parser() gives (asset_gui.py builds one itself).
    Several runs add to the same folder. With a.skip_existing, items that already have a picture are left alone, and with
    a.batch the work is done (and saved) that many items at a time, so an interrupted run can simply be started again.
    Returns {"swf": files written, "png": frames written, "ffdec": whether ffdec was found, "skipped": items already done}.
    Exits (SystemExit) with a message when the cache lacks what it needs."""
    done, cmd, skipped = 0, None, 0

    defp = os.path.join(a.cache, *M.hashed(a.build, "defPacks/1030").split("/"))
    if not os.path.exists(defp):
        sys.exit("missing %s\nget it with: python mirror_assets.py --types defs,den --out %s" % (defp, a.cache))
    rows = {int(r["id"]): r for r in M.read_def(defp).values()}
    root = os.path.join(a.cache, a.build, "items")
    if not os.path.isdir(root):
        sys.exit("no %s; get den items with: python mirror_assets.py --types den --out %s" % (root, a.cache))

    swf_dir, png_dir = os.path.join(a.out, "swf"), os.path.join(a.out, "png")
    os.makedirs(swf_dir, exist_ok=True)
    os.makedirs(png_dir, exist_ok=True)

    meta, todo = {}, []
    for did in sorted(rows):
        if a.item and did not in a.item:
            continue
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in rows[did].get("abbrName", "") or "")
        for cat in range(1, MAX_CAT + 1):
            p = os.path.join(root, M.hash_name(str(did << 16 | cat)))
            if not os.path.isfile(p):
                continue
            base = "%d_c%d_%s" % (did, cat, name) if name else "%d_c%d" % (did, cat)
            if a.skip_existing and (os.path.exists(os.path.join(png_dir, base + ".png")) or os.path.exists(os.path.join(png_dir, base + "_f01.png"))):
                skipped += 1
                continue
            try:
                o = M.AMF3(open(p, "rb").read()).value()
                swf = o["ba"]
            except (ValueError, IndexError, KeyError, TypeError) as e:
                print("skip item %d cat %d: %s" % (did, cat, e))
                continue
            with open(os.path.join(swf_dir, base + ".swf"), "wb") as f:
                f.write(swf)
            meta[base] = {"defId": did, "category": cat, "name": rows[did].get("abbrName"),
                          "cx": o.get("cx"), "cy": o.get("cy"), "so": o.get("so"), "r": o.get("r"),
                          "version": o.get("v")}
            todo.append(base)
            if a.limit and len(todo) >= a.limit:
                break
        if a.limit and len(todo) >= a.limit:
            break
    print("%d den item files to draw (%d already had pictures and were skipped); unwrapped to %s" % (len(todo), skipped, swf_dir), flush=True)

    if not a.no_render and todo:
        cmd = find_ffdec(a.ffdec)
        if not cmd:
            print("ffdec not found: the .swf files are written; install JPEXS ffdec or pass --ffdec to make PNGs")
        else:
            size = a.batch if a.batch and a.batch > 0 else len(todo)
            batches = [todo[i:i + size] for i in range(0, len(todo), size)]
            print("drawing with %s, %d process(es) at a time, in %d batch(es)" % (" ".join(cmd), max(1, min(a.jobs, size)), len(batches)), flush=True)
            started = time.time()
            for n, batch in enumerate(batches, 1):
                done += render_batch(a, cmd, batch, swf_dir, png_dir, meta)
                write_meta(a, {b: meta[b] for b in batch})  # saved after every batch: stopping now loses at most this one
                finished = sum(len(b) for b in batches[:n])
                rate = finished / max(1, time.time() - started)
                print("batch %d of %d done: %d of %d items, %d picture frames so far, about %d minute(s) left" % (
                    n, len(batches), finished, len(todo), done, round((len(todo) - finished) / max(rate, 0.01) / 60)), flush=True)
    write_meta(a, meta)
    return {"swf": len(todo), "png": done, "ffdec": bool(cmd), "skipped": skipped}


if __name__ == "__main__":
    main()
