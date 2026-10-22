#!/usr/bin/env python3
"""Composite cached AJ avatar animations into one PNG per animation frame.

Needs only three files in one folder (no server code, no Node):
  mirror_assets.py  decode_imagearray.py  composite_avatar.py
and a cache folder made by mirror_assets.py (or copied from the server's cache/):
  <cache>/1830/defPacks/*  <cache>/1830/paletteData  <cache>/1830/imageArrays/*

usage:
  python composite_avatar.py --list-avatars
  python composite_avatar.py --avatar 8                      every animation, default look
  python composite_avatar.py --avatar all                    every avatar
  python composite_avatar.py --avatar 8 --category 7         one animation
  python composite_avatar.py --avatar 8 --wear 2346 --wear 1500=ff0000,00ff00,0000ff,ffffff
  python composite_avatar.py --avatar 8 --hflip              mirrored (facing the other way)
  python composite_avatar.py --find-item 2346                what an item id is (layer, slot, avatars)
  python composite_avatar.py --icon 2346 --icon 1500         the dress-up/shop icons of items
  python composite_avatar.py --icon all                      icons of every item in the cache
                                                             (download with --types icons)

options: --cache DIR (default cache)  --build N (1830)  --out DIR (default avatar-out)
         --color SLOT=C1,C2,C3,C4 (recolour a slot)  --scale 2  --both-flips

RAW (--raw [gray|rainbow])
  python composite_avatar.py --avatar 8 --raw
  Writes OUT/avatar8_cat7_raw/frameNN.png, a false-colour stack where every layer slot has its own
  hue and the stored palette index is the brightness (so you can see which layer is which and
  where the shading steps are), and OUT/avatar8_cat7_raw/layers/slotS/frameNN.png: that
  slot's EXACT stored values as an indexed PNG (see decode_imagearray.py --raw), all on the same
  canvas so they line up pixel for pixel. The optional gray|rainbow picks how those layer files
  are drawn. Item colours, --wear colours and --color are ignored in this mode.

HOW A LOOK IS BUILT (what the game does, from LayerBitmap / LayerAnim)
  packId = avatar << 24 | category <<16 | layerDefId; the file is imageArrays/hashName(str(packId)).
  category = one animation (idle, walk, ...). layerDefId 0 is that animation's sequence file.
  defPack 1027 gives each layerDefId a slot 1..10; slots are stacked in order.
  Default look: the body layer (slot 1), the avatar's eyes (def 1003 baseEyesId) and pattern
  (basePatternId, if any). --wear ITEMID adds a clothing item (defPack 1000); an item takes the
  slot of its layer, replacing whatever the avatar had there.
  Colours: each slot has four indexes into the 256-colour game palette (paletteData).
  Slots 1-3 use the avatar's colorLayer1/2/3; a worn item uses its own "colors" value
  (the item's default colours: the game uses the colour the player picked). Override with
  --wear ITEMID=C1,C2,C3,C4 (RRGGBB hex, no #) or --color SLOT=....
  The mixing of semi-transparent pixels, shading (s) and flipping follow LayerBitmap.paintFrame
  at half resolution, then everything is scaled by --scale.

OUTPUT   OUT/avatar<A>_cat<C>[_wearN-N]/frameNN.png, cropped to what is drawn, + meta.json with
  each frame's top-left (x, y in full-resolution canvas pixels; the game adds a further
  375/365 AVATAR_BITMAP_OFFSET, recorded as "offset" but NOT applied) and the animation order.
ICONS  --icon ITEM|all writes OUT/icons/item<ID>_large.png and _small.png (imageArrays of avatar 1,
  categories 25 and 26, SimpleIcon), coloured with the item's default colours.
Not covered: den items, rooms and the media files (they are SWFs, not imageArrays).
"""
import argparse, json, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decode_imagearray as D
import mirror_assets as M

OFFSET = (375, 365)
MAX_CATEGORY = 64


# ------------------------------------------------------------------ data from the cache
class Data:
    def __init__(self, cache, build):
        self.cache, self.build = cache, build
        self.root = os.path.join(cache, build)
        self.items = {r["id"]: r for r in self.pack(1000).values()}
        self.avatars = {int(r["id"]): r for r in self.pack(1003).values()}
        self.layer_slot = {int(r["id"]): int(r["layerId"]) for r in self.pack(1027).values()}
        self.game = self.palette()

    def path(self, *parts):
        return os.path.join(self.cache, *M.hashed(self.build, "/".join(parts)).split("/"))

    def pack(self, did):
        p = self.path("defPacks", str(did))
        if not os.path.exists(p):
            what = {1000: "the item list", 1003: "the animal list", 1027: "the layer list", 10230: "the English names"}.get(did, "definition pack %s" % did)
            sys.exit("missing %s (defPack %s) in %s\nget it with: python mirror_assets.py --types defs --out %s\nor find out everything that is missing: python mirror_assets.py --doctor --out %s"
                     % (what, did, self.cache, self.cache, self.cache))
        try:
            return M.read_def(p)
        except Exception as e:
            sys.exit("defPack %s (%s) is damaged (%s): a download was probably cut off.\nDelete this file and download again: %s\nthen: python mirror_assets.py --types defs --out %s\nor run: python mirror_assets.py --doctor --out %s"
                     % (did, {1000: "items", 1003: "animals", 1027: "layers", 10230: "names"}.get(did, "definitions"), e, p, self.cache, self.cache))

    def palette(self):
        p = self.path("paletteData")
        if not os.path.exists(p):
            sys.exit("missing the colour palette (paletteData) in %s\nget it with: python mirror_assets.py --types defs --out %s\nor find out everything that is missing: python mirror_assets.py --doctor --out %s"
                     % (self.cache, self.cache, self.cache))
        raw = open(p, "rb").read()
        n = struct.unpack(">i", raw[:4])[0]
        return [struct.unpack(">I", raw[4 + i:8 + i])[0] & 0xFFFFFF for i in range(0, n, 4)]

    def image_file(self, avatar, category, layer):
        pid = M.pack_id(avatar, category, layer)
        p = os.path.join(self.root, "imageArrays", M.hash_name(str(pid)))
        return p if os.path.isfile(p) else None

    def body_layer(self):
        return next(i for i, s in sorted(self.layer_slot.items()) if s == 1)


def slot_palette(game, packed=None, colors=None):
    """100 RGBA entries: index = palette_index + 25 * t"""
    if colors is None:
        idx = [(packed >> s) & 0xFF for s in (24, 16, 8, 0)]
        colors = [((game[i] >> 16) & 255, (game[i] >> 8) & 255, game[i] & 255) for i in idx]
    base = D.build_animal_palette(*colors)
    return [base[v % 25] + (D.ALPHA[v // 25],) for v in range(100)]


def item_colors(row):
    """an item's "colors" is one packed colour, or several separated by commas (its choices): the first"""
    return int((row.get("colors") or "0").split(",")[0] or 0)


def parse_colors(text):
    cols = text.split(",")
    if len(cols) != 4:
        sys.exit("colours need four RRGGBB values separated by commas: %r" % text)
    return [D.parse_hex(c) for c in cols]


def build_look(data, avatar, wear, overrides):
    """-> [(slot, layerDefId, palette)] sorted by slot, and a list of notes"""
    av = data.avatars[avatar]
    notes, layers = [], {}

    def put(slot, layer, pal):
        layers[slot] = (slot, layer, pal)

    def av_pal(slot):
        return slot_palette(data.game, packed=int(av["colorLayer%d" % min(slot, 3)]))

    put(1, data.body_layer(), av_pal(1))
    for key in ("baseEyesId", "basePatternId"):
        iid = av.get(key, "0")
        if iid != "0" and iid in data.items:
            layer = int(data.items[iid]["layerDefId"])
            slot = data.layer_slot.get(layer)
            if slot:
                put(slot, layer, av_pal(slot))
    for spec in wear:
        iid, _, cols = spec.partition("=")
        row = data.items.get(iid)
        if not row:
            sys.exit("no item %s in defPack 1000 (see --find-item)" % iid)
        layer = int(row["layerDefId"])
        slot = data.layer_slot.get(layer)
        if not slot:
            sys.exit("item %s (layer %d) has no layer slot" % (iid, layer))
        if not int(row["avatarUseFlags"]) >> (avatar - 1) & 1:
            notes.append("item %s is not meant for avatar %d" % (iid, avatar))
        pal = slot_palette(data.game, colors=parse_colors(cols)) if cols else \
            slot_palette(data.game, packed=item_colors(row)) if slot > 3 else av_pal(slot)
        put(slot, layer, pal)
    for slot, cols in overrides.items():
        if slot in layers:
            layers[slot] = (slot, layers[slot][1], slot_palette(data.game, colors=cols))
    return [layers[s] for s in sorted(layers)], notes


# ------------------------------------------------------------------ raw mode
def raw_stack_palette(slot):
    """100 RGBA entries for the false-colour stack: one hue per layer slot, brightness = palette
    index, alpha = the stored transparency step. No avatar colours."""
    import colorsys
    pal = [(0, 0, 0, 0)] * 100
    hue = ((slot - 1) % 10) / 10.0
    for v in range(1, 100):
        idx, t = v % 25, v // 25
        if idx:
            r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 0.3 + 0.7 * idx / 24.0)
            pal[v] = (int(r * 255), int(g * 255), int(b * 255), D.ALPHA[t])
    return pal


def raw_layer_values(frames_by_slot, hflip):
    """each slot's stored pixel values on the shared canvas the composite uses (so they line up).
    returns (minx, miny, W, H, {slot: values}) in half-resolution units, or None if empty"""
    frs = [f for f in frames_by_slot.values() if f]
    if not frs:
        return None
    minx = min(f["x"] for f in frs)
    miny = min(f["y"] for f in frs)
    W = max(f["x"] + f["w"] for f in frs) - minx
    H = max(f["y"] + f["h"] for f in frs) - miny
    out = {}
    for slot, f in frames_by_slot.items():
        if not f:
            continue
        vals = [0] * (W * H)
        px = D.unrle_fit(f["b"], f["w"] * f["h"])
        ox, oy = f["x"] - minx, f["y"] - miny
        for i, v in enumerate(px):
            if v:
                vals[(oy + i // f["w"]) * W + ox + i % f["w"]] = v
        if hflip:
            vals = [vals[y * W + (W - 1 - x)] for y in range(H) for x in range(W)]
        out[slot] = vals
    if hflip:
        minx = 500 - (minx + W)
    return minx, miny, W, H, out


# ------------------------------------------------------------------ compositing
def layer_pixels(fr, shadow=True):
    w, h = fr["w"], fr["h"]
    px = D.unrle_fit(fr["b"], w * h)
    sh = {}
    if shadow and fr.get("s"):
        sm = int(fr.get("sm") or 3)
        for i, v in enumerate(D.unrle_fit(fr["s"], w * h)):
            if v:
                sh[i] = min(1.0, ((v << sm) + 24) / 255)
    return px, sh


def composite(layers, hflip):
    """layers: [(frame, palette)] bottom first -> (minx, miny, w, h, pixels) in half-res units"""
    frs = [(f, p) for f, p in layers if f]
    if not frs:
        return None
    minx = min(f["x"] for f, _ in frs)
    miny = min(f["y"] for f, _ in frs)
    W = max(f["x"] + f["w"] for f, _ in frs) - minx
    H = max(f["y"] + f["h"] for f, _ in frs) - miny
    canvas = [None] * (W * H)
    for f, pal in frs:
        px, sh = layer_pixels(f)
        ox, oy = f["x"] - minx, f["y"] - miny
        for i, v in enumerate(px):
            if v == 0 or v % 25 == 0 or v >= 100:
                continue
            x, y = ox + i % f["w"], oy + i // f["w"]
            r, g, b, a = pal[v]
            old = canvas[y * W + x]
            if a < 255 and old:
                orr, og, ob, oa = old
                if a == 192:
                    r, g, b = (3 * r + orr) // 4, (3 * g + og) // 4, (3 * b + ob) // 4
                elif a == 128:
                    r, g, b = (r + orr) // 2, (g + og) // 2, (b + ob) // 2
                else:
                    r, g, b = (r + 3 * orr) // 4, (g + 3 * og) // 4, (b + 3 * ob) // 4
                a = max(a, oa)
            canvas[y * W + x] = (r, g, b, a)
        for i, fac in sh.items():
            x, y = ox + i % f["w"], oy + i // f["w"]
            c = canvas[y * W + x]
            if c:
                canvas[y * W + x] = (int(c[0] * fac), int(c[1] * fac), int(c[2] * fac), c[3])
    if hflip:
        canvas = [canvas[y * W + (W - 1 - x)] for y in range(H) for x in range(W)]
        minx = 500 - (minx + W)
    return minx, miny, W, H, canvas


def to_png(W, H, canvas, scale):
    SW = W * scale
    out = bytearray(SW * H * scale * 4)
    for y in range(H):
        for x in range(W):
            p = canvas[y * W + x]
            if p is None:
                continue
            q = bytes(p)
            for dy in range(scale):
                o = ((y * scale + dy) * SW + x * scale) * 4
                for dx in range(scale):
                    out[o:o + 4] = q
                    o += 4
    return D.png_bytes(SW, H * scale, out)


# ------------------------------------------------------------------ run
def render_avatar(data, avatar, a, wear, overrides):
    look, notes = build_look(data, avatar, wear, overrides)
    if a.raw:
        look = [(slot, layer, raw_stack_palette(slot)) for slot, layer, _ in look]
    for n in notes:
        print("  note: " + n)
    tag = "_wear" + "-".join(w.partition("=")[0] for w in wear) if wear else ""
    if a.raw:
        tag += "_raw"
    cats = a.category or range(MAX_CATEGORY)
    flips = [False, True] if a.both_flips else [a.hflip]
    made = 0
    for cat in cats:
        files = [(slot, layer, pal, data.image_file(avatar, cat, layer)) for slot, layer, pal in look]
        files = [t for t in files if t[3]]
        if not files:
            continue
        objs = []
        for slot, layer, pal, path in files:
            try:
                objs.append((slot, pal, D.read_source(path, False)[1]["f"]))
            except (ValueError, OSError) as e:
                print("  cat %d layer %d: %s" % (cat, layer, e))
        if not objs:
            continue
        seq_path = data.image_file(avatar, cat, 0)
        seq = None
        if seq_path:
            try:
                seq = list(D.read_source(seq_path, False)[1]["f"])
            except (ValueError, OSError) as e:
                print("  cat %d sequence: %s" % (cat, e))
        steps = seq if seq else list(range(max(len(f) for _, _, f in objs)))
        for flip in flips:
            folder = os.path.join(a.out, "avatar%d_cat%d%s%s" % (avatar, cat, tag, "_flip" if flip else ""))
            os.makedirs(folder, exist_ok=True)
            meta = {"avatar": avatar, "category": cat, "hflip": flip, "scale": a.scale, "offset": OFFSET,
                    "wear": wear, "sequence": seq, "slots": [s for s, _, _ in objs], "frames": []}
            for n, idx in enumerate(steps):
                res = composite([(f[idx] if isinstance(idx, int) and idx < len(f) else None, pal)
                                 for _, pal, f in objs], flip)
                if res is None:
                    meta["frames"].append({"step": n, "image": idx, "empty": True})
                    continue
                x, y, W, H, canvas = res
                name = "frame%02d.png" % n
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(to_png(W, H, canvas, a.scale))
                if a.raw:  # every layer as its own exact indexed PNG on the same canvas
                    fbs = {slot: (f[idx] if isinstance(idx, int) and idx < len(f) else None) for slot, _, f in objs}
                    lv = raw_layer_values(fbs, flip)
                    if lv:
                        for slot, vals in lv[4].items():
                            ld = os.path.join(folder, "layers", "slot%d" % slot)
                            os.makedirs(ld, exist_ok=True)
                            with open(os.path.join(ld, name), "wb") as fh:
                                fh.write(D.indexed_png(lv[2], lv[3], vals, a.scale, D.raw_palette(a.raw)))
                meta["frames"].append({"step": n, "image": idx, "file": name, "x": x * 2, "y": y * 2,
                                       "w": W * a.scale, "h": H * a.scale})
            with open(os.path.join(folder, "meta.json"), "w") as fh:
                json.dump(meta, fh, indent=1)
            made += 1
            print("avatar %d cat %d%s: %d frames -> %s" % (avatar, cat, " (flipped)" if flip else "", len(steps), folder))
    if not made:
        print("avatar %d: no image files in %s (download with mirror_assets.py --types avatars --avatar %d)" % (
            avatar, data.root, avatar))


def render_icons(data, wanted, a):
    ids = sorted(data.items, key=int) if "all" in wanted else wanted
    folder = os.path.join(a.out, "icons")
    os.makedirs(folder, exist_ok=True)
    made = skipped = 0
    for iid in ids:
        row = data.items.get(iid)
        if not row:
            print("no item %s" % iid)
            continue
        layer = int(row["layerDefId"])
        if layer == 3:
            continue  # the body has no icon (Item.makeSmallIcon)
        pal = slot_palette(data.game, packed=item_colors(row))
        for cat, name in ((25, "large"), (26, "small")):
            path = data.image_file(1, cat, layer)
            if not path:
                skipped += 1
                continue
            try:
                frames = D.read_source(path, False)[1]["f"]
            except (ValueError, OSError) as e:
                print("icon %s %s: %s" % (iid, name, e))
                continue
            res = composite([(frames[0] if frames else None, pal)], a.hflip)
            if res is None:
                continue
            x, y, W, H, canvas = res
            with open(os.path.join(folder, "item%s_%s.png" % (iid, name)), "wb") as fh:
                fh.write(to_png(W, H, canvas, a.scale))
            made += 1
    print("icons: %d written to %s, %d not in the cache" % (made, folder, skipped))


# ------------------------------------------------------------------ library use (asset_gui.py)
def animation_categories(data, avatar):
    """[(category, steps)] for the animations of this avatar whose body file is in the cache"""
    out = []
    body = data.body_layer()
    for cat in range(MAX_CATEGORY):
        path = data.image_file(avatar, cat, body)
        if not path:
            continue
        seq = data.image_file(avatar, cat, 0)
        try:
            n = len(D.read_source(seq, False)[1]["f"]) if seq else len(D.read_source(path, False)[1]["f"])
        except (ValueError, OSError):
            continue
        out.append((cat, n))
    return out


def render_animation(data, avatar, cat, wear=(), overrides=None, hflip=False, scale=2, bg=None):
    """one animation on a shared canvas, in memory.
    wear: ["2346", "1500=C1,C2,C3,C4", ...]; overrides: {slot: [(r,g,b) x4]}; bg: (r,g,b) or None.
    returns {"w", "h" (pixels), "x", "y" (canvas position of the top-left, full-res), "steps",
    "frames": [RGBA bytes, w*h*4 each], "notes": [...]} or None when the files are missing"""
    look, notes = build_look(data, avatar, list(wear), overrides or {})
    objs = []
    for slot, layer, pal in look:
        path = data.image_file(avatar, cat, layer)
        if path:
            objs.append((slot, pal, D.read_source(path, False)[1]["f"]))
    if not objs:
        return None
    seq_path = data.image_file(avatar, cat, 0)
    seq = list(D.read_source(seq_path, False)[1]["f"]) if seq_path else None
    steps = seq if seq else list(range(max(len(f) for _, _, f in objs)))
    results = []
    for idx in steps:
        results.append(composite([(f[idx] if isinstance(idx, int) and idx < len(f) else None, pal)
                                  for _, pal, f in objs], hflip))
    live = [r for r in results if r]
    if not live:
        return None
    minx = min(r[0] for r in live)
    miny = min(r[1] for r in live)
    W = max(r[0] + r[2] for r in live) - minx
    H = max(r[1] + r[3] for r in live) - miny
    SW, SH = W * scale, H * scale
    frames = []
    for res in results:
        if bg:
            buf = bytearray(bytes(bg) + b"\xff") * (SW * SH)
        else:
            buf = bytearray(SW * SH * 4)
        if res:
            x0, y0, w, h, canvas = res
            ox, oy = (x0 - minx) * scale, (y0 - miny) * scale
            for y in range(h):
                for x in range(w):
                    p = canvas[y * w + x]
                    if p is None:
                        continue
                    r, g, b, a = p
                    for dy in range(scale):
                        o = ((oy + y * scale + dy) * SW + ox + x * scale) * 4
                        for dx in range(scale):
                            if bg and a < 255:
                                br, bgc, bb = buf[o], buf[o + 1], buf[o + 2]
                                buf[o:o + 4] = bytes(((r * a + br * (255 - a)) // 255, (g * a + bgc * (255 - a)) // 255,
                                                      (b * a + bb * (255 - a)) // 255, 255))
                            else:
                                buf[o:o + 4] = bytes((r, g, b, a))
                            o += 4
        frames.append(bytes(buf))
    return {"w": SW, "h": SH, "x": minx * 2, "y": miny * 2, "steps": steps, "frames": frames, "notes": notes}


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--avatar", action="append", default=[], help="avatar id, repeatable, or 'all'")
    ap.add_argument("--category", type=int, action="append", default=[])
    ap.add_argument("--wear", action="append", default=[], metavar="ITEM[=C1,C2,C3,C4]")
    ap.add_argument("--color", action="append", default=[], metavar="SLOT=C1,C2,C3,C4")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--build", default="1830")
    ap.add_argument("--out", default="avatar-out")
    ap.add_argument("--hflip", action="store_true")
    ap.add_argument("--raw", nargs="?", const="gray", choices=["gray", "rainbow"],
                    help="no avatar colours: false-colour stack + one exact indexed PNG per layer (see RAW)")
    ap.add_argument("--both-flips", action="store_true")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--list-avatars", action="store_true")
    ap.add_argument("--find-item", metavar="ITEM")
    ap.add_argument("--icon", action="append", default=[], metavar="ITEM|all")
    return ap


def main():
    a = make_parser().parse_args()

    data = Data(a.cache, a.build)
    if a.list_avatars:
        for i in sorted(data.avatars):
            have = sum(1 for c in range(MAX_CATEGORY) if data.image_file(i, c, data.body_layer()))
            print("avatar %2d: %d animations with a body file in the cache" % (i, have))
        return
    if a.find_item:
        row = data.items.get(a.find_item)
        if not row:
            sys.exit("no item %s" % a.find_item)
        layer = int(row["layerDefId"])
        who = [i for i in sorted(data.avatars) if int(row["avatarUseFlags"]) >> (i - 1) & 1]
        print("item %s: type %s, layerDefId %d, slot %s, default colours %s, avatars %s" % (
            a.find_item, row["type"], layer, data.layer_slot.get(layer), row["colors"], who))
        return
    if a.icon:
        render_icons(data, a.icon, a)
        if not a.avatar:
            return
    if not a.avatar:
        sys.exit("give --avatar N (or all); --list-avatars shows what the cache has")
    avs = sorted(data.avatars) if "all" in a.avatar else [int(x) for x in a.avatar]
    for av in avs:
        if av not in data.avatars:
            sys.exit("no avatar %d in defPack 1003" % av)
    overrides = {}
    for spec in a.color:
        slot, _, cols = spec.partition("=")
        overrides[int(slot)] = parse_colors(cols)
    for av in avs:
        render_avatar(data, av, a, a.wear, overrides)


if __name__ == "__main__":
    main()
