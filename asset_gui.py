#!/usr/bin/env python3
"""A local web page for the asset tools (standard library only; Pillow is used if installed).

  python asset_gui.py                 starts the page and opens your browser
  python asset_gui.py --cache cache --build 1830 --no-browser

What it gives you
  * one tab per script in this folder (mirror_assets, composite_avatar, decode_imagearray,
    extract_den_items, ...). The forms are BUILT FROM EACH SCRIPT'S OWN OPTIONS (its make_parser()),
    so a new or changed option shows up the next time the page is loaded. Any other .py file here
    with a make_parser() function gets a tab too.
  * a "GIF maker" tab: pick an avatar, animation, colours and clothing, watch the animation, and
    export it as GIF (Pillow if installed, otherwise a built-in encoder), APNG or a zip of PNGs.
    Built on composite_avatar.py, so it picks up changes to that script when the file changes.

Needs next to it: composite_avatar.py, decode_imagearray.py, mirror_assets.py, anim_encode.py.

Safety (the default, private mode): it listens on 127.0.0.1 only, and every request needs the random
token that is in the URL it prints (and a matching Host), so other web pages and other computers cannot
use it. It runs only the scripts it finds in this folder, with the arguments you set in the forms, as
normal programs (no shell).

PUBLIC MODE (--public) is for putting the GIF maker on the internet (see deploy/DEPLOY.md). It serves ONLY
the GIF maker: no script tabs, no running programs, no cache-folder changes, no server-side saves (saved
colours and outfits stay in each visitor's browser), no downloads from the game CDN (unless you add
--allow-download), a queue so a few visitors cannot use all the CPU, and a per-address rate limit. Put it
behind a reverse proxy that does HTTPS (Caddy / nginx) and listen on 127.0.0.1.

The page can also live somewhere else (GitHub Pages: see site/build.py and deploy/DEPLOY.md): give the
server that site's address with --cors-origin https://YOU.github.io. Browsers from that site, and only
that site, may then call the API; browser requests from any other website are refused.
"""
import argparse, base64, collections, importlib, importlib.util, io, json, os, re, secrets, shlex, subprocess, sys
import threading, time, traceback, urllib.parse, webbrowser, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(__file__)
sys.path.insert(0, HERE)

STATE = {"token": "", "cache": "cache", "build": "1830", "workdir": os.getcwd(), "port": 0, "explicit": False, "saves": os.path.join(HERE, "asset-gui-saves"),
         "den": "den-out", "public": False, "cors": [], "trust_proxy": False, "allow_download": False, "allowed_hosts": [], "max_scale": 6, "rate": 40}
LOCK = threading.RLock()
RENDER = threading.BoundedSemaphore(8)  # how many previews / exports may be drawn at once (--max-concurrent)


class Busy(Exception):
    """every render slot is taken"""


def heavy(fn, *args):
    if not RENDER.acquire(timeout=25):
        raise Busy("The server is busy drawing other animations. Try again in a moment.")
    try:
        return fn(*args)
    finally:
        RENDER.release()


# public mode: the only things a visitor may ask for
PUBLIC_GET = {"/", "/healthz", "/api/info", "/api/avatars", "/api/animations", "/api/palette", "/api/avatar-colors", "/api/items", "/api/parts",
              "/api/den", "/api/den-item", "/api/den-image"}
PUBLIC_POST = {"/api/preview", "/api/export", "/api/look"}
HEAVY_PATHS = {"/api/preview", "/api/export", "/api/animations"}
_hits = {}  # (address, kind) -> request times in the last minute


def rate_limited(addr, path):
    """True when this address has used up its requests for the last minute (heavy ones count against a small limit)"""
    now = time.time()
    with LOCK:
        if len(_hits) > 20000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 120]:
                del _hits[k]
        # den pictures are small static files and a page of them is dozens of requests: their own, roomier bucket
        kinds = (("img", STATE["rate"] * 60),) if path == "/api/den-image" else (("all", STATE["rate"] * 8), ("heavy", STATE["rate"]))
        for kind, limit in kinds:
            if kind == "heavy" and path not in HEAVY_PATHS:
                continue
            q = _hits.setdefault((addr, kind), collections.deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return True
            q.append(now)
    return False


# ------------------------------------------------------------------ script discovery (fresh each time)
def script_files():
    return sorted(f for f in os.listdir(HERE) if f.endswith(".py") and f != SELF and not f.startswith("_"))


def introspect(parser):
    import argparse as ap
    out = []
    for act in parser._actions:
        if isinstance(act, ap._HelpAction):
            continue
        d = {"dest": act.dest, "flags": list(act.option_strings), "help": (act.help or "").replace("%(default)s", str(act.default)),
             "choices": [str(c) for c in act.choices] if act.choices else None,
             "required": bool(getattr(act, "required", False)), "positional": not act.option_strings,
             "metavar": list(act.metavar) if isinstance(act.metavar, tuple) else act.metavar,
             "type": getattr(act.type, "__name__", "str") if act.type else "str", "nargs": act.nargs}
        default = act.default
        d["default"] = default if isinstance(default, (str, int, float, bool)) or default is None else None
        # options that are "the cache folder" / "the build" follow the page's setting when left blank
        if act.dest in ("cache", "out") and default == "cache" and act.option_strings:
            d["follows"] = "cache"
        elif act.dest == "build" and isinstance(default, str) and default.isdigit():
            d["follows"] = "build"
        if isinstance(act, (ap._StoreTrueAction, ap._StoreFalseAction)):
            d["kind"] = "flag"
        elif isinstance(act, ap._AppendAction):
            d["kind"] = "append"
        elif isinstance(act, ap._CountAction):
            d["kind"] = "count"
        elif act.nargs == "?":
            d["kind"] = "optional"
        elif act.nargs in ("+", "*") or (isinstance(act.nargs, int) and act.nargs > 1):
            d["kind"] = "list"
        else:
            d["kind"] = "value"
        out.append(d)
    return out


def dump_schema():
    """imports every script and describes its options; run in a fresh process so it is always current"""
    scripts = []
    for f in script_files():
        name = f[:-3]
        try:
            spec = importlib.util.spec_from_file_location("_probe_" + name, os.path.join(HERE, f))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "make_parser"):
                continue
            doc = (mod.__doc__ or "").strip()
            scripts.append({"name": name, "file": f, "doc": doc, "options": introspect(mod.make_parser())})
        except SystemExit:
            continue
        except Exception as e:  # a script that fails to import is shown, not hidden
            scripts.append({"name": name, "file": f, "doc": "", "options": [], "error": "%s: %s" % (type(e).__name__, e)})
    return {"scripts": scripts}


_schema_cache = {"key": None, "value": None}


def schema():
    key = tuple((f, os.path.getmtime(os.path.join(HERE, f))) for f in script_files())
    with LOCK:
        if _schema_cache["key"] != key:
            r = subprocess.run([sys.executable, os.path.join(HERE, SELF), "--dump-schema"], capture_output=True, text=True,
                               cwd=HERE, timeout=120)
            if r.returncode != 0:
                raise RuntimeError("could not read the scripts' options:\n" + r.stderr[-800:])
            _schema_cache.update(key=key, value=json.loads(r.stdout))
        return _schema_cache["value"]


def set_config(cache, build):
    """change the cache folder / build the page and the scripts use"""
    out = {}
    if cache is not None and str(cache).strip():
        path = os.path.abspath(os.path.expanduser(str(cache).strip()))
        if not os.path.isdir(path):
            raise ValueError("%s is not a folder" % path)
        STATE["cache"] = path
    if build is not None and str(build).strip():
        b = str(build).strip()
        if not b.isdigit():
            raise ValueError("the build is a number such as 1830")
        STATE["build"] = b
    with LOCK:
        _data.clear()
        _preview_cache.clear()
    root = os.path.join(STATE["cache"], STATE["build"])
    img = os.path.join(root, "imageArrays")
    out.update(cache=STATE["cache"], build=STATE["build"], hasBuild=os.path.isdir(root),
               hasDefs=os.path.isdir(os.path.join(root, "defPacks")),
               images=len(os.listdir(img)) if os.path.isdir(img) else 0)
    return out


def build_argv(opts, values):
    """command-line words for the form values; options first-class from the script's own schema"""
    pos, argv = [], []
    for o in opts:
        v = values.get(o["dest"])
        if (v is None or v == "") and o.get("follows"):
            v = STATE["cache"] if o["follows"] == "cache" else STATE["build"]
        if v is None or v == "" or v is False or v == []:
            continue
        flag = next((f for f in o["flags"] if f.startswith("--")), o["flags"][0] if o["flags"] else None)
        k = o["kind"]
        if o["positional"]:
            pos += shlex.split(str(v)) if k == "list" else [str(v)]
        elif k == "flag":
            argv.append(flag)
        elif k == "value":
            argv += [flag, str(v)]
        elif k == "optional":
            argv += [flag] if v == "__const__" else [flag, str(v)]
        elif k == "append":
            for line in str(v).splitlines():
                if line.strip():
                    argv += [flag, line.strip()]
        elif k == "list":
            argv += [flag] + shlex.split(str(v))
        elif k == "count":
            argv += [flag] * int(v)
    extra = values.get("__extra__")
    return pos + argv + (shlex.split(extra) if extra else [])


# ------------------------------------------------------------------ jobs
JOBS = {}


class Job:
    def __init__(self, script, argv):
        self.id = secrets.token_hex(6)
        self.cmd = " ".join(["python", script] + [shlex.quote(a) for a in argv])
        self.lines = collections.deque(maxlen=6000)
        self.base = 0  # how many lines have dropped off the front
        self.done, self.code = False, None
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, script)] + argv, cwd=STATE["workdir"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", bufsize=1, env=env)
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        for line in self.proc.stdout:
            with LOCK:
                if len(self.lines) == self.lines.maxlen:
                    self.base += 1
                self.lines.append(line.rstrip("\n"))
        self.code = self.proc.wait()
        self.done = True


# ------------------------------------------------------------------ GIF maker backend
_mods = {"mtimes": None, "C": None, "D": None, "M": None, "E": None}
_data = {}
_preview_cache = collections.OrderedDict()


def libs():
    """composite_avatar & friends, reloaded when their files change"""
    names = ["mirror_assets", "decode_imagearray", "composite_avatar", "anim_encode", "extract_den_items"]
    key = tuple(os.path.getmtime(os.path.join(HERE, n + ".py")) for n in names)
    with LOCK:
        if _mods["mtimes"] != key:
            loaded = []
            for n in names:
                m = sys.modules.get(n)
                loaded.append(importlib.reload(m) if m else importlib.import_module(n))
            _mods.update(mtimes=key, M=loaded[0], D=loaded[1], C=loaded[2], E=loaded[3], X=loaded[4])
            _data.clear()
            _preview_cache.clear()
        return _mods


def data():
    L = libs()
    key = (STATE["cache"], STATE["build"])
    with LOCK:
        if key not in _data:
            _data[key] = {"d": L["C"].Data(*key), "loc": None}
        return _data[key]


def hexof(rgb):
    return "%02x%02x%02x" % tuple(rgb)


def unhex(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def pal_hex(game, packed):
    return [hexof(((game[i] >> 16) & 255, (game[i] >> 8) & 255, game[i] & 255))
            for i in [(packed >> s) & 0xFF for s in (24, 16, 8, 0)]]


CUSTOM_LOW, CUSTOM_HIGH = 0xFD000000, 0xFDFFFFFF  # Burrow: a number here is the id of a server-made custom colour (lib/colours.js)


def cset(game, packed):
    """a Burrow colour code (four palette indexes in one number, first slot in the top byte)
    -> {"i": [4 indexes], "h": [4 hex colours]}"""
    idx = [(packed >> s) & 0xFF for s in (24, 16, 8, 0)]
    return {"i": idx, "h": pal_hex(game, packed)}


def clean_cset(x, game):
    """a colour set from the page / a share code / a save: a list of four hex colours (older
    versions), {"i": [4 palette indexes]} or {"h": [4 hex colours]} (a custom colour set).
    returns {"i": [...] or None, "h": [...]}; with indexes the hex colours come from the game palette."""
    if isinstance(x, list):
        x = {"h": x}
    if not isinstance(x, dict):
        raise ValueError("a colour set is four colours")
    if x.get("i") is not None:
        idx = x["i"]
        if not isinstance(idx, list) or len(idx) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(game) for v in idx):
            raise ValueError("a colour set is four palette numbers, each 0 to %d" % (len(game) - 1))
        if idx[0] == 0xFD:
            raise ValueError("palette number 253 in the first slot would be read as a custom-colour id; pick another colour for it")
        return {"i": idx, "h": [hexof(((game[v] >> 16) & 255, (game[v] >> 8) & 255, game[v] & 255)) for v in idx]}
    return {"i": None, "h": clean_colors(x.get("h"))}


def hexes(c):
    """the four hex colours of a colour set sent by the page (an object, or an older plain list)"""
    return c["h"] if isinstance(c, dict) else c


def names():
    h = data()
    if h["loc"] is None:
        d = h["d"]
        loc = d.pack(10230)
        h["loc"] = {str(k): v for k, v in loc.items() if isinstance(v, str)}
    return h["loc"]


_ttl = {}


def cached_for(key, seconds, make):
    now = time.time()
    with LOCK:
        hit = _ttl.get(key)
        if hit and now - hit[0] < seconds:
            return hit[1]
    value = make()
    with LOCK:
        _ttl[key] = (now, value)
    return value


def api_avatars():
    return cached_for(("avatars", STATE["cache"], STATE["build"]), 60, _api_avatars)


def _api_avatars():
    d, nm = data()["d"], names()
    out = []
    for aid in sorted(d.avatars):
        row = d.avatars[aid]
        n = sum(1 for c in range(libs()["C"].MAX_CATEGORY) if d.image_file(aid, c, d.body_layer()))
        out.append({"id": aid, "name": nm.get(row.get("titleStrRef"), "Avatar %d" % aid), "animations": n})
    return out


def api_avatar_colors(avatar):
    d = data()["d"]
    row = d.avatars[avatar]
    return {str(s): cset(d.game, int(row["colorLayer%d" % s])) for s in (1, 2, 3)}


def api_items(q, avatar, cat, limit):
    d, nm, C = data()["d"], names(), libs()["C"]
    words = [w for w in q.lower().split() if w]
    out = []
    for r in d.items.values():
        name = nm.get(r.get("titleStrId"), "")
        if not name or (words and not all(w in name.lower() for w in words)):
            continue
        layer = int(r["layerDefId"])
        slot = d.layer_slot.get(layer)
        if not slot or slot <= 3:  # 1 body, 2 pattern, 3 eyes: they have their own pickers
            continue
        fits = bool(int(r["avatarUseFlags"]) >> (avatar - 1) & 1)
        out.append({"id": r["id"], "name": name, "slot": slot, "layer": layer, "fits": fits,
                    "kind": "clothing" if r["type"] == "1" else "body part",
                    "has": bool(d.image_file(avatar, cat, layer)) if cat is not None else None,
                    "colors": cset(d.game, C.item_colors(r))})
    out.sort(key=lambda x: (not x["fits"], not (x["has"] or False), x["name"].lower()))
    return {"total": len(out), "items": out[:limit]}


DL_LOCK = threading.Lock()

HEX6 = __import__("re").compile(r"^[0-9a-fA-F]{6}$")


def clean_hex(c):
    c = str(c).strip().lstrip("#")
    if not HEX6.match(c):
        raise ValueError("%r is not a colour code (six hex digits like ff8800)" % (c,))
    return c.lower()


def clean_colors(cols):
    if not isinstance(cols, list) or len(cols) != 4:
        raise ValueError("a colour set is exactly four colour codes")
    return [clean_hex(c) for c in cols]


def validate_look(look):
    """a look from the page, a share code or a saved file -> a normalised dict, or ValueError"""
    if not isinstance(look, dict):
        raise ValueError("that is not an outfit")
    if look.get("v", 1) not in (1, 2):
        raise ValueError("this outfit was made by a newer version of the page")
    game = data()["d"].game
    def num(key, lo, hi, default=None):
        v = look.get(key, default)
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError("missing or bad %r" % key)
        if not lo <= v <= hi:
            raise ValueError("%r must be %d to %d" % (key, lo, hi))
        return v
    out = {"v": 2, "a": num("a", 1, 255), "c": num("c", 0, 63), "f": 1 if look.get("f") else 0, "s": num("s", 1, 6, 2),
           "d": num("d", 20, 2000, 80), "l": 0 if look.get("l") in (0, False) else 1,
           "b": clean_hex(look["b"]) if look.get("b") else None, "sl": {}, "w": []}
    if look.get("g") in ("gif", "apng", "zip"):
        out["g"] = look["g"]
    for slot, cols in (look.get("sl") or {}).items():
        if str(slot) not in ("1", "2", "3"):
            raise ValueError("colour slot %r does not exist" % slot)
        if cols:
            out["sl"][str(slot)] = clean_cset(cols, game)
    w = look.get("w") or []
    if not isinstance(w, list) or len(w) > 15:
        raise ValueError("an outfit holds at most 15 items (three in each of the five clothing places)")
    for it in w:
        iid = str(it.get("i") if isinstance(it, dict) else "")
        if not iid.isdigit() or len(iid) > 7:
            raise ValueError("bad item id %r" % iid)
        out["w"].append({"i": iid, "c": clean_cset(it["c"], game) if it.get("c") else None})
    return out


def resolve_look(look):
    """validate, then look the items up: names, slots, their own colours, whether they fit"""
    L = validate_look(look)
    d, nm, C = data()["d"], names(), libs()["C"]
    if L["a"] not in d.avatars:
        raise ValueError("avatar %d does not exist in this game data" % L["a"])
    items = []
    for it in L["w"]:
        row = d.items.get(it["i"])
        if not row:
            raise ValueError("item %s is not in this game data" % it["i"])
        layer = int(row["layerDefId"])
        items.append({"id": row["id"], "name": nm.get(row.get("titleStrId")) or "item " + row["id"], "slot": d.layer_slot.get(layer),
                      "colors": cset(d.game, C.item_colors(row)), "custom": it["c"],
                      "fits": bool(int(row["avatarUseFlags"]) >> (L["a"] - 1) & 1)})
    return {"look": L, "items": items}


def read_saves():
    try:
        with open(os.path.join(STATE["saves"], "saves.json"), encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        v = {}
    return {"colors": {n: c for n, c in (v.get("colors") or {}).items()} if isinstance(v.get("colors"), dict) else {},
            "outfits": v.get("outfits") if isinstance(v.get("outfits"), dict) else {}}


def change_saves(op, kind, name, value):
    if kind not in ("colors", "outfits"):
        raise ValueError("unknown kind")
    name = str(name or "").strip()
    if not name or len(name) > 60 or any(ord(ch) < 32 for ch in name):
        raise ValueError("give it a name of 1 to 60 characters")
    with LOCK:
        saves = read_saves()
        if op == "delete":
            saves[kind].pop(name, None)
        else:
            if len(saves[kind]) >= 500 and name not in saves[kind]:
                raise ValueError("too many saved items; delete some first")
            saves[kind][name] = clean_cset(value, data()["d"].game) if kind == "colors" else validate_look(value)
        os.makedirs(STATE["saves"], exist_ok=True)
        tmp = os.path.join(STATE["saves"], "saves.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(saves, f, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(STATE["saves"], "saves.json"))
        return saves


def image_key(av, cat, layer):
    M = libs()["M"]
    return "%s/imageArrays/%s" % (STATE["build"], M.hash_name(str(M.pack_id(av, cat, layer))))


def fetch_keys(keys):
    """download exactly these CDN file names (worked out from the defs, never from user text).
    Names that answer 404 are remembered in <cache>/.mirror/misses-gui.txt, so they are not asked again."""
    M = libs()["M"]
    m = M.Mirror(STATE["cache"], 8)
    with DL_LOCK:
        _, hits = m.run("gui", list(keys), label="page")
    return hits


def needed_layers(p):
    C = libs()["C"]
    av, cat, wear, ov, *_ = look_args(p)
    look, _ = C.build_look(data()["d"], av, wear, ov)
    return av, cat, [0] + [layer for _, layer, _ in look]


def ensure_files(p):
    """download whatever the requested look needs and is missing; returns notes for the page"""
    notes = []
    d = data()["d"]
    av, cat, layers = needed_layers(p)
    missing = [l for l in layers if not d.image_file(av, cat, l)]
    if not missing:
        return notes
    if p.get("auto") is False or (STATE["public"] and not STATE["allow_download"]):
        lost = [l for l in missing if l != 0]  # 0 is the animation-order file, which most animations do not have
        if lost:
            by_layer = {int(r["layerDefId"]): names().get(r.get("titleStrId")) or "item " + r["id"] for r in d.items.values()}
            notes.append("not downloaded here: " + ", ".join(sorted({by_layer.get(l, "layer %d" % l) for l in lost})))
        return notes
    keys = {image_key(av, cat, l): l for l in missing}
    hits = fetch_keys(keys)
    got = [l for k, l in keys.items() if k in hits]
    if got:
        notes.append("downloaded %d file%s" % (len(got), "" if len(got) == 1 else "s"))
    lost = [l for k, l in keys.items() if k not in hits and l != 0]
    if lost:
        by_layer = {int(r["layerDefId"]): names().get(r.get("titleStrId")) or "item " + r["id"] for r in d.items.values()}
        notes.append("not on the CDN for this animation: " + ", ".join(sorted({by_layer.get(l, "layer %d" % l) for l in lost})))
    return notes


def ensure_avatar(av):
    """an avatar with nothing downloaded: find its animations (the body in categories 0-63), then
    fetch its default look for each one. returns how many files arrived"""
    C = libs()["C"]
    d = data()["d"]
    body = d.body_layer()
    cats = {image_key(av, c, body): c for c in range(C.MAX_CATEGORY)}
    found = sorted(cats[k] for k in fetch_keys(cats))
    look, _ = C.build_look(d, av, [], {})
    layers = [0] + [layer for _, layer, _ in look if layer != body]
    keys = [image_key(av, c, l) for c in found for l in layers]
    fetch_keys(keys)
    return len(found)


def api_parts(avatar, cat):
    """the eye and pattern choices of an avatar: type 0 items in slot 3 (eyes) and 2 (pattern) it can use"""
    d, nm = data()["d"], names()
    row = d.avatars[avatar]
    out = {"eyes": [], "pattern": []}
    for r in d.items.values():
        if r["type"] != "0":
            continue
        layer = int(r["layerDefId"])
        slot = d.layer_slot.get(layer)
        kind = {3: "eyes", 2: "pattern"}.get(slot)
        if not kind or not int(r["avatarUseFlags"]) >> (avatar - 1) & 1:
            continue
        name = nm.get(r.get("titleStrId")) or ""
        out[kind].append({"id": r["id"], "layer": layer, "slot": slot,
                          "name": name if name and not name.isdigit() else "%s %s" % ("Eyes" if kind == "eyes" else "Pattern", r["id"]),
                          "default": r["id"] == row.get("baseEyesId" if kind == "eyes" else "basePatternId"),
                          "has": bool(d.image_file(avatar, cat, layer)) if cat is not None else None})
    for k in out:
        out[k].sort(key=lambda x: (not x["default"], int(x["id"])))
    return out


# ------------------------------------------------------------------ den items
_den = {"stamp": None, "pics": {}, "meta": {}}
DEN_PNG = re.compile(r"^(\d+)_c(\d+)_(.*?)(?:_f(\d+))?\.png$")
CURRENCY = {"0": "gems", "1": "tickets", "3": "diamonds"}


def den_pictures():
    """({(den id, category): [png file names, one per frame]}, meta.json) of the rendered pictures in --den, re-read when the folder changes"""
    pdir, mpath = os.path.join(STATE["den"], "png"), os.path.join(STATE["den"], "meta.json")
    try:
        stamp = (STATE["den"], os.stat(pdir).st_mtime, os.stat(mpath).st_mtime if os.path.exists(mpath) else 0)
    except OSError:
        stamp = (STATE["den"], None, None)
    with LOCK:
        if _den["stamp"] != stamp:
            pics = {}
            if stamp[1] is not None:
                found = collections.defaultdict(dict)
                for e in os.scandir(pdir):
                    m = DEN_PNG.match(e.name)
                    if m:
                        found[(int(m.group(1)), int(m.group(2)))][int(m.group(4) or 1)] = e.name
                pics = {k: [v[n] for n in sorted(v)] for k, v in found.items()}
            meta = {}
            try:
                with open(mpath) as f:
                    for m in json.load(f).values():
                        meta[(int(m.get("defId", 0)), int(m.get("category", 0)))] = m
            except (OSError, ValueError, TypeError):
                pass
            _den.update(stamp=stamp, pics=pics, meta=meta)
        return _den["pics"], _den["meta"]


def den_rows():
    h = data()
    if "den" not in h:
        d, nm = h["d"], names()
        rows = []
        for r in d.pack(1030).values():
            try:
                did = int(r["id"])
            except (KeyError, ValueError):
                continue
            rows.append({"id": did, "name": nm.get(r.get("nameStrId")) or r.get("abbrName") or "den item %d" % did,
                         "abbr": r.get("abbrName") or "", "cost": int(r.get("cost") or 0), "members": r.get("membersOnly") == "1",
                         "group": int(r.get("sortCat") or 0), "row": r})
        rows.sort(key=lambda x: x["id"])
        h["den"] = rows
    return h["den"]


def best_pic(pics, did):
    for c in range(1, 9):
        if (did, c) in pics:
            return {"cat": c, "frames": len(pics[(did, c)])}
    return None


def api_den(q, filt, sort, page, size):
    pics, _ = den_pictures()
    words = [w for w in q.lower().split() if w]
    out = []
    for r in den_rows():
        hay = (r["name"] + " " + r["abbr"]).lower()
        # a bare number means that exact id (or a name with that number in it, like "2012 banner"), not every id containing it
        if words and not all((w in hay) or (w.isdigit() and int(w) == r["id"]) for w in words):
            continue
        pic = best_pic(pics, r["id"])
        if (filt == "pic" and not pic) or (filt == "nopic" and pic) or (filt == "anim" and not (pic and pic["frames"] > 1)) \
                or (filt == "free" and r["cost"] != 0) or (filt == "members" and not r["members"]):
            continue
        out.append((r, pic))
    key = {"name": lambda t: t[0]["name"].lower(), "cost": lambda t: (t[0]["cost"], t[0]["id"])}.get(sort, lambda t: t[0]["id"])
    out.sort(key=key)
    size = max(12, min(120, size))
    pages = max(1, -(-len(out) // size))
    page = max(0, min(page, pages - 1))
    return {"total": len(out), "page": page, "pages": pages, "rendered": len({k[0] for k in pics}),
            "items": [{"id": r["id"], "name": r["name"], "abbr": r["abbr"], "cost": r["cost"], "members": r["members"], "pic": pic}
                      for r, pic in out[page * size:(page + 1) * size]]}


def api_den_item(did):
    rows = {r["id"]: r for r in den_rows()}
    if did not in rows:
        raise ValueError("no den item %d" % did)
    r = rows[did]
    pics, meta = den_pictures()
    M, build = libs()["M"], STATE["build"]
    row = r["row"]
    root = os.path.join(STATE["cache"], build)
    return {"id": did, "name": r["name"], "abbr": r["abbr"], "cost": r["cost"], "members": r["members"], "group": r["group"],
            "currency": CURRENCY.get(str(row.get("currencyType")), "currency " + str(row.get("currencyType"))),
            "type": row.get("typeCat"), "layer": row.get("layer"), "scale": row.get("scalePercent"), "enviro": row.get("enviroType"),
            "special": row.get("specialType"), "status": row.get("itemStatus"),
            "pictures": [{"cat": c, "frames": len(pics[(did, c)]), "meta": {k: meta.get((did, c), {}).get(k) for k in ("cx", "cy", "so", "r")}}
                         for c in range(1, 9) if (did, c) in pics],
            "inCache": [c for c in range(1, 9) if os.path.isfile(os.path.join(root, "items", M.hash_name(str(did << 16 | c))))] if not STATE["public"] else None,
            "music": bool(r["abbr"]) and os.path.isfile(os.path.join(root, *M.hashed(build, "audio/%s.mp3" % r["abbr"]).split("/")[1:])) if not STATE["public"] else None}


def den_image(did, cat, frame):
    pics, _ = den_pictures()
    files = pics.get((did, cat))
    if not files or not 1 <= frame <= len(files):
        return None
    with open(os.path.join(STATE["den"], "png", files[frame - 1]), "rb") as f:  # the name came from our own folder listing, never from the request
        return f.read()


def den_swf(did, cat):
    d = os.path.join(STATE["den"], "swf")
    prefix = "%d_c%d_" % (did, cat)
    names_ = sorted(n for n in (os.listdir(d) if os.path.isdir(d) else []) if n.startswith(prefix) and n.endswith(".swf"))
    if not names_:
        return None
    with open(os.path.join(d, names_[0]), "rb") as f:
        return f.read(), names_[0]


DEN_RENDER_LOCK = threading.Lock()


def den_render(did):
    """private mode: fetch a den item's files (as the GIF maker does for avatars), unwrap them and draw them with ffdec"""
    if STATE["public"]:
        raise ValueError("not available here")
    rows = {r["id"]: r for r in den_rows()}
    if did not in rows:
        raise ValueError("no den item %d" % did)
    L = libs()
    keys = {"%s/items/%s" % (STATE["build"], L["M"].hash_name(str(did << 16 | c))): c for c in range(1, 9)}
    fetch_keys(keys)
    from types import SimpleNamespace
    ns = SimpleNamespace(cache=STATE["cache"], build=STATE["build"], out=STATE["den"], item=[did], ffdec=None, jobs=1,
                         no_render=False, no_crop=False, limit=0)
    with DEN_RENDER_LOCK:
        try:
            r = L["X"].run(ns)
        except SystemExit as e:
            raise ValueError(str(e.code))
    if not r["swf"]:
        raise ValueError("the game has no picture file for this den item")
    if not r["ffdec"]:
        raise ValueError("ffdec (JPEXS) was not found, so the picture could not be drawn. Install it and Java, or run extract_den_items.py with --ffdec; the .swf was saved.")
    if not r["png"]:
        raise ValueError("ffdec ran but produced no picture")
    return {"ok": True, "frames": r["png"]}


def look_args(p):
    """request -> (avatar, category, wear specs, overrides, hflip, scale, bg)"""
    wear = []
    for w in p.get("wear", []):
        if w.get("colors"):
            wear.append("%s=%s" % (w["id"], ",".join(clean_hex(c) for c in hexes(w["colors"]))))
        else:
            wear.append(str(w["id"]))
    overrides = {int(s): [unhex(clean_hex(c)) for c in hexes(cols)] for s, cols in (p.get("slots") or {}).items() if cols}
    bg = unhex(p["bg"]) if p.get("bg") else None
    return int(p["avatar"]), int(p["cat"]), wear, overrides, bool(p.get("hflip")), max(1, min(STATE["max_scale"], int(p.get("scale", 2)))), bg


def render(p):
    d0 = data()["d"]
    av0, cat0, layers0 = needed_layers(p)
    have = [bool(d0.image_file(av0, cat0, l)) for l in layers0]  # a file arriving later must not hit a stale picture
    key = json.dumps({k: p.get(k) for k in ("avatar", "cat", "wear", "slots", "hflip", "scale", "bg")}, sort_keys=True) + repr(have)
    with LOCK:
        if key in _preview_cache:
            return _preview_cache[key]
    C = libs()["C"]
    av, cat, wear, ov, hflip, scale, bg = look_args(p)
    r = C.render_animation(data()["d"], av, cat, wear, ov, hflip, scale, bg)
    if r is None:
        raise ValueError("no image files for that avatar and animation in %s" % STATE["cache"])
    with LOCK:
        _preview_cache[key] = r
        while len(_preview_cache) > 6:
            _preview_cache.popitem(last=False)
    return r


def api_preview(p):
    dl = ensure_files(p)
    r = render(p)
    D = libs()["D"]
    return {"w": r["w"], "h": r["h"], "x": r["x"], "y": r["y"], "steps": r["steps"], "notes": dl + r["notes"],
            "frames": [base64.b64encode(D.png_bytes(r["w"], r["h"], f)).decode() for f in r["frames"]]}


def api_export(p):
    ensure_files(p)
    r = render(p)
    L = libs()
    fmt = p.get("format", "gif")
    delay = max(20, min(2000, int(p.get("delay", 80))))
    loops = 0 if p.get("loop", True) else 1
    base = "avatar%s_anim%s" % (p["avatar"], p["cat"])
    if fmt == "apng":
        return L["E"].apng_encode(r["frames"], r["w"], r["h"], delay, loops), "image/png", base + ".png"
    if fmt == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, f in enumerate(r["frames"]):
                z.writestr("%s_%02d.png" % (base, i), L["D"].png_bytes(r["w"], r["h"], f))
        return buf.getvalue(), "application/zip", base + "_frames.zip"
    use_pillow = {"auto": None, "off": False, "on": True}.get(p.get("pillow", "auto"))
    gif = L["E"].gif_encode(r["frames"], r["w"], r["h"], delay, loops, transparent=not p.get("bg"), pillow=use_pillow)
    return gif, "image/gif", base + ".gif"


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    server_version = "AssetGUI"

    def log_message(self, *a):
        pass

    def addr(self):
        """who is asking (behind a trusted reverse proxy: the address it saw)"""
        if STATE["trust_proxy"]:
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if fwd:
                return fwd
        return self.client_address[0]

    def log_request(self, code="-", size="-"):
        if STATE["public"] and str(code).isdigit() and int(code) >= 400:  # only trouble goes in the log
            sys.stderr.write("%s %s %s -> %s\n" % (self.addr(), self.command, self.path.split("?")[0], code))

    def gate(self, allowed):
        """public mode: only the allowed paths, and not too often. returns True if the request may go on"""
        url = urllib.parse.urlparse(self.path)
        if url.path not in allowed:
            self.send(404, json.dumps({"error": "not found"}))
            return False
        if url.path != "/healthz" and not self.origin_allowed():
            self.send(403, json.dumps({"error": "This site is not allowed to use this server."}))
            return False
        if url.path != "/healthz" and rate_limited(self.addr(), url.path):
            self.send(429, json.dumps({"error": "Too many requests. Wait a minute and try again."}), extra={"Retry-After": "30"})
            return False
        return True

    def origin_allowed(self):
        """public mode: a browser request carries the address of the site whose page made it (Origin).
        fine if there is none (curl, a visitor's own address bar), if it is this server's own page,
        or if it is one of --cors-origin. anything else is some other website using our API: refused."""
        o = self.headers.get("Origin")
        if not o or o in STATE["cors"]:
            return True
        return urllib.parse.urlparse(o).netloc.lower() == (self.headers.get("Host") or "").lower()

    def do_OPTIONS(self):
        """the browser's question before a cross-site POST: may this page do that? yes, for --cors-origin sites only"""
        o = self.headers.get("Origin")
        if not STATE["public"] or not self.ok() or not o or o not in STATE["cors"]:
            return self.send(403, "forbidden", "text/plain")
        path = urllib.parse.urlparse(self.path).path
        if path not in PUBLIC_GET and path not in PUBLIC_POST:
            return self.send(404, json.dumps({"error": "not found"}))
        if rate_limited(self.addr(), "/preflight"):
            return self.send(429, json.dumps({"error": "Too many requests."}), extra={"Retry-After": "30"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", o)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if STATE["public"]:
            return not STATE["allowed_hosts"] or host in STATE["allowed_hosts"]
        if host not in ("127.0.0.1", "localhost"):
            return False
        url = urllib.parse.urlparse(self.path)
        tok = self.headers.get("X-Token") or urllib.parse.parse_qs(url.query).get("t", [""])[0]
        return secrets.compare_digest(tok, STATE["token"])

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        o = self.headers.get("Origin")
        if STATE["public"] and o and o in STATE["cors"]:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the page gave up on this request (it cancels a preview when you change something again)

    def fail(self, e, code=500):
        if isinstance(e, Busy):
            return self.send(503, json.dumps({"error": str(e)}), extra={"Retry-After": "5"})
        if isinstance(e, SystemExit):  # the scripts report a missing file with sys.exit(message)
            return self.send(400, json.dumps({"error": str(e.code)}))
        if code == 500:  # 400s are the caller's mistake (a bad value); only real faults get a traceback
            sys.stderr.write(traceback.format_exc())
        self.send(code, json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":  # for the update script and monitors; says nothing about the machine
            return self.send(200, "ok", "text/plain")
        if not self.ok():
            return self.send(403, "forbidden", "text/plain")
        if STATE["public"] and not self.gate(PUBLIC_GET):
            return
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/healthz":
                return self.send(200, "ok", "text/plain")
            if url.path == "/":
                return self.send(200, PAGE.replace("__TOKEN__", STATE["token"]), "text/html; charset=utf-8")
            if url.path == "/api/info" and STATE["public"]:
                return self.send(200, json.dumps({"public": True, "build": STATE["build"], "pillow": libs()["E"].has_pillow(),
                                                  "download": STATE["allow_download"]}))
            if url.path == "/api/info":
                return self.send(200, json.dumps({"public": False,"workdir": STATE["workdir"], "cache": STATE["cache"], "build": STATE["build"],
                                                  "pillow": libs()["E"].has_pillow(), "python": sys.version.split()[0],
                                                  "explicit": STATE["explicit"], "saves": STATE["saves"]}))
            if url.path == "/api/schema":
                return self.send(200, json.dumps(schema()))
            if url.path == "/api/job":
                job = JOBS.get(q.get("id"))
                if not job:
                    return self.send(404, json.dumps({"error": "no such job"}))
                since = int(q.get("since", 0))
                with LOCK:
                    start = max(0, since - job.base)
                    lines = list(job.lines)[start:]
                    nxt = job.base + len(job.lines)
                return self.send(200, json.dumps({"lines": lines, "next": nxt, "done": job.done, "code": job.code, "cmd": job.cmd}))
            if url.path == "/api/saves":
                return self.send(200, json.dumps(read_saves()))
            if url.path == "/api/rescan":
                with LOCK:
                    _data.clear()
                    _preview_cache.clear()
                return self.send(200, "{}")
            if url.path == "/api/avatars":
                return self.send(200, json.dumps(api_avatars()))
            if url.path == "/api/animations":
                C = libs()["C"]
                av = int(q["avatar"])
                anims = cached_for(("anims", STATE["cache"], av), 30, lambda: C.animation_categories(data()["d"], av))
                if not anims and q.get("auto", "1") == "1" and (not STATE["public"] or STATE["allow_download"]):
                    ensure_avatar(av)
                    _ttl.clear()
                    anims = C.animation_categories(data()["d"], av)
                return self.send(200, json.dumps([{"cat": c, "steps": n} for c, n in anims]))
            if url.path == "/api/palette":
                g = data()["d"].game
                return self.send(200, json.dumps([hexof(((c >> 16) & 255, (c >> 8) & 255, c & 255)) for c in g]))
            if url.path == "/api/avatar-colors":
                return self.send(200, json.dumps(api_avatar_colors(int(q["avatar"]))))
            if url.path == "/api/den":
                return self.send(200, json.dumps(api_den(q.get("q", ""), q.get("filter", "all"), q.get("sort", "id"), int(q.get("page", 0)), int(q.get("size", 60)))))
            if url.path == "/api/den-item":
                return self.send(200, json.dumps(api_den_item(int(q["id"]))))
            if url.path == "/api/den-image":
                png = den_image(int(q["id"]), int(q.get("cat", 1)), int(q.get("frame", 1)))
                if png is None:
                    return self.send(404, json.dumps({"error": "no such picture"}))
                return self.send(200, png, "image/png", {"Cache-Control": "public, max-age=600"})
            if url.path == "/api/den-swf":  # private mode only: not in PUBLIC_GET
                got = den_swf(int(q["id"]), int(q.get("cat", 1)))
                if got is None:
                    return self.send(404, json.dumps({"error": "no such file"}))
                return self.send(200, got[0], "application/x-shockwave-flash", {"Content-Disposition": 'attachment; filename="%s"' % got[1]})
            if url.path == "/api/parts":
                cat = int(q["cat"]) if q.get("cat", "") != "" else None
                return self.send(200, json.dumps(api_parts(int(q["avatar"]), cat)))
            if url.path == "/api/items":
                cat = int(q["cat"]) if q.get("cat", "") != "" else None
                return self.send(200, json.dumps(api_items(q.get("q", ""), int(q["avatar"]), cat, int(q.get("limit", 40)))))
            return self.send(404, json.dumps({"error": "not found"}))
        except (Exception, SystemExit) as e:
            return self.fail(e, 400 if isinstance(e, (ValueError, KeyError)) else 500)

    def do_POST(self):
        if not self.ok():
            return self.send(403, "forbidden", "text/plain")
        if STATE["public"] and not self.gate(PUBLIC_POST):
            return
        url = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > (200_000 if STATE["public"] else 2_000_000):
                return self.send(413, json.dumps({"error": "too large"}))
            body = json.loads(self.rfile.read(n) or b"{}")
            if url.path in ("/api/run", "/api/argv"):
                sc = {s["name"]: s for s in schema()["scripts"]}
                s = sc.get(body.get("script"))
                if not s:
                    return self.send(404, json.dumps({"error": "unknown script"}))
                argv = build_argv(s["options"], body.get("values") or {})
                if url.path == "/api/argv":
                    return self.send(200, json.dumps({"cmd": " ".join(["python", s["file"]] + [shlex.quote(a) for a in argv])}))
                job = Job(s["file"], argv)
                JOBS[job.id] = job
                return self.send(200, json.dumps({"job": job.id, "cmd": job.cmd}))
            if url.path == "/api/config":
                return self.send(200, json.dumps(set_config(body.get("cache"), body.get("build"))))
            if url.path == "/api/saves":
                return self.send(200, json.dumps(change_saves(body.get("op"), body.get("kind"), body.get("name"), body.get("data"))))
            if url.path == "/api/look":
                return self.send(200, json.dumps(resolve_look(body.get("look"))))
            if url.path == "/api/den-render":  # private mode only: not in PUBLIC_POST
                return self.send(200, json.dumps(den_render(int(body.get("id")))))
            if url.path == "/api/stop":
                job = JOBS.get(body.get("job"))
                if job and not job.done:
                    job.proc.terminate()
                return self.send(200, "{}")
            if url.path == "/api/preview":
                return self.send(200, json.dumps(heavy(api_preview, body)))
            if url.path == "/api/export":
                data_, ctype, fname = heavy(api_export, body)
                return self.send(200, data_, ctype, {"Content-Disposition": 'attachment; filename="%s"' % fname})
            return self.send(404, json.dumps({"error": "not found"}))
        except (Exception, SystemExit) as e:
            return self.fail(e, 400 if isinstance(e, (ValueError, KeyError)) else 500)


def den_status():
    """one line for the start-up message: which folder the den pictures are read from, and how many there are"""
    try:
        n = sum(1 for f in os.listdir(os.path.join(STATE["den"], "png")) if f.endswith(".png"))
    except OSError:
        return "den pictures: %s  <- NOT FOUND: the Den items tab will show no pictures (extract_den_items.py --out THAT FOLDER fills it, or fix --den)" % STATE["den"]
    return "den pictures: %s (%d files)%s" % (STATE["den"], n, "" if n else "  <- EMPTY")


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default="cache", help="folder with the downloaded files (default ./cache)")
    ap.add_argument("--build", default="1830")
    ap.add_argument("--port", type=int, default=0, help="port to listen on (default: any free one)")
    ap.add_argument("--workdir", default=os.getcwd(), help="folder the scripts run in (default: where you started this)")
    ap.add_argument("--saves", default=os.path.join(HERE, "asset-gui-saves"),
                    help="folder for saved colours and outfits (default: asset-gui-saves next to this script)")
    ap.add_argument("--den", default="den-out", help="folder with the rendered den item pictures made by extract_den_items.py (default ./den-out)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page automatically")
    ap.add_argument("--public", action="store_true", help="internet-facing mode: GIF maker only, rate limits, no downloads, no saves on the server (see deploy/DEPLOY.md)")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1; with --public put a reverse proxy in front)")
    ap.add_argument("--allowed-host", action="append", default=[], help="public mode: only answer requests for this site name (repeatable)")
    ap.add_argument("--cors-origin", action="append", default=[], help="public mode: let the page hosted at this address call the API, e.g. https://YOU.github.io (repeatable; no path, no trailing slash)")
    ap.add_argument("--trust-proxy", action="store_true", help="public mode: take the visitor's address from X-Forwarded-For (only behind a proxy you control)")
    ap.add_argument("--allow-download", action="store_true", help="public mode: let visitors make the server fetch missing files from the game CDN (off by default)")
    ap.add_argument("--rate", type=int, default=40, help="public mode: previews/exports per visitor per minute (default 40)")
    ap.add_argument("--max-concurrent", type=int, default=2, help="public mode: animations drawn at the same time (default 2)")
    ap.add_argument("--max-scale", type=int, default=3, help="public mode: biggest size multiplier a visitor may ask for (default 3)")
    ap.add_argument("--dump-schema", action="store_true", help=argparse.SUPPRESS)
    return ap


def main():
    a = make_parser().parse_args()
    if a.dump_schema:
        print(json.dumps(dump_schema()))
        return
    STATE.update(token=secrets.token_urlsafe(18), cache=os.path.abspath(os.path.expanduser(a.cache)), build=a.build,
                 workdir=os.path.abspath(a.workdir), den=os.path.abspath(os.path.expanduser(a.den)), saves=os.path.abspath(os.path.expanduser(a.saves)), explicit="--cache" in sys.argv[1:] or any(x.startswith("--cache=") for x in sys.argv[1:]))
    global RENDER
    if a.public:
        STATE.update(public=True, token="", trust_proxy=a.trust_proxy, allow_download=a.allow_download,
                     allowed_hosts=[h.lower() for h in a.allowed_host], cors=[o.rstrip("/") for o in a.cors_origin], max_scale=max(1, min(6, a.max_scale)), rate=max(1, a.rate))
        RENDER = threading.BoundedSemaphore(max(1, a.max_concurrent))
    elif a.host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("--host %s would open the private tools (script runner, cache folder, saves) to the network.\n"
                 "Use --public for an internet-facing page, or leave --host alone for your own computer." % a.host)
    ThreadingHTTPServer.request_queue_size = 64  # the default of 5 refuses connections when a page opens many requests at once
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    if a.public:
        print("Asset tools PUBLIC page on http://%s:%d/  (cache %s, build %s)\n  downloads from the CDN: %s; %d draws at once; %d previews/exports per visitor per minute\n  pages allowed to call it from elsewhere: %s\n  %s\n  behind a reverse proxy? add --trust-proxy and --allowed-host YOUR.SITE"
              % (a.host, port, STATE["cache"], a.build, "ON" if a.allow_download else "off", max(1, a.max_concurrent), STATE["rate"],
                 ", ".join(STATE["cors"]) or "none", den_status()), flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        return
    url = "http://127.0.0.1:%d/?t=%s" % (port, STATE["token"])
    print("Asset tools page: %s\n  cache: %s (build %s)\n  scripts run in: %s\n  saved colours/outfits: %s\n  %s\n  Ctrl+C to stop"
          % (url, STATE["cache"], a.build, STATE["workdir"], STATE["saves"], den_status()))
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        for j in JOBS.values():
            if not j.done:
                j.proc.terminate()


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Asset tools</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--mute:#5d6672;--line:#d6dbe1;--acc:#2f6fed;--accink:#fff;--warn:#b25b00;--bad:#c2282f;--good:#1a7f45;--code:#101418;--codeink:#d7e2ee}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--panel:#1c2026;--ink:#e6e9ee;--mute:#99a3b0;--line:#323943;--acc:#6b9bff;--accink:#0b1426;--warn:#e0a35a;--bad:#ff7a80;--good:#4cc183}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:10px 18px;display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0}header small{color:var(--mute)}
.cfg{display:flex;gap:8px;align-items:center;flex-wrap:wrap;flex:1;min-width:300px}.cfg label{margin:0}.cfg input[type=text]{width:auto;flex:1;min-width:180px;max-width:460px}.cfg #cfgBuild{flex:none;width:70px;min-width:0}
nav{display:flex;gap:2px;padding:8px 18px 0;flex-wrap:wrap;background:var(--panel);border-bottom:1px solid var(--line)}
nav button{border:1px solid transparent;border-bottom:none;background:none;color:var(--mute);padding:7px 14px;border-radius:8px 8px 0 0;cursor:pointer;font:inherit}
nav button.on{background:var(--bg);color:var(--ink);border-color:var(--line);font-weight:600}
main{padding:16px 18px;max-width:1280px;margin:0 auto}
footer{padding:8px 18px 28px;text-align:center;color:var(--mute);font-size:12px}
section{display:none}section.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
h2{font-size:14px;margin:0 0 10px}
label{display:block;font-weight:600;font-size:12.5px;margin:0 0 3px}
.row{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px 16px}
.field small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
input[type=text],input[type=number],select,textarea{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:56px;resize:vertical}
.chk{display:flex;gap:8px;align-items:flex-start}.chk input{margin-top:3px}
button.btn{padding:7px 14px;border-radius:7px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer;font:inherit}
button.btn.pri{background:var(--acc);color:var(--accink);border-color:var(--acc);font-weight:600}
button.btn:disabled{opacity:.5;cursor:default}
pre.out{background:var(--code);color:var(--codeink);padding:10px;border-radius:8px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:8px 0 0;font:12.5px/1.4 ui-monospace,Consolas,monospace}
.cmd{font:12.5px ui-monospace,Consolas,monospace;color:var(--mute);word-break:break-all;margin:6px 0}
details summary{cursor:pointer;color:var(--mute)}details pre{white-space:pre-wrap;font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--mute)}
.err{color:var(--bad)}.ok{color:var(--good)}.mute{color:var(--mute)}
.gif{display:grid;grid-template-columns:minmax(300px,420px) 1fr;gap:16px}
@media (max-width:820px){.gif{grid-template-columns:1fr}}
.sw{display:inline-block;width:26px;height:26px;border-radius:6px;border:1px solid var(--line);cursor:pointer;vertical-align:middle;padding:0}
.swrow{display:flex;gap:6px;align-items:center;margin:4px 0}
.swrow span.n{width:78px;font-size:12.5px;color:var(--mute)}
.pop{position:absolute;z-index:20;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;box-shadow:0 8px 30px #0005}
.pal{display:grid;grid-template-columns:repeat(16,15px);gap:2px;margin-bottom:8px}
.pal i{width:15px;height:15px;border-radius:3px;cursor:pointer;border:1px solid #0002}
.list{max-height:230px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.list div.it{display:flex;gap:8px;align-items:center;padding:5px 8px;border-bottom:1px solid var(--line)}
.list div.it:last-child{border:none}.it .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--line);color:var(--mute)}
.tools{margin:2px 0 8px;padding:6px 8px;border:1px dashed var(--line);border-radius:8px}.tools input[type=text]{width:auto;flex:1;min-width:160px}.tools select{width:auto}
input.bad{border-color:var(--bad)!important}textarea.code{font:12px ui-monospace,Consolas,monospace;min-height:64px;word-break:break-all}
.stage{background-image:linear-gradient(45deg,#8883 25%,transparent 25%,transparent 75%,#8883 75%),linear-gradient(45deg,#8883 25%,transparent 25%,transparent 75%,#8883 75%);background-size:20px 20px;background-position:0 0,10px 10px;border:1px solid var(--line);border-radius:10px;min-height:240px;display:flex;align-items:center;justify-content:center;padding:10px;overflow:auto}
.stage canvas{max-width:100%;height:auto}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}

/* ---- compact layout, and small screens ---- */
[hidden]{display:none!important}
body{font-size:13px}
header{padding:6px 12px;gap:4px 10px}
nav{padding:6px 12px 0;flex-wrap:nowrap;overflow-x:auto}
nav button{white-space:nowrap;padding:5px 10px}
main{padding:10px 12px}
.card{padding:8px 10px;margin-bottom:8px;border-radius:8px}
label{margin:0 0 2px;font-size:12px}
input[type=text],input[type=number],select,textarea{padding:4px 6px}
button.btn{padding:4px 9px}
.row{grid-template-columns:repeat(auto-fill,minmax(min(100%,150px),1fr));gap:6px 10px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:6px 8px;margin-bottom:6px}
.row2 .field,.row .field{min-width:0}
.opts{display:flex;flex-wrap:wrap;gap:2px 14px;margin:2px 0 4px}
details.sect{padding:0}
details.sect>summary{cursor:pointer;font-weight:600;padding:7px 10px;list-style:none;display:flex;align-items:center;gap:6px;user-select:none}
details.sect>summary::-webkit-details-marker{display:none}
details.sect>summary::before{content:'\25B8';color:var(--mute);display:inline-block;width:10px;transition:transform .12s}
details.sect[open]>summary::before{transform:rotate(90deg)}
details.sect>.sbody{padding:0 10px 8px}
.toolbar{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
.toolbar .btn{padding:3px 8px;font-size:12px}
.gif{grid-template-columns:minmax(290px,370px) 1fr;gap:10px;align-items:start}
.gif>.right{position:sticky;top:8px}
.preview .stage{min-height:200px;padding:6px}
.preview .bar{margin-top:6px}
.exportbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
.exportbar>select{width:auto;flex:1 1 110px;min-width:0}
.exportbar #pillfield{flex:1 1 130px;min-width:0}
.exportbar #pillfield select{width:100%}
.swrow{gap:4px;margin:3px 0}
.swrow span.n{width:52px}
.sw{width:24px;height:24px}
.pop{width:min(330px,calc(100vw - 16px))}
.pal{grid-template-columns:repeat(16,1fr);gap:2px}
.pal i{width:auto;height:auto;aspect-ratio:1}
.list{max-height:180px}
textarea.code{min-height:48px}
/* den items */
.den{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,360px);gap:10px;align-items:start}
.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:6px;margin-bottom:8px}
.dcell{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;padding:4px;min-height:96px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);cursor:pointer;font:inherit;min-width:0}
.dcell.on,.dcell:hover{border-color:var(--acc)}
.dcell img{max-width:100%;max-height:64px;object-fit:contain}
.dcell span{font-size:11px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.noimg{color:var(--mute);font-size:11px;text-align:center;padding:14px 2px}.noimg.big{padding:60px 8px;font-size:13px}
.ddetail{position:sticky;top:8px;max-height:calc(100vh - 20px);overflow:auto}
.dview{display:flex;align-items:center;justify-content:center;min-height:140px;margin:8px 0;border:1px solid var(--line);border-radius:8px;padding:6px;background-image:linear-gradient(45deg,#8883 25%,transparent 25%,transparent 75%,#8883 75%),linear-gradient(45deg,#8883 25%,transparent 25%,transparent 75%,#8883 75%);background-size:20px 20px;background-position:0 0,10px 10px}
.dview img{max-width:100%;max-height:300px;object-fit:contain}
.dmeta{margin:6px 0}.drow{display:flex;justify-content:space-between;gap:10px;padding:2px 0;border-bottom:1px solid var(--line);font-size:12.5px}.drow .k{color:var(--mute)}
a.btn{display:inline-block;text-decoration:none;color:var(--ink);padding:4px 9px;border:1px solid var(--line);border-radius:7px;background:var(--panel)}
@media (max-width:760px){
  .den{grid-template-columns:1fr}
  .ddetail{position:fixed;left:0;right:0;bottom:0;top:auto;z-index:15;max-height:75vh;border-radius:14px 14px 0 0;margin:0;padding:10px 12px calc(12px + env(safe-area-inset-bottom,0px));box-shadow:0 -8px 30px #0005}
  .dgrid{grid-template-columns:repeat(auto-fill,minmax(84px,1fr))}
}
@media (max-width:760px){
  main{padding:8px 10px}
  .gif{grid-template-columns:1fr}
  .gif>.right{order:-1;top:0;z-index:5;margin:0 -10px;padding:0 10px;background:var(--bg)}   /* the picture stays in view while you change things */
  .preview .stage{min-height:110px;max-height:36vh;padding:2px}
  .stage canvas{max-height:33vh;width:auto;max-width:100%}
  input[type=text],input[type=number],select,textarea{font-size:16px}   /* stops phones zooming into a field you tap */
  button.btn,select,input[type=text],input[type=number]{min-height:38px}
  .sw{width:32px;height:32px}
  .swrow span.n{width:46px}
  .pop{position:fixed;left:0!important;right:0;bottom:0;top:auto!important;width:auto;max-height:70vh;overflow:auto;border-radius:14px 14px 0 0;padding:12px 12px calc(12px + env(safe-area-inset-bottom,0px))}
  .row{grid-template-columns:1fr 1fr}
  .list div.it{flex-wrap:wrap}
}
</style></head><body>
<header><h1>Asset tools</h1>
<div class="cfg"><label for="cfgCache">Cache folder</label><input type="text" id="cfgCache" placeholder="e.g. cache or D:\downloads" spellcheck="false">
<label for="cfgBuild">Build</label><input type="text" id="cfgBuild" size="5"><button class="btn pri" id="cfgUse">Use</button><span id="cfgMsg" class="mute"></span></div>
<small id="info">loading…</small></header>
<nav id="tabs"></nav>
<main id="main"></main>
<footer>slopped together by berrycon</footer>
<script>
const TOKEN="__TOKEN__";
const API=String(window.API_BASE||'').replace(/\/+$/,'');   // set by config.js on a static site; empty when the server serves the page itself
const $=(s,r=document)=>r.querySelector(s);
function el(tag,attrs={},...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){if(k==='class')e.className=v;else if(k==='text')e.textContent=v;else if(k.startsWith('on'))e.addEventListener(k.slice(2),v);else if(v!==false&&v!=null)e.setAttribute(k,v===true?'':v)}for(const kid of kids.flat()){if(kid==null)continue;e.append(kid.nodeType?kid:document.createTextNode(String(kid)))}return e}
async function api(path,opt={}){const r=await fetch(API+path,{...opt,headers:{...(TOKEN?{'X-Token':TOKEN}:{}),...(opt.body?{'Content-Type':'application/json'}:{}),...(opt.headers||{})}});if(!r.ok){let m='';try{m=(await r.json()).error}catch(e){m=await r.text()}throw new Error(m||('HTTP '+r.status))}return r}
const getJ=async p=>(await api(p)).json();
const postJ=async(p,b,signal)=>(await api(p,{method:'POST',body:JSON.stringify(b),signal}));
const store={get(k,d){try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const debounce=(f,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>f(...a),ms)}};

/* ---------- cache folder / build (shared by every tab) ---------- */
const CONF={cache:'',build:''};
const followText=k=>'follows the page: '+(k==='cache'?CONF.cache:CONF.build);
function applyConf(c){CONF.cache=c.cache;CONF.build=c.build;$('#cfgCache').value=c.cache;$('#cfgBuild').value=c.build;
  document.querySelectorAll('[data-follows]').forEach(i=>{i.placeholder=followText(i.dataset.follows)});window.dispatchEvent(new Event('confchange'))}
async function useConf(quiet){const m=$('#cfgMsg');m.className='mute';m.textContent='checking…';
  try{const c=await(await postJ('/api/config',{cache:$('#cfgCache').value,build:$('#cfgBuild').value})).json();applyConf(c);store.set('conf',{cache:c.cache,build:c.build});
    if(!c.hasBuild){m.className='err';m.textContent='No "'+c.build+'" folder in there — use the folder that CONTAINS '+c.build+'.'}
    else if(!c.hasDefs){m.className='err';m.textContent='Found the build folder but no definitions yet. Run mirror_assets with --types defs.'}
    else{m.className='ok';m.textContent=c.images.toLocaleString()+' image files found.'}
    if(window.gifReload)window.gifReload()}catch(e){m.className='err';m.textContent=String(e.message||e)}}
$('#cfgUse').onclick=()=>useConf();$('#cfgCache').addEventListener('keydown',e=>{if(e.key==='Enter')useConf()});$('#cfgBuild').addEventListener('keydown',e=>{if(e.key==='Enter')useConf()});

/* ---------- saved colours / share codes ---------- */
let SAVES={colors:{},outfits:{}};
window.PUBLIC=!!API;window.PUBLIC_DL=false;   // a page hosted elsewhere is by definition talking to a public server
const localSaves=()=>{const v=store.get('saves',{});return{colors:v.colors||{},outfits:v.outfits||{}}};
const loadSaves=async()=>{if(window.PUBLIC){SAVES=localSaves();return}try{SAVES=await getJ('/api/saves')}catch(e){}};
async function savesPost(body){   // private mode: the server's saves file; public mode: this browser only
  if(!window.PUBLIC)return(await postJ('/api/saves',body)).json();
  const n=String(body.name||'').trim();if(!n||n.length>60)throw new Error('Give it a name of 1 to 60 characters.');if(!['colors','outfits'].includes(body.kind))throw new Error('unknown kind');
  const sv=localSaves();if(body.op==='delete')delete sv[body.kind][n];else{if(Object.keys(sv[body.kind]).length>=200&&!(n in sv[body.kind]))throw new Error('Too many saved items; delete some first.');sv[body.kind][n]=body.data}
  store.set('saves',sv);return sv}
const parseCodes=t=>{const out=[];t=String(t).replace(/rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})[^)]*\)/gi,(_,r,g,b)=>' '+[r,g,b].map(n=>Math.min(255,+n).toString(16).padStart(2,'0')).join('')+' ');for(const m of t.matchAll(/#?\b([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b/g)){let h=m[1].toLowerCase();if(h.length===3)h=h.split('').map(c=>c+c).join('');out.push(h)}
  if(!out.length)return null;while(out.length<4)out.push(out[out.length-1]);return out.slice(0,4)};
const b64e=s=>btoa(String.fromCharCode(...new TextEncoder().encode(s))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
const b64d=s=>{s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';return new TextDecoder().decode(Uint8Array.from(atob(s),c=>c.charCodeAt(0)))};
const decodeShare=t=>{t=String(t).trim().replace(/\s+/g,'');if(t.startsWith('{'))return JSON.parse(t);if(!t.startsWith('ACT1.'))throw new Error('That does not look like a share code (it should start with ACT1.)');return JSON.parse(b64d(t.slice(5)))};

/* ---------- tabs ---------- */
const sections={};let tabsEl=$('#tabs'),mainEl=$('#main');
function addTab(id,title,node){const b=el('button',{text:title,onclick:()=>show(id)});tabsEl.append(b);const s=el('section',{},node);mainEl.append(s);sections[id]={b,s};return s}
function show(id){for(const[k,v]of Object.entries(sections)){v.b.classList.toggle('on',k===id);v.s.classList.toggle('on',k===id)}store.set('tab',id)}

/* ---------- script forms (built from each script's own options) ---------- */
function scriptTab(sc){
  const key='vals:'+sc.name;let vals=store.get(key,{});
  const root=el('div');
  const docParts=(sc.doc||'').split('\n\n');
  root.append(el('div',{class:'card'},el('h2',{text:sc.file}),el('div',{class:'mute',text:docParts[0]||''}),sc.doc?el('details',{},el('summary',{text:'Full help text'}),el('pre',{text:sc.doc})):null,sc.error?el('div',{class:'err',text:sc.error}):null));
  if(sc.error)return root;
  const form=el('div',{class:'row'});const cmd=el('div',{class:'cmd'});const out=el('pre',{class:'out',hidden:true});
  const upd=debounce(async()=>{try{const r=await(await postJ('/api/argv',{script:sc.name,values:vals})).json();cmd.textContent=r.cmd}catch(e){cmd.textContent=String(e)}},150);
  const set=(d,v)=>{vals[d]=v;store.set(key,vals);upd()};
  const metaOf=o=>Array.isArray(o.metavar)?o.metavar.join(' '):(o.metavar||o.dest.toUpperCase());
  for(const o of sc.options){
    const name=o.positional?o.dest:o.flags.join(', ');let ctl;const v=vals[o.dest];
    if(o.kind==='flag'){ctl=el('div',{class:'chk'},el('input',{type:'checkbox',checked:!!v,onchange:e=>set(o.dest,e.target.checked)}),el('div',{},el('b',{text:name}),o.help?el('small',{text:o.help}):null));form.append(el('div',{class:'field'},ctl));continue}
    if(o.choices&&(o.kind==='value')){ctl=el('select',{onchange:e=>set(o.dest,e.target.value)},el('option',{value:'',text:o.default!=null&&o.default!==''?'(default: '+o.default+')':'(not set)'}),o.choices.map(c=>el('option',{value:c,text:c,selected:v===c})))}
    else if(o.kind==='optional'){ctl=el('select',{onchange:e=>set(o.dest,e.target.value)},el('option',{value:'',text:'(off)'}),el('option',{value:'__const__',text:'on (default style)',selected:v==='__const__'}),(o.choices||[]).map(c=>el('option',{value:c,text:c,selected:v===c})))}
    else if(o.kind==='append'){ctl=el('textarea',{placeholder:'one per line  ('+metaOf(o)+')',oninput:e=>set(o.dest,e.target.value)});ctl.value=v||''}
    else if(o.kind==='list'){ctl=el('input',{type:'text',placeholder:metaOf(o)+'  (separate with spaces)',oninput:e=>set(o.dest,e.target.value)});ctl.value=v||''}
    else if(o.kind==='count'){ctl=el('input',{type:'number',min:0,oninput:e=>set(o.dest,e.target.value)});ctl.value=v||''}
    else{ctl=el('input',{type:o.type==='int'?'number':'text',placeholder:o.follows?followText(o.follows):(o.default!=null&&o.default!==''?'default: '+o.default:metaOf(o)),oninput:e=>set(o.dest,e.target.value)});ctl.value=v||'';if(o.follows){ctl.dataset.follows=o.follows}}
    form.append(el('div',{class:'field'},el('label',{text:name+(o.required?' *':'')}),ctl,o.help?el('small',{text:o.help}):null));
  }
  const extra=el('input',{type:'text',placeholder:'extra command-line arguments, e.g. --has avatar 8 item 276',oninput:e=>set('__extra__',e.target.value)});extra.value=vals.__extra__||'';
  form.append(el('div',{class:'field',style:'grid-column:1/-1'},el('label',{text:'Extra arguments (raw)'}),extra,el('small',{text:'Anything the form does not cover. Quote values that contain spaces.'})));
  window.addEventListener('confchange',upd);
  const run=el('button',{class:'btn pri',text:'Run'}),stop=el('button',{class:'btn',text:'Stop',disabled:true}),status=el('span',{class:'mute'});let job=null,timer=null;
  run.onclick=async()=>{out.hidden=false;out.textContent='';status.textContent='running…';run.disabled=true;stop.disabled=false;
    try{const r=await(await postJ('/api/run',{script:sc.name,values:vals})).json();job=r.job;cmd.textContent=r.cmd;let since=0;
      const poll=async()=>{try{const j=await getJ('/api/job?id='+job+'&since='+since);const near=out.scrollHeight-out.scrollTop-out.clientHeight<40;if(j.lines.length){out.textContent+=j.lines.join('\n')+'\n';since=j.next;if(near)out.scrollTop=out.scrollHeight}
        if(j.done){status.textContent=j.code===0?'finished':'exited with code '+j.code;run.disabled=false;stop.disabled=true;return}}catch(e){status.textContent=String(e);run.disabled=false;stop.disabled=true;return}timer=setTimeout(poll,400)};poll()}
    catch(e){status.textContent=String(e);run.disabled=false;stop.disabled=true}};
  stop.onclick=async()=>{if(job)await postJ('/api/stop',{job});status.textContent='stopping…'};
  root.append(el('div',{class:'card'},form,cmd,el('div',{class:'bar'},run,stop,status),out));upd();return root}

/* ---------- GIF maker ---------- */
/* collapsible sections, open/closed state remembered in this browser */
const OPEN=store.get('open',{});
const SLOTNAME={4:'tail',5:'legs',6:'back',7:'neck',8:'head'};
const narrow=()=>!!(window.matchMedia&&window.matchMedia('(max-width:760px)').matches);
function sect(key,title,openDefault,...kids){const d=el('details',{class:'card sect'},el('summary',{text:title}),el('div',{class:'sbody'},...kids));
  d.open=key in OPEN?!!OPEN[key]:openDefault;d.addEventListener('toggle',()=>{OPEN[key]=d.open;store.set('open',OPEN)});return d}

function gifTab(){
  const S=store.get('gif',{avatar:null,cat:null,hflip:false,scale:2,delay:80,loop:true,bgMode:'transparent',bg:'#ffffff',format:'gif',slots:{},wear:[]});
  const save=()=>{store.set('gif',S);track()};let defaults={},palette=[],anims=[],frames=[],cur=0,timer=null,ctl=null,seq=0;
  const root=el('div',{class:'gif'});const left=el('div',{class:'left'}),right=el('div',{class:'right'});root.append(left,right);
  const avSel=el('select'),animSel=el('select'),msg=el('div',{class:'mute'}),note=el('div',{class:'mute'});
  const keepChk=el('input',{type:'checkbox'}),autoChk=el('input',{type:'checkbox'}),flip=el('input',{type:'checkbox'}),scaleSel=el('select',{},[1,2,3,4].map(n=>el('option',{value:n,text:n+'×'+(n===2?' (game size)':'')})));
  const delay=el('input',{type:'number',min:20,max:2000,step:10}),loop=el('input',{type:'checkbox'});
  const bgSel=el('select',{},el('option',{value:'transparent',text:'transparent'}),el('option',{value:'color',text:'solid colour'})),bgCol=el('input',{type:'color'});
  const fmt=el('select',{},el('option',{value:'gif',text:'GIF'}),el('option',{value:'apng',text:'APNG (full quality)'}),el('option',{value:'zip',text:'PNG frames (zip)'}));
  const pill=el('select',{},el('option',{value:'auto',text:'encoder: auto'}),el('option',{value:'off',text:'encoder: built-in'}));
  let parts={eyes:[],pattern:[]};const patSel=el('select'),eyesSel=el('select');
  const field=(label,ctl)=>el('div',{class:'field'},el('label',{text:label}),ctl);
  const opt=(ctl,text,title)=>el('label',{class:'chk',style:'font-weight:400;margin:0',title},ctl,text);
  left.append(sect('look','Avatar',true,
    el('div',{class:'row2'},field('Avatar',avSel),field('Animation',animSel)),
    el('div',{class:'row2'},field('Pattern',patSel),field('Eyes',eyesSel)),
    el('div',{class:'opts'},opt(flip,'face the other way'),
      opt(autoChk,'download missing files automatically','Files that are not on this computer yet are fetched from the game CDN when you pick them.'),
      opt(keepChk,'keep clothes and colours when changing avatar','When you pick another avatar, keep the clothing you added and the colours you changed (items the new avatar cannot wear are left out).')),
    msg,note));
  const colBody=el('div'),colCard=sect('colours','Colours',true,colBody);left.append(colCard);
  const itemBody=el('div'),itemCard=sect('clothing','Clothing',true,itemBody);left.append(itemCard);
  /* pattern and eyes: items of type 0 in layer slots 2 and 3 that this avatar can use */
  async function loadParts(){if(S.avatar==null)return;try{parts=await getJ('/api/parts?avatar='+S.avatar+'&cat='+(S.cat??''))}catch(e){return}drawParts()}
  function drawParts(){for(const[kind,sel,slot]of[['pattern',patSel,2],['eyes',eyesSel,3]]){const have=S.wear.find(w=>w.slot===slot),def=parts[kind].find(p=>p.default);
      sel.replaceChildren(el('option',{value:'',text:'(default'+(def?': '+def.name:'')+')'}),...parts[kind].filter(p=>!p.default).map(p=>el('option',{value:p.id,text:p.name+(p.has===false&&S.auto!==false?' (downloads)':'')})));
      sel.value=have?have.id:'';if(sel.value!==(have?have.id:''))sel.value=''}}
  for(const[kind,sel,slot]of[['pattern',patSel,2],['eyes',eyesSel,3]])sel.onchange=()=>{S.wear=S.wear.filter(w=>w.slot!==slot);
    if(sel.value){const p=parts[kind].find(x=>x.id===sel.value);if(p)S.wear.push({id:p.id,name:p.name,slot,def:null,colors:null})}save();drawWorn();previewD()};
  const outCard=sect('format','Size, speed and background',!narrow(),el('div',{class:'row'},
    field('Size',scaleSel),field('Frame time (ms)',delay),
    el('div',{class:'field'},el('label',{text:'Background'}),bgSel,bgCol),el('div',{class:'field'},el('label',{class:'chk',style:'font-weight:400;margin-top:18px'},loop,'loop forever'))));
  left.append(outCard);
  const shareBody=el('div'),shareCard=sect('share','Save and share',false,shareBody);left.append(shareCard);
  const codeBox=el('textarea',{class:'code',readonly:true,spellcheck:'false',title:'copy this to share the whole look'}),loadBox=el('textarea',{class:'code',placeholder:'paste a share code here',spellcheck:'false'});
  const outName=el('input',{type:'text',placeholder:'name for this outfit',size:16}),outList=el('div',{class:'list',hidden:true});
  const lookNow=()=>({v:2,a:S.avatar,c:S.cat,f:S.hflip?1:0,s:S.scale,d:+delay.value||80,l:S.loop?1:0,b:S.bgMode==='color'?S.bg.slice(1):null,sl:Object.fromEntries(Object.entries(S.slots).map(([k,c])=>[k,wire(c)])),w:S.wear.map(w=>({i:w.id,c:w.colors?wire(w.colors):null})),g:fmt.value});
  const refreshCode=()=>{codeBox.value=S.avatar==null||S.cat==null?'':'ACT1.'+b64e(JSON.stringify(lookNow()))};
  async function applyLook(look){const r=await(await postJ('/api/look',{look})).json(),L=r.look;
    S.avatar=L.a;S.cat=L.c;S.hflip=!!L.f;S.scale=L.s;S.delay=L.d;S.loop=!!L.l;S.bgMode=L.b?'color':'transparent';if(L.b)S.bg='#'+L.b;S.slots=Object.fromEntries(Object.entries(L.sl||{}).map(([k,c])=>[k,norm(c)]));
    S.wear=r.items.map(it=>({id:it.id,name:it.name,slot:it.slot,def:norm(it.colors),colors:it.custom?norm(it.custom):null}));
    avSel.value=S.avatar;flip.checked=S.hflip;scaleSel.value=S.scale;delay.value=S.delay;loop.checked=S.loop;bgSel.value=S.bgMode;bgCol.value=S.bg;bgCol.style.display=S.bgMode==='color'?'':'none';if(L.g)fmt.value=L.g;fmt.onchange();
    save();await loadAvatar(true);const bad=r.items.filter(i=>!i.fits).map(i=>i.name);if(bad.length)flash('Loaded. Not made for this avatar: '+bad.join(', '),true)}
  const colList=el('div',{class:'list',hidden:true});
  function drawSavedColors(){const names=Object.keys(SAVES.colors).sort();colList.hidden=!names.length;colList.replaceChildren();
    for(const n of names){colList.append(el('div',{class:'it'},el('span',{class:'nm',text:n,title:n}),
      el('span',{style:'display:inline-flex;gap:4px'},norm(SAVES.colors[n]).h.map(h=>el('i',{style:'width:18px;height:18px;border-radius:4px;border:1px solid var(--line);background:#'+h,title:'#'+h}))),
      el('button',{class:'btn',text:'copy code',onclick:async()=>{const c=norm(SAVES.colors[n]);const t=c.i?String(packOf(c)):c.h.join(' ');try{await navigator.clipboard.writeText(t);flash('Copied '+t)}catch(e){flash(t)}}}),
      el('button',{class:'btn',text:'delete',onclick:async()=>{try{SAVES=await savesPost({op:'delete',kind:'colors',name:n});drawSavedColors();drawColors();drawWorn()}catch(e){flash(String(e.message||e),true)}}})))}}
  function drawOutfits(){drawSavedColors();const names=Object.keys(SAVES.outfits).sort();outList.hidden=!names.length;outList.replaceChildren();
    for(const n of names){outList.append(el('div',{class:'it'},el('span',{class:'nm',text:n,title:n}),
      el('button',{class:'btn',text:'load',onclick:async()=>{try{await applyLook(SAVES.outfits[n]);flash('Loaded "'+n+'".')}catch(e){flash(String(e.message||e),true)}}}),
      el('button',{class:'btn',text:'delete',onclick:async()=>{try{SAVES=await savesPost({op:'delete',kind:'outfits',name:n});drawOutfits();drawColors();drawWorn()}catch(e){flash(String(e.message||e),true)}}})))}}
  shareBody.append(
    el('label',{text:'Share code (copy this to send the whole look to someone)'}),codeBox,
    el('div',{class:'bar'},el('button',{class:'btn',text:'copy code',onclick:async()=>{refreshCode();try{await navigator.clipboard.writeText(codeBox.value);flash('Copied.')}catch(e){codeBox.select();document.execCommand('copy');flash('Copied.')}}})),
    el('label',{text:'Load a look from a code',style:'margin-top:10px'}),loadBox,
    el('div',{class:'bar'},el('button',{class:'btn',text:'load code',onclick:async()=>{try{await applyLook(decodeShare(loadBox.value));flash('Loaded.')}catch(e){flash(String(e.message||e),true)}}})),
    el('label',{text:'Saved colour sets (use them from "codes…" on any colour row)',style:'margin-top:10px'}),colList,
    el('label',{text:'Saved outfits (kept on this computer)',style:'margin-top:10px'}),
    el('div',{class:'bar',style:'margin-top:0'},outName,el('button',{class:'btn',text:'save this look',onclick:async()=>{const n=outName.value.trim();if(!n){flash('Give the outfit a name first.',true);return}
      try{SAVES=await savesPost({op:'put',kind:'outfits',name:n,data:lookNow()});outName.value='';drawOutfits();flash('Saved "'+n+'".')}catch(e){flash(String(e.message||e),true)}}})),outList);

  /* palette popup */
  let pop=null;const closePop=()=>{if(pop){pop.remove();pop=null}};document.addEventListener('mousedown',e=>{if(pop&&!pop.contains(e.target)&&!e.target.classList.contains('sw'))closePop()});
  /* ---- colour sets: a Burrow colour code (four palette numbers packed into one number) or custom RGB ---- */
  const CUSTOM_LO=0xFD000000,CUSTOM_HI=0xFDFFFFFF;   // Burrow: a number in this range is the id of a server-made custom colour
  const cs=i=>({i:[...i],h:i.map(k=>palette[k])});
  const csHex=h=>{const ix=h.map(x=>palette.indexOf(x));return ix.every(k=>k>=0)&&ix[0]!==253?{i:ix,h:[...h]}:{i:null,h:[...h]}};
  const norm=x=>{if(!x)return null;if(Array.isArray(x))return csHex(x.map(t=>String(t).replace('#','').toLowerCase()));if(x.i&&x.i.length===4)return cs(x.i);return csHex((x.h||[]).map(t=>String(t).toLowerCase()))};
  const packOf=c=>c.i?((c.i[0]*256+c.i[1])*256+c.i[2])*256+c.i[3]:null;
  const unpack=n=>[Math.floor(n/16777216)%256,Math.floor(n/65536)%256,Math.floor(n/256)%256,n%256];
  const same=(a,b)=>!!a.i===!!b.i&&JSON.stringify(a.i||a.h)===JSON.stringify(b.i||b.h);
  const wire=c=>c.i?{i:c.i}:{h:c.h};
  const rgbNums=c=>c.h.map(h=>parseInt(h,16));
  const nearest=hex=>{const r=parseInt(hex.substr(0,2),16),g=parseInt(hex.substr(2,2),16),b=parseInt(hex.substr(4,2),16);let best=0,bd=1e9;
    palette.forEach((p,k)=>{if(k===253)return;const d=(r-parseInt(p.substr(0,2),16))**2+(g-parseInt(p.substr(2,2),16))**2+(b-parseInt(p.substr(4,2),16))**2;if(d<bd){bd=d;best=k}});return best};
  function pick(anchor,onPick){closePop();const r=anchor.getBoundingClientRect();pop=el('div',{class:'pop'});
    pop.append(el('div',{class:'mute',text:'Game palette: tap a colour (hover shows its number)',style:'margin-bottom:6px'}),el('div',{class:'pal'},palette.map((h,k)=>el('i',{style:'background:#'+h,title:'#'+k+' · '+h,onclick:()=>{onPick({idx:k});closePop()}}))));
    const ci=el('input',{type:'color',oninput:e=>{onPick({hex:e.target.value.slice(1)})}});pop.append(el('div',{class:'bar',style:'margin:0'},el('span',{class:'mute',text:'or a custom RGB colour:'}),ci));document.body.append(pop);
    if(!narrow()){const vw=document.documentElement.clientWidth,pw=pop.offsetWidth;pop.style.left=Math.max(8,Math.min(window.scrollX+r.left,window.scrollX+vw-pw-8))+'px';pop.style.top=(window.scrollY+r.bottom+6)+'px'}}
  const swatches=(c,onSet)=>el('span',{style:'display:inline-flex;gap:5px'},c.h.map((h,k)=>{const b=el('button',{class:'sw',style:'background:#'+h,title:(c.i?'palette #'+c.i[k]:'custom')+' · #'+h});
    b.onclick=()=>pick(b,ch=>{const h2=[...c.h];
      if(ch.idx!==undefined){if(k===0&&ch.idx===253){flash("Palette colour 253 in the first slot would be read as one of Burrow's custom-colour ids. Pick another.",true);return}
        h2[k]=palette[ch.idx];if(c.i){const i2=[...c.i];i2[k]=ch.idx;onSet({i:i2,h:h2})}else onSet(csHex(h2))}
      else{h2[k]=ch.hex;onSet({i:null,h:h2})}});return b}));

  const openTools=new Set();
  function flash(t,bad){note.textContent='';note.append(el('span',{class:bad?'err':'ok',text:t}))}
  async function saveColors(name,c){name=(name||'').trim();if(!name){flash('Give the colours a name first.',true);return}
    try{SAVES=await savesPost({op:'put',kind:'colors',name,data:wire(c)});flash('Saved "'+name+'".');drawColors();drawWorn();drawSavedColors()}catch(e){flash(String(e.message||e),true)}}
  function colorRow(key,lead,c,onSet,extras){
    const toggle=el('button',{class:'btn',text:'codes…',title:'Burrow colour code, palette numbers or hex codes: type, paste, copy, save, load'});
    const panel=el('div',{class:'tools',hidden:!openTools.has(key)});
    toggle.onclick=()=>{openTools.has(key)?openTools.delete(key):openTools.add(key);panel.hidden=!openTools.has(key)};
    const bad=(inp,on)=>inp.classList.toggle('bad',!!on);
    const code=packOf(c);
    const state=c.i?el('span',{class:'ok',text:'Burrow code '+code}):el('span',{class:'err',text:'custom RGB (not a palette colour): '+rgbNums(c).join(',')});
    const copy=el('button',{class:'btn',text:c.i?'copy code':'copy RGB numbers',onclick:async()=>{const t=c.i?String(code):rgbNums(c).join(',');try{await navigator.clipboard.writeText(t);flash('Copied '+t)}catch(e){flash(t)}}});
    const codeIn=el('input',{type:'text',spellcheck:'false',size:12,title:'one number: the four palette numbers packed together, the first slot in the top byte'});codeIn.value=c.i?String(code):'';
    const applyCode=()=>{const t=codeIn.value.trim();if(!/^\d+$/.test(t)||Number(t)>4294967295){bad(codeIn,true);return}const n=Number(t);
      if(n>=CUSTOM_LO&&n<=CUSTOM_HI){bad(codeIn,true);flash('That number is a Burrow custom-colour id: the server keeps its four colours, so they cannot be read from the number. Type the colours as hex codes instead.',true);return}
      bad(codeIn,false);const nc=cs(unpack(n));if(!same(nc,c))onSet(nc)};
    codeIn.addEventListener('keydown',e=>{if(e.key==='Enter')applyCode()});codeIn.addEventListener('change',applyCode);
    const idxIn=el('input',{type:'text',spellcheck:'false',size:16,title:'four palette numbers, 0 to 255, first slot first'});idxIn.value=c.i?c.i.join(' '):'';
    const applyIdx=()=>{const v=(idxIn.value.match(/\d+/g)||[]).map(Number);if(v.length!==4||v.some(k=>k>=palette.length)||v[0]===253){bad(idxIn,true);return}bad(idxIn,false);const nc=cs(v);if(!same(nc,c))onSet(nc)};
    idxIn.addEventListener('keydown',e=>{if(e.key==='Enter')applyIdx()});idxIn.addEventListener('change',applyIdx);
    const codes=el('input',{type:'text',spellcheck:'false',title:'four hex colours, e.g. ff8800 00aa44 112233 ffffff. Exact game colours become a Burrow code; anything else is a custom RGB set.'});codes.value=c.h.join(' ');
    const applyHex=()=>{const l=parseCodes(codes.value);if(!l){bad(codes,true);return}bad(codes,false);const nc=csHex(l);if(!same(nc,c))onSet(nc)};
    codes.addEventListener('keydown',e=>{if(e.key==='Enter')applyHex()});codes.addEventListener('change',applyHex);
    const snap=c.i?null:el('button',{class:'btn',text:'snap to game palette',title:'use the closest game colours: gives a normal Burrow code',onclick:()=>onSet(cs(c.h.map(nearest)))});
    const nm=el('input',{type:'text',placeholder:'name to save as',size:14});
    const saved=el('select',{},el('option',{value:'',text:'use saved colours…'}),Object.keys(SAVES.colors).sort().map(n=>el('option',{value:n,text:n})));
    saved.onchange=()=>{const e=SAVES.colors[saved.value];if(e)onSet(norm(e))};
    panel.append(el('div',{class:'bar',style:'margin:2px 0 6px'},state,copy,snap),
      el('div',{class:'bar',style:'margin:2px 0 6px'},el('span',{class:'mute',text:'Burrow code'}),codeIn,el('span',{class:'mute',text:'palette numbers'}),idxIn),
      el('div',{class:'bar',style:'margin:2px 0 6px'},el('span',{class:'mute',text:'hex codes'}),codes),
      el('div',{class:'bar',style:'margin:2px 0'},saved,nm,el('button',{class:'btn',text:'save these',onclick:()=>saveColors(nm.value,c)})));
    return el('div',{},el('div',{class:'swrow',style:'flex-wrap:wrap'},lead,swatches(c,onSet),extras,toggle),panel)}
  function drawColors(){colBody.replaceChildren();
    for(const[s,label]of[['1','Body'],['2','Pattern'],['3','Eyes']]){const cols=S.slots[s]||defaults[s];if(!cols)continue;
      colBody.append(colorRow('slot'+s,el('span',{class:'n',text:label}),cols,n=>{S.slots[s]=n;save();drawColors();previewD()},
        S.slots[s]?el('button',{class:'btn',text:'reset',onclick:()=>{delete S.slots[s];save();drawColors();previewD()}}):null))}
    colBody.append(el('div',{class:'mute',text:'Four colours per part. "codes…" lets you paste, copy and save them.'}))}

  let searchSeq=0;const q=el('input',{type:'text',placeholder:'search clothing by name, e.g. fox hat'}),res=el('div',{class:'list',hidden:true}),worn=el('div');
  async function search(){const my=++searchSeq;if(!q.value.trim()||S.avatar==null){res.hidden=true;return}try{const r=await getJ('/api/items?q='+encodeURIComponent(q.value)+'&avatar='+S.avatar+'&cat='+(S.cat??'')+'&limit=40');if(my!==searchSeq)return;res.hidden=false;res.replaceChildren();
    if(!r.items.length)res.append(el('div',{class:'it mute',text:'No matches.'}));
    for(const it of r.items){const have=S.wear.some(w=>w.id===it.id);res.append(el('div',{class:'it'},el('span',{class:'nm',text:it.name,title:it.name}),el('span',{class:'tag',text:'#'+it.id}),
      !it.fits?el('span',{class:'tag',text:'not for this avatar'}):it.has===false?el('span',{class:'tag',text:S.auto!==false?'downloads when added':'not downloaded'}):null,
      el('button',{class:'btn',text:have?'added':'add',disabled:have||!it.fits,onclick:()=>{const same=S.wear.filter(w=>w.slot===it.slot);let off=null;   // up to three per place, as in Burrow's client; a 4th pushes the oldest off
          if(same.length>=3){off=same[0];S.wear=S.wear.filter(w=>w!==off)}
          S.wear.push({id:it.id,name:it.name,slot:it.slot,def:norm(it.colors),colors:null});save();drawWorn();search();preview();
          if(off)flash('Up to 3 items per place: '+off.name+' came off to make room.')}})))}
    if(r.total>r.items.length)res.append(el('div',{class:'it mute',text:r.total-r.items.length+' more — narrow the search'}))}catch(e){res.hidden=false;res.textContent=String(e)}}
  q.oninput=debounce(search,250);
  function drawWorn(){worn.replaceChildren();drawParts();const clothes=S.wear.filter(w=>w.slot>3);if(!clothes.length){worn.append(el('div',{class:'mute',text:'Nothing added. Up to 3 items per place (head, neck, back, legs, tail); a 4th pushes the oldest off. Later ones are drawn over earlier ones.',style:'margin-top:6px'}));return}
    for(const w of clothes){worn.append(colorRow('item'+w.id,el('span',{style:'display:inline-flex;gap:6px;align-items:center;flex:1;min-width:130px'},el('b',{text:w.name}),el('span',{class:'tag',text:SLOTNAME[w.slot]||'slot '+w.slot})),w.colors||w.def,n=>{w.colors=n;save();drawWorn();previewD()},
      [w.colors?el('button',{class:'btn',text:'reset',onclick:()=>{w.colors=null;save();drawWorn();previewD()}}):null,el('button',{class:'btn',text:'remove',onclick:()=>{S.wear=S.wear.filter(x=>x!==w);save();drawWorn();search();previewD()}})]))}}
  itemBody.append(q,res,worn);

  /* preview */
  const canvas=el('canvas'),info=el('div',{class:'mute'}),stage=el('div',{class:'stage'},canvas);const playBtn=el('button',{class:'btn',text:'pause'});let playing=true;
  playBtn.onclick=()=>{playing=!playing;playBtn.textContent=playing?'pause':'play';if(playing)tick()};
  const expMsg=el('span',{class:'mute'}),expBtn=el('button',{class:'btn pri',text:'Export'});
  const hideBtn=el('button',{class:'btn',text:'hide',title:'hide the picture to see more of the controls',onclick:e=>{stage.hidden=!stage.hidden;e.target.textContent=stage.hidden?'show':'hide'}});
  right.append(el('div',{class:'card preview'},stage,el('div',{class:'bar'},playBtn,hideBtn,info),
    el('div',{class:'exportbar'},fmt,el('span',{id:'pillfield'},pill),expBtn,expMsg)));
  function tick(){clearTimeout(timer);if(!frames.length||!playing)return;const f=frames[cur%frames.length];canvas.width=f.width;canvas.height=f.height;const c=canvas.getContext('2d');c.clearRect(0,0,f.width,f.height);c.drawImage(f,0,0);cur++;timer=setTimeout(tick,Math.max(20,+delay.value||80))}
  function request(extra){return{avatar:S.avatar,cat:S.cat,hflip:S.hflip,scale:S.scale,bg:S.bgMode==='color'?S.bg.slice(1):null,slots:S.slots,wear:S.wear.map(w=>({id:w.id,colors:w.colors})),auto:S.auto!==false,...(extra||{})}}
  async function preview(){refreshCode();if(S.avatar==null||S.cat==null)return;if(ctl)ctl.abort();ctl=new AbortController();const my=++seq;msg.textContent=S.auto!==false?'drawing… (fetching anything that is missing)':'drawing…';
    try{const r=await(await postJ('/api/preview',request(),ctl.signal)).json();if(my!==seq)return;
      frames=await Promise.all(r.frames.map(b=>new Promise((ok,no)=>{const i=new Image();i.onload=()=>ok(i);i.onerror=no;i.src='data:image/png;base64,'+b})));cur=0;
      info.textContent=r.w+'×'+r.h+' px · '+frames.length+' frames'+(r.notes&&r.notes.length?' · '+r.notes.join('; '):'');msg.textContent='';tick()}
    catch(e){if(e.name==='AbortError')return;frames=[];clearTimeout(timer);const c=canvas.getContext('2d');c.clearRect(0,0,canvas.width,canvas.height);info.textContent='';msg.innerHTML='';msg.append(el('span',{class:'err',text:String(e.message||e)}))}}
  const previewD=debounce(preview,200);
  expBtn.onclick=async()=>{expMsg.textContent='encoding…';expBtn.disabled=true;try{const r=await postJ('/api/export',request({format:fmt.value,delay:+delay.value||80,loop:S.loop,pillow:pill.value}));const b=await r.blob();
      const ext={gif:'gif',apng:'png',zip:'zip'}[fmt.value];const a=el('a',{href:URL.createObjectURL(b),download:'avatar'+S.avatar+'_anim'+S.cat+(fmt.value==='zip'?'_frames':'')+'.'+ext});document.body.append(a);a.click();a.remove();expMsg.textContent='saved ('+Math.round(b.size/1024)+' KB)'}catch(e){expMsg.textContent='';expMsg.append(el('span',{class:'err',text:String(e.message||e)}))}expBtn.disabled=false};
  fmt.onchange=()=>{$('#pillfield',right).style.display=fmt.value==='gif'?'':'none';refreshCode()};

  /* wiring */
  flip.onchange=()=>{S.hflip=flip.checked;save();previewD()};autoChk.onchange=()=>{S.auto=autoChk.checked;save();search();previewD()};keepChk.onchange=()=>{S.keep=keepChk.checked;save()};scaleSel.onchange=()=>{S.scale=+scaleSel.value;save();previewD()};
  delay.oninput=()=>{S.delay=+delay.value;save();refreshCode()};loop.onchange=()=>{S.loop=loop.checked;save();refreshCode()};
  bgSel.onchange=()=>{S.bgMode=bgSel.value;bgCol.style.display=S.bgMode==='color'?'':'none';save();previewD()};bgCol.oninput=()=>{S.bg=bgCol.value;save();previewD()};
  async function loadAvatar(keepLook){animSel.disabled=true;animSel.replaceChildren(el('option',{text:'loading…'}));try{await loadAvatarInner(keepLook)}finally{animSel.disabled=false}}
  async function loadAvatarInner(keepLook){defaults=await getJ('/api/avatar-colors?avatar='+S.avatar);for(const k of Object.keys(defaults))defaults[k]=norm(defaults[k]);msg.textContent=S.auto!==false?'checking files (missing ones are downloaded)…':'';anims=await getJ('/api/animations?avatar='+S.avatar+'&auto='+(S.auto!==false?1:0));msg.textContent='';
    animSel.replaceChildren(...anims.map(a=>el('option',{value:a.cat,text:'#'+a.cat+' · '+a.steps+' frame'+(a.steps===1?'':'s')})));
    if(!anims.length){msg.textContent=window.PUBLIC?'This avatar is not available on this server.':'No animations of this avatar are downloaded'+(S.auto!==false?' and none were found on the CDN':' yet. Tick "download missing files automatically", or run: mirror_assets.py --types avatars --avatar '+S.avatar);S.cat=null;frames=[];clearTimeout(timer);drawColors();return}
    if(!anims.some(a=>a.cat===S.cat))S.cat=(anims.find(a=>a.cat===7)||anims[0]).cat;animSel.value=S.cat;msg.textContent='';if(!keepLook){S.slots={};S.wear=[]}save();drawColors();drawWorn();loadParts();previewD()}
  avSel.onchange=async()=>{S.avatar=+avSel.value;note.textContent='';let left_out=[];
    if(S.keep===true&&S.wear.length){try{const r=await(await postJ('/api/look',{look:lookNow()})).json();const bad=r.items.filter(i=>!i.fits);left_out=bad.map(b=>b.name);S.wear=S.wear.filter(w=>!bad.some(b=>b.id===w.id))}catch(e){}}
    await loadAvatar(S.keep===true);if(left_out.length)flash('Left out (not made for this avatar): '+left_out.join(', '),true)};animSel.onchange=()=>{S.cat=+animSel.value;save();search();loadParts();previewD()};
  /* ---- undo / redo (25 steps each way) and resets ---- */
  const undo=[],redo=[];let hcur=null,restoring=false,lastTrack=0;
  const snap=()=>JSON.stringify({a:S.avatar,c:S.cat,f:S.hflip,s:S.scale,d:S.delay,l:S.loop,m:S.bgMode,b:S.bg,sl:S.slots,w:S.wear});
  const undoBtn=el('button',{class:'btn',text:'↶ Undo',disabled:true,title:'Ctrl+Z'}),redoBtn=el('button',{class:'btn',text:'Redo ↷',disabled:true,title:'Ctrl+Y'});
  function updHist(){undoBtn.disabled=!undo.length;redoBtn.disabled=!redo.length;undoBtn.textContent='↶ Undo'+(undo.length?' ('+undo.length+')':'');redoBtn.textContent='Redo ↷'+(redo.length?' ('+redo.length+')':'')}
  function track(){const now=snap();if(hcur===null){hcur=now;return}if(now===hcur)return;if(restoring){hcur=now;return}
    const t=Date.now();if(t-lastTrack>600){undo.push(hcur);if(undo.length>25)undo.shift()}  // a burst of edits (dragging a colour) is one step
    lastTrack=t;redo.length=0;hcur=now;updHist()}
  async function restore(str){const o=JSON.parse(str);restoring=true;try{const newAv=o.a!==S.avatar;
      S.avatar=o.a;S.cat=o.c;S.hflip=o.f;S.scale=o.s;S.delay=o.d;S.loop=o.l;S.bgMode=o.m;S.bg=o.b;S.slots=o.sl;S.wear=o.w;
      avSel.value=S.avatar;flip.checked=S.hflip;scaleSel.value=S.scale;delay.value=S.delay;loop.checked=S.loop;bgSel.value=S.bgMode;bgCol.value=S.bg;bgCol.style.display=S.bgMode==='color'?'':'none';
      if(newAv)await loadAvatar(true);else{animSel.value=S.cat;save();drawColors();drawWorn();search();previewD()}}
    finally{restoring=false;hcur=snap();updHist()}}
  let busy=Promise.resolve();const serial=fn=>{busy=busy.then(fn,fn);return busy};  // one undo/redo at a time, even with a held-down key
  undoBtn.onclick=()=>serial(async()=>{if(!undo.length)return;redo.push(hcur);await restore(undo.pop())});
  redoBtn.onclick=()=>serial(async()=>{if(!redo.length)return;undo.push(hcur);await restore(redo.pop())});
  document.addEventListener('keydown',e=>{if(!(e.ctrlKey||e.metaKey)||!sections.gif||!sections.gif.s.classList.contains('on'))return;const t=e.target;
    if(t&&(t.tagName==='TEXTAREA'||(t.tagName==='INPUT'&&t.type==='text')))return;const k=e.key.toLowerCase();
    if(k==='z'&&!e.shiftKey){e.preventDefault();undoBtn.click()}else if(k==='y'||(k==='z'&&e.shiftKey)){e.preventDefault();redoBtn.click()}});
  left.prepend(el('div',{class:'toolbar'},undoBtn,redoBtn,
    el('button',{class:'btn',text:'reset colours',title:'back to the default colours of the avatar and of each item',onclick:()=>{S.slots={};for(const w of S.wear)w.colors=null;save();drawColors();drawWorn();previewD()}}),
    el('button',{class:'btn',text:'clear clothing',title:'take off everything you added (pattern and eyes stay)',onclick:()=>{S.wear=S.wear.filter(w=>w.slot<=3);save();drawWorn();search();previewD()}})));
  const loading=on=>{for(const sel of[avSel,animSel]){sel.disabled=on;if(on)sel.replaceChildren(el('option',{text:'loading…'}))}};
  const init=async()=>{try{loading(true);msg.textContent='loading the game data… (the first time can take a few seconds)';await loadSaves();drawOutfits();avSel.replaceChildren();palette=await getJ('/api/palette');for(const k of Object.keys(S.slots))S.slots[k]=norm(S.slots[k]);for(const w of S.wear){w.def=norm(w.def);w.colors=w.colors?norm(w.colors):null}const avs=await getJ('/api/avatars');
      avSel.append(...avs.filter(a=>!window.PUBLIC||a.animations>0).map(a=>el('option',{value:a.id,text:a.name+' #'+a.id+(window.PUBLIC?'':' · '+a.animations+' downloaded')})));
      if(S.avatar==null||!avs.some(a=>a.id===S.avatar))S.avatar=(avs.find(a=>a.animations>0)||avs[0]).id;avSel.value=S.avatar;
      if(window.PUBLIC&&!window.PUBLIC_DL){S.auto=false;autoChk.parentElement.style.display='none'}flip.checked=S.hflip;autoChk.checked=S.auto!==false;keepChk.checked=S.keep===true;scaleSel.value=S.scale;delay.value=S.delay;loop.checked=S.loop;bgSel.value=S.bgMode;bgCol.value=S.bg;bgCol.style.display=S.bgMode==='color'?'':'none';fmt.onchange();
      await loadAvatar(true);loading(false);hcur=snap();undo.length=0;redo.length=0;updHist()}catch(e){loading(false);msg.innerHTML='';msg.append(el('span',{class:'err',text:'Could not load the game data: '+(e.message||e)+(window.PUBLIC?'. The server may be down, or may not allow this site.':'. Is the cache folder right (see the top of the page)?')}))}};
  init();window.gifReload=init;
  return root}

/* ---------- den items: browse the game's den items and see their pictures ---------- */
function denTab(){
  const root=el('div',{class:'den'});
  const q=el('input',{type:'text',placeholder:'search den items: a name or an id (piano, 617 ...)',spellcheck:'false'});
  const mk=(pairs)=>pairs.map(([v,t])=>el('option',{value:v,text:t}));
  const filt=el('select',{},...mk([['all','all items'],['pic','with a picture'],['nopic','without a picture'],['anim','animated'],['free','free'],['members','members only']]));
  const sort=el('select',{},...mk([['id','by id'],['name','by name'],['cost','by price']]));
  const grid=el('div',{class:'dgrid'}),pager=el('div',{class:'bar dpager'}),status=el('div',{class:'mute'});
  const detail=el('div',{class:'card ddetail',hidden:true});
  let page=0,selId=null,anim=null;
  const pic=(id,cat,frame)=>API+'/api/den-image?id='+id+'&cat='+cat+'&frame='+frame+(TOKEN?'&t='+encodeURIComponent(TOKEN):'');
  const price=it=>it.cost===0?'free':it.cost.toLocaleString()+' '+(it.currency||'gems');

  async function load(){status.textContent='loading…';
    try{const r=await getJ('/api/den?q='+encodeURIComponent(q.value)+'&filter='+filt.value+'&sort='+sort.value+'&page='+page+'&size=60');page=r.page;
      grid.replaceChildren(...r.items.map(it=>el('button',{class:'dcell'+(it.id===selId?' on':''),title:it.name+' (#'+it.id+')',onclick:()=>open(it.id)},
        it.pic?el('img',{src:pic(it.id,it.pic.cat,1),loading:'lazy',alt:it.name}):el('div',{class:'noimg',text:'no picture'}),el('span',{text:it.name}))));
      status.textContent=r.total.toLocaleString()+' den item'+(r.total===1?'':'s')+(r.total?' · page '+(r.page+1)+' of '+r.pages:'')+(r.rendered?' · '+r.rendered+' with pictures':' · no pictures rendered yet');
      pager.replaceChildren(el('button',{class:'btn',text:'‹ previous',disabled:r.page===0,onclick:()=>{page--;load();grid.scrollIntoView&&grid.scrollIntoView()}}),
        el('button',{class:'btn',text:'next ›',disabled:r.page>=r.pages-1,onclick:()=>{page++;load();grid.scrollIntoView&&grid.scrollIntoView()}}));
      if(!r.items.length)grid.append(el('div',{class:'mute',text:'No den items match.'}))}
    catch(e){status.textContent='';grid.replaceChildren(el('div',{class:'err',text:String(e.message||e)}))}}

  function stop(){if(anim){clearInterval(anim);anim=null}}
  async function open(id){selId=id;stop();detail.hidden=false;detail.replaceChildren(el('div',{class:'mute',text:'loading…'}));
    document.querySelectorAll('.dcell.on').forEach(c=>c.classList.remove('on'));
    try{const d=await getJ('/api/den-item?id='+id);let cur=d.pictures.length?d.pictures[0]:null;
      const view=el('div',{class:'dview'}),catRow=el('div',{class:'bar',style:'margin:4px 0'}),msg=el('div',{class:'mute'});
      const show=pc=>{cur=pc;stop();view.replaceChildren();
        if(!pc){view.append(el('div',{class:'noimg big',text:'no picture rendered yet'}));return}
        const imgs=[];for(let f=1;f<=pc.frames;f++)imgs.push(el('img',{src:pic(d.id,pc.cat,f),alt:d.name}));
        const one=imgs[0];view.append(one);
        if(imgs.length>1){let i=0;anim=setInterval(()=>{i=(i+1)%imgs.length;one.src=imgs[i].src},90)}}
      detail.replaceChildren(...[
        el('div',{class:'bar',style:'margin:0;justify-content:space-between'},el('b',{text:d.name}),el('button',{class:'btn',text:'close',onclick:()=>{stop();detail.hidden=true;selId=null}})),
        view,
        d.pictures.length>1?(catRow.append(...d.pictures.map(pc=>el('button',{class:'btn',text:'view '+pc.cat,onclick:()=>show(pc)}))),catRow):null,
        el('div',{class:'dmeta'},...[['id','#'+d.id],['art name',d.abbr||'—'],['price',price(d)],['members',d.members?'yes':'no'],['group',d.group],['type',d.type],['layer',d.layer],['scale',d.scale?d.scale+'%':'—'],['environment',d.enviro],
          ['frames',d.pictures.length?d.pictures.map(p=>'view '+p.cat+': '+p.frames).join(', '):'—'],
          ['placement (cx, cy, so, r)',cur&&cur.meta&&cur.meta.cx!==null?[cur.meta.cx,cur.meta.cy,cur.meta.so,cur.meta.r].join(', '):'—'],
          ...(window.PUBLIC?[]:[['in your cache',d.inCache&&d.inCache.length?'views '+d.inCache.join(', '):'not downloaded'],['music file',d.music?'yes':'no']])].map(([k,v])=>el('div',{class:'drow'},el('span',{class:'k',text:k}),el('span',{text:String(v)})))),
        el('div',{class:'bar'},el('button',{class:'btn',text:'copy id',onclick:async()=>{try{await navigator.clipboard.writeText(String(d.id));msg.textContent='Copied '+d.id}catch(e){msg.textContent=String(d.id)}}}),
          ...(window.PUBLIC?[]:[
            d.pictures.length?el('a',{class:'btn',href:API+'/api/den-swf?id='+d.id+'&cat='+(cur?cur.cat:1)+(TOKEN?'&t='+encodeURIComponent(TOKEN):''),text:'download .swf',download:''}):null,
            el('button',{class:'btn'+(d.pictures.length?'':' pri'),text:d.pictures.length?'draw again':'draw it now',title:'download the item\'s files if needed and draw them (needs ffdec and Java)',onclick:async e=>{e.target.disabled=true;msg.textContent='drawing… (downloads the files if needed; a few seconds)';
              try{await postJ('/api/den-render',{id:d.id});await open(d.id);load()}catch(er){msg.className='err';msg.textContent=String(er.message||er);e.target.disabled=false}}})])),
        msg,
        d.pictures.length||window.PUBLIC?null:el('div',{class:'mute',text:'Or from a terminal: python extract_den_items.py --item '+d.id})].filter(Boolean));   // the browser's replaceChildren would print a null as the word "null"
      show(cur);if(narrow()&&detail.scrollIntoView)detail.scrollIntoView({block:'nearest'})}
    catch(e){detail.replaceChildren(el('div',{class:'err',text:String(e.message||e)}),el('button',{class:'btn',text:'close',onclick:()=>{detail.hidden=true}}))}}

  const reload=debounce(()=>{page=0;load()},250);q.oninput=reload;filt.onchange=sort.onchange=()=>{page=0;load()};
  root.append(el('div',{class:'dleft'},el('div',{class:'card'},q,el('div',{class:'row2',style:'margin-top:6px;margin-bottom:0'},el('div',{class:'field'},filt),el('div',{class:'field'},sort)),status),grid,pager),detail);
  load();return root}

/* ---------- start ---------- */
(async()=>{
  try{const i=await getJ('/api/info');window.PUBLIC=!!i.public;window.PUBLIC_DL=!!i.download;
    if(i.public){$('#info').textContent='Pillow: '+(i.pillow?'yes':'no (built-in GIF encoder)');$('.cfg').style.display='none'}
    else{$('#info').textContent='scripts run in: '+i.workdir+' · Pillow: '+(i.pillow?'yes':'no (built-in GIF encoder)')+' · Python '+i.python;applyConf(i);
      const saved=store.get('conf',null);if(!i.explicit&&saved&&(saved.cache!==i.cache||saved.build!==i.build)){$('#cfgCache').value=saved.cache;$('#cfgBuild').value=saved.build;await useConf(true)}}}catch(e){$('#info').textContent=window.PUBLIC?'Could not reach the server'+(API?' at '+API:'')+': '+String(e.message||e):String(e);if(window.PUBLIC){$('.cfg').style.display='none';tabsEl.style.display='none'}}
  addTab('gif','GIF maker',gifTab());
  addTab('den','Den items',denTab());
  if(!window.PUBLIC){try{const sc=await getJ('/api/schema');for(const s of sc.scripts)addTab(s.name,s.name,scriptTab(s))}catch(e){addTab('err','Scripts',el('div',{class:'card err',text:String(e.message||e)}))}}
  const want=store.get('tab','gif');show(sections[want]?want:'gif');
})();
</script></body></html>'''

if __name__ == "__main__":
    main()
