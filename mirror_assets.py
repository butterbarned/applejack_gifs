#!/usr/bin/env python3
"""Download Animal Jam Classic assets from the official CDN (standard library only, Python 3.8+).

The CDN no longer lets anyone list its files, so this works the way the game does: it
downloads the definition packs (defPacks), works out each file's exact hashed name from
them, and asks for it. Files that do not exist answer 404 and are remembered, so a re-run
only asks about what is new. Nothing is ever deleted or overwritten.

usage:
  python mirror_assets.py --dry-run                      what each type would ask for
  python mirror_assets.py --types defs,rooms             pick types (comma list)
  python mirror_assets.py --types avatars --avatar 8     one animal
  python mirror_assets.py --types all                    everything below

TYPES (--types, default "defs")
  defs      the definition packs and the colour palette (small; always fetched first when
            another type needs them)
  avatars   the animals themselves: body, eyes and pattern layers (+ animation sequences)
            for every animation, for every avatar
  clothing  every wearable item's layer, for each avatar allowed to wear it, per animation
            (BY FAR the largest type; see the estimate in --dry-run)
  media     media/<id> files: SWFs for icons, cards, wallpapers, UI art and more
            (ids 1..--media-max)
  rooms     roomDefs/<room>.xroom for every room in the room def; each carries its own art
            (minigame scenes are rooms too, so this includes them)
  icons     the dress-up / shop / inventory icons: small (category 26) and large (25) pictures
            of every layer, as imageArrays of avatar 1 (item/SimpleIcon)
  den       den item art: items/<defId<<16|category> for every den item (furniture, decor...)
  audio     den music and sound items: audio/<abbrName>.mp3 for every den item name
  shell     the game's UI libraries: assets/HUDAssets.swf and assets/sharedFonts.swf
  streams   streams/<id>.mp3 and .flv (videos and spoken clips) for the ids in defPack 1053
  all       all of the above
  Not covered: the game client itself (ajclient.swf), masterpieces, subtitles, poll and
  feedback defs; their names have not been worked out.

DOWNLOAD ONE THING (same words as --has below)
  python mirror_assets.py --get avatar 8 item 276       every animation of that clothing item, avatar 8
  python mirror_assets.py --get avatar 8 item 276 category 7
  python mirror_assets.py --get avatar 8                 that avatar's body/eyes/pattern files
  python mirror_assets.py --get icon 2346 | den 52 | media 44 | room party/venue_carnival | audio NAME

SOMETHING IS MISSING OR WRONG?
  python mirror_assets.py --doctor            checks the folder for everything the GIF maker needs and prints the
                                              exact download command for whatever is missing
  python mirror_assets.py --forget-misses     clears the downloader's memory of "not on the CDN" answers (use if
                                              the CDN was refusing you, or after changing --build); files stay

LOOK AT WHAT YOU HAVE (no network; reads --out)
  python mirror_assets.py --report                 counts per type, per avatar, what is missing
  python mirror_assets.py --report --verbose       also lists every item/room/track present
  python mirror_assets.py --find fox hat           search items, den items and avatars BY NAME
                                                   (English names, defPack 10230), and say
                                                   whether each is downloaded
  python mirror_assets.py --has avatar 8 item 276  is this asset here? which animations?
  --has takes key value pairs (or key=value):
    avatar A item I [category C]   clothing item I (defPack 1000 id) for avatar A
    avatar A layer L [category C]  a layer def id directly (0 = the animation sequence file)
    icon I        the shop/dress-up icons of item I      den D     a den item (defPack 1030 id)
    media N       media/N       room PATH      e.g. room party/venue_carnival
    audio NAME    audio/NAME.mp3     stream N     def N (defPack)     shell NAME (HUDAssets...)

options:
  --out DIR         where files go (default ./cache, laid out like the server's cache, so
                    <out>/1830/imageArrays/<hash>). Use a different folder to keep it apart.
  --build N         CDN build folder (default 1830)
  --avatar N        only this avatar id (repeatable)           [avatars, clothing]
  --category N      only this animation category (repeatable)  [avatars, clothing]
  --wide            ask every avatar about every category any avatar has (slower, finds
                    layers an avatar's own body lacks)       [avatars, clothing]
  --media-max N     highest media id to ask for (default 10000; the defs use ids up to 9428)
  --den-cats N      den item categories 1..N to ask for (default 8; the ones found are 1-3)
  --concurrency N   parallel connections (default 16)
  --limit N         stop after N requests per type (to test speed)
  --dry-run         count what would be asked for; downloads nothing but the defs
  --no-prune        ask about every avatar x animation x layer, even the ones known to 404
                    (default: skip animations where sampling found no clothing for that avatar,
                    and eye/pattern layers an avatar cannot use; saves roughly a third of requests)
  --no-defs-fetch   never download defPacks, only use ones already in --out

Finished runs leave <out>/.mirror/ with the 404 list per type (so re-runs skip them) and the
animation categories found per avatar. Delete it to ask again about everything.
"""
import argparse, collections, http.client, json, os, queue, re, struct, sys, threading, time, zlib

HOST = "ajcontent.akamaized.net"
UA = "Mozilla/5.0"
QUERY = "?v=3"
MAX_CATEGORY_SCAN = 64  # categories 0..63 are scanned to learn which exist per avatar
BASE_ITEMS_EXTRA = (0,)  # 0 = the animation sequence file
BASE_SLOTS = (1, 2, 3, 9)  # body, pattern, eyes... layer slots 1-3 (+9), from defPack 1027
DEF_IDS = list(range(1000, 1066)) + [10230]


# ------------------------------------------------------------------ names
def hash_name(name):
    """LoaderCache.hashItV2: the last path part of every versioned asset is hashed"""
    import hashlib
    s, out = "W3 7r4Ck h4X0r3rs" + name, ""
    for i, c in enumerate(s):
        out = out + c if i % 2 == 0 else c + out
    return hashlib.md5(out.encode()).hexdigest()


def hashed(build, path):
    parts = path.split("/")
    parts.append(hash_name(parts.pop()))
    return build + "/" + "/".join(parts)


def pack_id(avatar, category, item):
    return avatar << 24 | category << 16 | item


# ------------------------------------------------------------------ AMF3 (defs)
class AMF3:
    def __init__(self, d):
        self.d, self.i, self.strs, self.objs, self.traits = d, 0, [], [], []

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
        s = self.d[self.i:self.i + n].decode("utf8", "replace")
        self.i += n
        if n:
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
            out = {}
            self.objs.append(out)
            while True:
                k = self.string()
                if k == "":
                    break
                out[k] = self.value()
            for n in range(r >> 1):
                out[str(n)] = self.value()
            return out
        if t == 10:
            r = self.u29()
            if not r & 1:
                return self.objs[r >> 1]
            if not r & 2:
                names, dyn = self.traits[r >> 2]
            else:
                dyn, n = bool(r & 8), r >> 4
                self.string()
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
        raise ValueError("unsupported AMF3 type %d" % t)


def read_def(path):
    with open(path, "rb") as f:
        raw = f.read()
    return AMF3(zlib.decompress(raw, -15)).value()


# ------------------------------------------------------------------ http + pool
THROTTLED = collections.Counter()  # 429 / 5xx answers seen (the CDN asking us to slow down)


class Conn(threading.local):
    c = None


_conn = Conn()


def http_get(key, tries=4):
    """returns (status, body or None). 404/403 are final answers; other errors are retried"""
    path = "/" + key + QUERY
    for attempt in range(tries):
        try:
            if _conn.c is None:
                _conn.c = http.client.HTTPSConnection(HOST, timeout=30)
            _conn.c.request("GET", path, headers={"User-Agent": UA})
            r = _conn.c.getresponse()
            body = r.read()
            if r.status == 200:
                return 200, body
            if r.status in (403, 404):
                return r.status, None
            if r.status in (429, 500, 502, 503, 504):
                THROTTLED[r.status] += 1
                time.sleep(2 * (attempt + 1))
                continue
            return r.status, None
        except (http.client.HTTPException, OSError):
            try:
                _conn.c.close()
            except Exception:
                pass
            _conn.c = None
            time.sleep(1 + attempt)
    return -1, None


class Mirror:
    def __init__(self, out, concurrency, create=True):
        self.out, self.conc = os.path.abspath(out), concurrency
        self.state = os.path.join(self.out, ".mirror")
        if create:
            os.makedirs(self.state, exist_ok=True)
        self.lock = threading.Lock()

    def dest(self, key):
        if key.startswith("/") or ".." in key.split("/") or "\\" in key:
            raise ValueError("unsafe key " + key)
        p = os.path.abspath(os.path.join(self.out, *key.split("/")))
        if not p.startswith(self.out + os.sep):
            raise ValueError("unsafe key " + key)
        return p

    def misses(self, name):
        path = os.path.join(self.state, "misses-%s.txt" % name)
        try:
            with open(path) as f:
                return set(f.read().split())
        except OSError:
            return set()

    def run(self, name, keys, limit=0, label=None, remember=True):
        """ask for every key; save hits. returns Counter of results and the set of hit keys.
        remember=False asks about everything again and does not record 404s (used by --get)"""
        missed = self.misses(name) if remember else set()
        miss_file = open(os.path.join(self.state, "misses-%s.txt" % name), "a") if remember else open(os.devnull, "w")
        stats = collections.Counter()
        hits, failed = set(), []
        it = iter(keys)
        started = time.time()
        sent = [0]

        def take():
            with self.lock:
                while True:
                    if limit and sent[0] >= limit:
                        return None
                    k = next(it, None)
                    if k is None:
                        return None
                    if k in missed:
                        stats["known missing"] += 1
                        continue
                    d = self.dest(k)
                    if os.path.exists(d):
                        stats["already have"] += 1
                        hits.add(k)
                        continue
                    sent[0] += 1
                    return k

        def worker():
            while True:
                k = take()
                if k is None:
                    return
                status, body = http_get(k)
                with self.lock:
                    if status == 200:
                        d = self.dest(k)
                        os.makedirs(os.path.dirname(d), exist_ok=True)
                        tmp = "%s.%d.tmp" % (d, os.getpid())
                        with open(tmp, "wb") as f:
                            f.write(body)
                        os.replace(tmp, d)
                        stats["downloaded"] += 1
                        stats["bytes"] += len(body)
                        hits.add(k)
                    elif status == 404:
                        stats["missing (404)"] += 1
                        miss_file.write(k + "\n")
                    elif status == 403:
                        # the CDN answers 404 for a name that does not exist; 403 means it is refusing us (blocked, rate
                        # limited): NOT remembered as missing, so a later run asks again
                        stats["forbidden (403)"] += 1
                        failed.append(k)
                    else:
                        stats["failed"] += 1
                        failed.append(k)

        done = threading.Event()

        def ticker():
            while not done.wait(10):
                print("  [%s %ds] asked %d | new %d | 404 %d | have %d | skipped %d | failed %d | %.1f MB | %.0f req/s%s" % (
                    label or name, time.time() - started, sent[0], stats["downloaded"], stats["missing (404)"],
                    stats["already have"], stats["known missing"], stats["failed"], stats["bytes"] / 1048576,
                    sent[0] / max(1, time.time() - started),
                    " | THROTTLED %d (CDN says slow down; lower --concurrency)" % sum(THROTTLED.values()) if THROTTLED else ""), flush=True)

        threading.Thread(target=ticker, daemon=True).start()
        threads = [threading.Thread(target=worker) for _ in range(self.conc)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            print("\nstopped; run again to continue (finished files are skipped)")
            miss_file.flush()
            os._exit(1)
        done.set()
        miss_file.close()
        if failed:
            with open(os.path.join(self.state, "failed-%s.txt" % name), "w") as f:
                f.write("\n".join(failed) + "\n")
        secs = max(1, time.time() - started)
        print("  %s: asked %d in %ds | new %d (%.1f MB) | 404 %d | already had %d | known missing %d | failed %d" % (
            name, sent[0], secs, stats["downloaded"], stats["bytes"] / 1048576, stats["missing (404)"],
            stats["already have"], stats["known missing"], stats["failed"]))
        if stats["forbidden (403)"]:
            print("  !! the CDN refused %d requests (403). That usually means it is blocking or rate-limiting you: wait a while, use a lower "
                  "--concurrency, and run the same command again (refused names are not remembered)." % stats["forbidden (403)"])
        return stats, hits


# ------------------------------------------------------------------ plan
class Plan:
    def __init__(self, m, a):
        self.m, self.a, self.build = m, a, a.build
        self._defs = {}
        self.cloth_cats = None  # {avatar: animations that have clothing}; None = ask about all

    def defpath(self, did):
        return os.path.join(self.m.out, *hashed(self.build, "defPacks/%d" % did).split("/"))

    def keys_defs(self):
        for did in DEF_IDS:
            yield hashed(self.build, "defPacks/%d" % did)
        yield hashed(self.build, "paletteData")

    def need_defs(self, ids):
        missing = [i for i in ids if not os.path.exists(self.defpath(i))]
        if missing and not self.a.no_defs_fetch:
            print("fetching defPacks %s ..." % ", ".join(map(str, missing)))
            self.m.run("defs", [hashed(self.build, "defPacks/%d" % i) for i in missing], label="defs")
        for i in ids:
            if not os.path.exists(self.defpath(i)):
                sys.exit("defPack %d is not in %s (drop --no-defs-fetch, or run --types defs)" % (i, self.m.out))

    def d(self, did):
        if did not in self._defs:
            self.need_defs([did])
            self._defs[did] = read_def(self.defpath(did))
        return self._defs[did]

    def avatars(self):
        ids = sorted(int(k) for k in self.d(1003))
        return [x for x in ids if not self.a.avatar or x in self.a.avatar]

    # --- animation categories per avatar
    def cat_file(self):
        return os.path.join(self.m.state, "categories-%s.json" % self.build)

    def load_cats(self):
        try:
            with open(self.cat_file()) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def discover(self, avatars):
        """which animation categories each avatar has, by asking for its body layers"""
        cats = self.load_cats()
        todo = [x for x in avatars if x not in cats]
        if todo:
            print("learning animation categories for %d avatars (scanning 0..%d for body/eyes) ..." % (
                len(todo), MAX_CATEGORY_SCAN - 1))
            probes ={"%s/imageArrays/%s" % (self.build, hash_name(str(pack_id(av, c, item)))): (av, c)
                      for av in todo for c in range(MAX_CATEGORY_SCAN) for item in (3, 22)}
            _, hit = self.m.run("categories", probes.keys(), label="categories")
            found = collections.defaultdict(set)
            for k in hit:
                av, c = probes[k]
                found[av].add(c)
            for av in todo:
                cats[av] = sorted(found[av])
            with open(self.cat_file(), "w") as f:
                json.dump(cats, f)
        return cats

    def cats_for(self, cats, av):
        use = cats.get(av, [])
        if self.a.wide:
            use = sorted({c for v in cats.values() for c in v})
        if self.a.category:
            use = [c for c in use if c in self.a.category]
        return use

    def image_key(self, av, c, item):
        return "%s/imageArrays/%s" % (self.build, hash_name(str(pack_id(av, c, item))))

    # --- key streams
    def base_items(self):
        slots = self.d(1027)
        ids = {0} | {int(k) for k, r in slots.items() if int(r["layerId"]) in BASE_SLOTS}
        return sorted(ids)

    def clothing_pairs(self):
        avs = set(self.avatars())
        pairs = set()
        for row in self.d(1000).values():
            layer = int(row["layerDefId"])
            if layer == 0:
                continue
            flags = int(row["avatarUseFlags"])
            for av in avs:
                if flags >> (av - 1) & 1:
                    pairs.add((layer, av))
        return pairs

    def avatar_layers(self):
        """{avatar: sorted layer defs worth asking for}: the sequence file (0), the body, the
        avatar's own eyes and pattern, and the eye/pattern layers whose items allow that avatar
        (avatarUseFlags). Asking every avatar about all 47 eye and pattern layers wastes half the
        requests on 404s."""
        slots = {int(r["id"]): int(r["layerId"]) for r in self.d(1027).values()}
        body = [i for i, sl in slots.items() if sl == 1]
        by_id = {r["id"]: r for r in self.d(1000).values()}
        out = {}
        for av in self.avatars():
            layers = {0, *body}
            row = self.d(1003)[str(av)]
            for key in ("baseEyesId", "basePatternId"):
                it = by_id.get(row.get(key, "0"))
                if it:
                    layers.add(int(it["layerDefId"]))
            out[av] = layers
        for r in by_id.values():
            if r["type"] == "0":
                flags = int(r["avatarUseFlags"])
                for av in out:
                    if flags >> (av - 1) & 1 and int(r["layerDefId"]) in slots:
                        out[av].add(int(r["layerDefId"]))
        return {av: sorted(v) for av, v in out.items()}

    def keys_avatars(self, cats):
        layers = self.avatar_layers() if not self.a.no_prune else {av: self.base_items() for av in self.avatars()}
        for av in self.avatars():
            for c in self.cats_for(cats, av):
                for item in layers[av]:
                    yield self.image_key(av, c, item)

    def cloth_file(self):
        return os.path.join(self.m.state, "clothing-categories-%s.json" % self.build)

    def load_cloth_cats(self):
        try:
            with open(self.cloth_file()) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def learn_clothing_cats(self, cats, samples=12):
        """Clothing does not exist in every animation the body does (avatar 8: 7 of 28 never have
        any). For each avatar, ask about a spread of `samples` clothing layers in every body
        animation, and keep the animations where at least a quarter of the samples existed."""
        known = self.load_cloth_cats()
        by_av = collections.defaultdict(list)
        for layer, av in self.clothing_pairs():
            by_av[av].append(layer)
        todo = [av for av in sorted(by_av) if av not in known and self.cats_for(cats, av)]
        if todo:
            print("learning which animations have clothing, %d avatars x %d sample layers ..." % (len(todo), samples))
            probes = {}
            for av in todo:
                ls = sorted(by_av[av])
                pick = [ls[i * (len(ls) - 1) // max(1, samples - 1)] for i in range(min(samples, len(ls)))]
                for layer in pick:
                    for c in self.cats_for(cats, av):
                        probes[self.image_key(av, c, layer)] = (av, c)
            _, hit = self.m.run("clothing", probes.keys(), label="clothing samples")
            # a category counts when several sample layers exist in it: a couple of special layers
            # (ids 1 and 31 exist in every animation) must not make every animation look valid
            need = max(3, samples // 4)
            count = collections.Counter(probes[k] for k in hit)
            for av in todo:
                known[av] = sorted(c for (a2, c), n in count.items() if a2 == av and n >= need)
                if not known[av]:
                    print("  avatar %d: no clothing found in any sample animation; skipped (use --no-prune to force)" % av)
            with open(self.cloth_file(), "w") as f:
                json.dump(known, f)
        return known

    def keys_clothing(self, cats):
        by_av = collections.defaultdict(list)
        for layer, av in self.clothing_pairs():
            by_av[av].append(layer)
        for av in sorted(by_av):
            use = self.cats_for(cats, av)
            if self.cloth_cats and av in self.cloth_cats:
                use = [c for c in use if c in self.cloth_cats[av]]
            for layer in sorted(by_av[av]):
                for c in use:
                    yield self.image_key(av, c, layer)

    def keys_media(self):
        for i in range(1, self.a.media_max + 1):
            yield hashed(self.build, "media/%d" % i)

    def keys_rooms(self):
        seen = set()
        for row in self.d(1011).values():
            p = row.get("pathName")
            if p and p not in seen:
                seen.add(p)
                yield hashed(self.build, "roomDefs/%s.xroom" % p)

    def keys_icons(self):
        # SimpleIcon: packId(avatar 1, 25 large | 26 small, layerDefId)
        for layer in sorted(int(r["id"]) for r in self.d(1027).values()):
            for cat in (25, 26):
                yield self.image_key(1, cat, layer)

    def keys_den(self):
        # DenItemHelper: items/<defId << 16 | category>
        for did in sorted(int(r["id"]) for r in self.d(1030).values()):
            for cat in range(1, self.a.den_cats + 1):
                yield "%s/items/%s" % (self.build, hash_name(str(did << 16 | cat)))

    def keys_audio(self):
        # DenItemHolder: a music den item plays audio/<abbrName>.mp3
        seen = set()
        for row in self.d(1030).values():
            name = row.get("abbrName")
            if name and name not in seen:
                seen.add(name)
                yield hashed(self.build, "audio/%s.mp3" % name)

    def keys_shell(self):
        yield hashed(self.build, "assets/HUDAssets.swf")
        yield hashed(self.build, "assets/sharedFonts.swf")

    def keys_streams(self):
        ids = set()
        for row in self.d(1053).values():
            for k in ("id", "streamRefId"):
                if str(row.get(k, "")).isdigit():
                    ids.add(int(row[k]))
        for i in sorted(ids):
            for ext in ("mp3", "flv"):
                yield hashed(self.build, "streams/%d.%s" % (i, ext))


# ------------------------------------------------------------------ what do we have
def have(m, key):
    p = m.dest(key)
    return p if os.path.isfile(p) else None


def categories_scan(m, plan, av, layer):
    return [c for c in range(MAX_CATEGORY_SCAN) if have(m, plan.image_key(av, c, layer))]


def spans(nums):
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'"""
    nums, out = sorted(nums), []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(out) or "none"


def parse_pairs(tokens):
    out, i = {}, 0
    while i < len(tokens):
        t = tokens[i]
        if "=" in t:
            k, v = t.split("=", 1)
            i += 1
        else:
            if i + 1 >= len(tokens):
                sys.exit("--has: %r needs a value (e.g. --has avatar 8 item 276)" % t)
            k, v = t, tokens[i + 1]
            i += 2
        out[k.lower()] = v
    return out


def lookup(m, plan, tokens):
    q = parse_pairs(tokens)
    show = lambda label, key: print("  %-26s %s" % (label, ("HAVE  " + have(m, key)) if have(m, key) else "missing  (" + key + ")"))
    if "avatar" in q or "item" in q or "layer" in q:
        if "avatar" not in q:
            sys.exit("--has: clothing and layers need an avatar, e.g. --has avatar 8 item 276")
        av = int(q["avatar"])
        items = {r["id"]: r for r in plan.d(1000).values()}
        slots = {int(r["id"]): int(r["layerId"]) for r in plan.d(1027).values()}
        if "item" in q:
            row = items.get(q["item"])
            if not row:
                sys.exit("no item %s in defPack 1000" % q["item"])
            layer = int(row["layerDefId"])
            ok = int(row["avatarUseFlags"]) >> (av - 1) & 1
            print("item %s \"%s\": layer def %d, slot %s, type %s; avatar %d %s wear it" % (
                q["item"], Names(plan).clothing(row), layer, slots.get(layer), row["type"], av, "can" if ok else "CANNOT"))
        else:
            layer = int(q["layer"])
            print("layer def %d, slot %s" % (layer, slots.get(layer)))
        learned = plan.load_cats().get(av)
        cats = [int(q["category"])] if "category" in q else (learned or list(range(MAX_CATEGORY_SCAN)))
        found = [c for c in cats if have(m, plan.image_key(av, c, layer))]
        print("avatar %d, layer %d: %d of %d animation categories downloaded%s" % (
            av, layer, len(found), len(cats), "" if learned or "category" in q else " (scanned 0-%d)" % (MAX_CATEGORY_SCAN - 1)))
        print("  have:    %s" % spans(found))
        print("  missing: %s" % spans([c for c in cats if c not in found]))
        if "category" in q:
            show("category %s" % q["category"], plan.image_key(av, int(q["category"]), layer))
    elif "icon" in q:
        row = {r["id"]: r for r in plan.d(1000).values()}.get(q["icon"])
        if not row:
            sys.exit("no item %s" % q["icon"])
        layer = int(row["layerDefId"])
        print("item %s \"%s\" uses layer def %d" % (q["icon"], Names(plan).clothing(row), layer))
        show("large icon (category 25)", plan.image_key(1, 25, layer))
        show("small icon (category 26)", plan.image_key(1, 26, layer))
    elif "den" in q:
        row = {int(r["id"]): r for r in plan.d(1030).values()}.get(int(q["den"]))
        if not row:
            sys.exit("no den item %s in defPack 1030" % q["den"])
        did = int(q["den"])
        print("den item %d \"%s\" (%s)" % (did, Names(plan).den(row), row.get("abbrName")))
        cats = [c for c in range(1, plan.a.den_cats + 1)]
        for c in cats:
            k = "%s/items/%s" % (plan.build, hash_name(str(did << 16 | c)))
            if have(m, k):
                show("category %d" % c, k)
        if not any(have(m, "%s/items/%s" % (plan.build, hash_name(str(did << 16 | c)))) for c in cats):
            print("  none of categories 1-%d downloaded" % cats[-1])
        print("  music file: " + ("HAVE  " + have(m, hashed(plan.build, "audio/%s.mp3" % row.get("abbrName"))))
              if have(m, hashed(plan.build, "audio/%s.mp3" % row.get("abbrName"))) else "  music file: missing (or not a music item)")
    elif "media" in q:
        show("media %s" % q["media"], hashed(plan.build, "media/%s" % q["media"]))
    elif "room" in q:
        show("room %s" % q["room"], hashed(plan.build, "roomDefs/%s.xroom" % q["room"]))
    elif "audio" in q:
        show("audio %s" % q["audio"], hashed(plan.build, "audio/%s.mp3" % q["audio"].replace(".mp3", "")))
    elif "stream" in q:
        for ext in ("mp3", "flv"):
            show("stream %s.%s" % (q["stream"], ext), hashed(plan.build, "streams/%s.%s" % (q["stream"], ext)))
    elif "def" in q:
        show("defPack %s" % q["def"], hashed(plan.build, "defPacks/%s" % q["def"]))
    elif "shell" in q:
        show(q["shell"], hashed(plan.build, "assets/%s" % q["shell"]))
    else:
        sys.exit("--has: say what to look up, e.g. --has avatar 8 item 276 | icon 276 | den 52 | media 100 | room party/venue_carnival")


def _scan_avatar(args):
    """which of the present file names are (avatar, category, layer) for this avatar"""
    av, layers, present = args
    out = {}
    for c in range(MAX_CATEGORY_SCAN):
        for l in layers:
            h = hash_name(str(pack_id(av, c, l)))
            if h in present:
                out[h] = (av, c, l)
    return out


# ------------------------------------------------------------------ names
class Names:
    """English names from the localization pack (defPack 10230): string id -> text"""

    def __init__(self, plan):
        self.text = {str(k): v for k, v in plan.d(10230).items() if isinstance(v, str)}
        self.plan = plan

    def get(self, sid):
        return self.text.get(str(sid)) or None

    def clothing(self, row):
        return self.get(row.get("titleStrId")) or "(no name)"

    def den(self, row):
        return self.get(row.get("nameStrId")) or row.get("abbrName") or "(no name)"

    def avatar(self, row):
        return self.get(row.get("titleStrRef")) or "(no name)"


def find(m, plan, words, limit):
    names = Names(plan)
    words = [w.lower() for w in words]
    match = lambda n: all(w in n.lower() for w in words)
    avs = sorted(int(k) for k in plan.d(1003))
    learned = plan.load_cats()
    rows = []
    for r in plan.d(1003).values():
        if match(names.avatar(r)):
            rows.append(("avatar", r["id"], names.avatar(r), r))
    for r in plan.d(1000).values():
        if match(names.clothing(r)):
            rows.append(("item" if r["type"] == "1" else "body/pattern", r["id"], names.clothing(r), r))
    for r in plan.d(1030).values():
        if match(names.den(r)):
            rows.append(("den", r["id"], names.den(r), r))
    print("%d match%s for %s" % (len(rows), "" if len(rows) == 1 else "es", " ".join(words)))
    for kind, iid, name, r in rows[:limit]:
        if kind == "den":
            cs = [c for c in range(1, plan.a.den_cats + 1)
                  if have(m, "%s/items/%s" % (plan.build, hash_name(str(int(iid) << 16 | c))))]
            status = ("downloaded (categories %s)" % spans(cs)) if cs else "not downloaded"
        elif kind == "avatar":
            n = sum(1 for c in range(MAX_CATEGORY_SCAN) if have(m, plan.image_key(int(iid), c, 3)))
            status = "%d animations of its body downloaded" % n
        else:
            layer = int(r["layerDefId"])
            flags = int(r["avatarUseFlags"])
            who = [a for a in avs if flags >> (a - 1) & 1]
            got = [a for a in who if any(have(m, plan.image_key(a, c, layer)) for c in (learned.get(a) or range(MAX_CATEGORY_SCAN)))]
            icon = "icon yes" if have(m, plan.image_key(1, 26, layer)) else "icon no"
            status = "downloaded for %d of %d avatars; %s" % (len(got), len(who), icon)
        print("  %-12s %-6s %-34s %s" % (kind, iid, name[:34], status))
    if len(rows) > limit:
        print("  ... %d more; narrow the search or raise --results" % (len(rows) - limit))


def keys_for(plan, tokens):
    """the file names behind a --has / --get request, as [(label, key)]"""
    q = parse_pairs(tokens)
    b = plan.build
    if "avatar" in q:
        av = int(q["avatar"])
        if "item" in q or "layer" in q:
            if "item" in q:
                row = {r["id"]: r for r in plan.d(1000).values()}.get(q["item"])
                if not row:
                    sys.exit("no item %s in defPack 1000" % q["item"])
                layers = [int(row["layerDefId"])]
            else:
                layers = [int(q["layer"])]
        else:
            layers = plan.avatar_layers().get(av)
            if layers is None:
                sys.exit("no avatar %d" % av)
        cats = [int(q["category"])] if "category" in q else (plan.load_cats().get(av) or list(range(MAX_CATEGORY_SCAN)))
        return [("avatar %d cat %d layer %d" % (av, c, l), plan.image_key(av, c, l)) for l in layers for c in cats]
    if "icon" in q:
        row = {r["id"]: r for r in plan.d(1000).values()}.get(q["icon"])
        if not row:
            sys.exit("no item %s" % q["icon"])
        layer = int(row["layerDefId"])
        return [("large icon", plan.image_key(1, 25, layer)), ("small icon", plan.image_key(1, 26, layer))]
    if "den" in q:
        row = {int(r["id"]): r for r in plan.d(1030).values()}.get(int(q["den"]))
        if not row:
            sys.exit("no den item %s" % q["den"])
        did = int(q["den"])
        out = [("den category %d" % c, "%s/items/%s" % (b, hash_name(str(did << 16 | c)))) for c in range(1, plan.a.den_cats + 1)]
        if row.get("abbrName"):
            out.append(("music", hashed(b, "audio/%s.mp3" % row["abbrName"])))
        return out
    if "media" in q:
        return [("media %s" % q["media"], hashed(b, "media/%s" % q["media"]))]
    if "room" in q:
        return [("room %s" % q["room"], hashed(b, "roomDefs/%s.xroom" % q["room"]))]
    if "audio" in q:
        return [("audio %s" % q["audio"], hashed(b, "audio/%s.mp3" % q["audio"].replace(".mp3", "")))]
    if "stream" in q:
        return [("stream %s.%s" % (q["stream"], e), hashed(b, "streams/%s.%s" % (q["stream"], e))) for e in ("mp3", "flv")]
    if "def" in q:
        return [("defPack %s" % q["def"], hashed(b, "defPacks/%s" % q["def"]))]
    if "shell" in q:
        return [(q["shell"], hashed(b, "assets/%s" % q["shell"]))]
    sys.exit("say what to get, e.g. --get avatar 8 item 276 | icon 276 | den 52 | media 100 | room party/venue_carnival")


def get_one(m, plan, tokens):
    todo = keys_for(plan, tokens)
    labels = {k: l for l, k in todo}
    already = [k for k in labels if have(m, k)]
    print("asking for %d file name%s (%d already here) ..." % (len(labels), "" if len(labels) == 1 else "s", len(already)))
    _, hits = m.run("get", labels.keys(), label="get", remember=False)
    got = sorted(labels[k] for k in hits)
    print("have now: %d of %d" % (len(got), len(labels)))
    if len(labels) <= 12:
        for k, l in labels.items():
            print("  %-30s %s" % (l, "HAVE  " + m.dest(k) if k in hits else "not on the CDN"))


def report(m, plan, verbose):
    build = plan.build
    print("output: %s   build %s\n" % (m.out, build))
    # defs
    nd = sum(1 for k in plan.keys_defs() if have(m, k))
    print("defs        %d present (68 names are tried; some do not exist on the CDN)" % nd)
    # image arrays: find which (avatar, category, layer) every file is
    imgdir = os.path.join(m.out, build, "imageArrays")
    present = set(os.listdir(imgdir)) if os.path.isdir(imgdir) else set()
    present = {f for f in present if re.fullmatch(r"[0-9a-f]{32}", f)}
    avs = sorted(int(k) for k in plan.d(1003))
    layers = sorted({int(r["id"]) for r in plan.d(1027).values()} | {0})
    base = set(plan.base_items())
    items = list(plan.d(1000).values())
    wear = collections.defaultdict(list)
    for r in items:
        if r["type"] == "1":
            wear[int(r["layerDefId"])].append(r["id"])
    found = {}
    if present:
        print("indexing %d image files ..." % len(present), flush=True)
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(os.cpu_count() or 1) as ex:
            for part in ex.map(_scan_avatar, [(av, layers, present) for av in avs]):
                found.update(part)
        for c in (25, 26):  # icons
            for l in layers:
                h = hash_name(str(pack_id(1, c, l)))
                if h in present:
                    found[h] = (1, c, l)
    per = collections.defaultdict(lambda: {"base": 0, "cloth": 0, "other": 0, "cats": set(), "items": set(), "layers": set()})
    icons = set()
    for h, (av, c, l) in found.items():
        if av == 1 and c in (25, 26):
            icons.add((c, l))
            continue
        d = per[av]
        d["cats"].add(c)
        if l in base:
            d["base"] += 1
        elif l in wear:
            d["cloth"] += 1
            d["layers"].add(l)
        else:
            d["other"] += 1
    tot_base = sum(d["base"] for d in per.values())
    tot_cloth = sum(d["cloth"] for d in per.values())
    print("avatars     %d files (bodies, eyes, patterns, sequences) for %d avatars" % (tot_base, sum(1 for d in per.values() if d["base"])))
    print("clothing    %d files for %d avatars, covering %d of %d clothing layers" % (
        tot_cloth, sum(1 for d in per.values() if d["cloth"]),
        len({l for d in per.values() for l in d["layers"]}), len(wear)))
    if per:
        print("  %-7s %6s %9s %11s  %s" % ("avatar", "base", "clothing", "layers", "animation categories"))
        for av in sorted(per):
            d = per[av]
            print("  %-7d %6d %9d %11d  %s" % (av, d["base"], d["cloth"], len(d["layers"]), spans(d["cats"])))
    print("icons       %d files (%d large, %d small) for %d layers" % (
        len(icons), sum(1 for c, _ in icons if c == 25), sum(1 for c, _ in icons if c == 26), len({l for _, l in icons})))
    print("unmatched   %d files in imageArrays are not any name this script knows" % (len(present) - len(found)))
    # den, audio
    den_rows = {int(r["id"]): r for r in plan.d(1030).values()}
    den_have = {}
    for did in den_rows:
        cs = [c for c in range(1, plan.a.den_cats + 1)
              if have(m, "%s/items/%s" % (build, hash_name(str(did << 16 | c))))]
        if cs:
            den_have[did] = cs
    print("den         %d of %d den items (%d files)" % (len(den_have), len(den_rows), sum(len(v) for v in den_have.values())))
    aud = sorted({r["abbrName"] for r in den_rows.values() if r.get("abbrName") and have(m, hashed(build, "audio/%s.mp3" % r["abbrName"]))})
    print("audio       %d music/sound tracks" % len(aud))
    med = [i for i in range(1, plan.a.media_max + 1) if have(m, hashed(build, "media/%d" % i))]
    print("media       %d of %d ids (%s)" % (len(med), plan.a.media_max, spans(med) if len(med) < 400 else "ids 1-%d with gaps" % plan.a.media_max))
    rooms = sorted({r["pathName"] for r in plan.d(1011).values() if r.get("pathName") and have(m, hashed(build, "roomDefs/%s.xroom" % r["pathName"]))})
    print("rooms       %d of %d" % (len(rooms), len({r.get("pathName") for r in plan.d(1011).values()})))
    shell = [n for n in ("HUDAssets.swf", "sharedFonts.swf") if have(m, hashed(build, "assets/" + n))]
    print("shell       %s" % (", ".join(shell) or "none"))
    st = sorted(i for i in range(0, 2000) if have(m, hashed(build, "streams/%d.mp3" % i)) or have(m, hashed(build, "streams/%d.flv" % i))) if os.path.isdir(os.path.join(m.out, build, "streams")) else []
    print("streams     %d" % len(st))
    if verbose:
        print("\n-- clothing items with at least one file, per avatar")
        for av in sorted(per):
            ids = sorted({int(i) for l in per[av]["layers"] for i in wear[l]})
            if ids:
                print("avatar %d: %s" % (av, spans(ids)))
        print("\n-- den items present")
        for did in sorted(den_have):
            print("  %5d %-34s categories %s" % (did, Names(plan).den(den_rows[did])[:34], spans(den_have[did])))
        print("\n-- audio: " + ", ".join(aud))
        print("\n-- rooms:\n  " + "\n  ".join(rooms))
        print("\n-- media ids: " + spans(med))


# ------------------------------------------------------------------ doctor
REQUIRED_DEFS = {1000: "items: clothing names and which animals can wear what",
                 1003: "animals: the list of avatars and their colours",
                 1027: "layers: which picture layer each item uses",
                 10230: "English names for items, den items and animals"}
STATE_FILES = ("misses-*.txt", "categories-*.json", "clothing-categories-*.json")


def forget_state(m):
    import glob
    n = 0
    for pat in STATE_FILES:
        for f in glob.glob(os.path.join(m.state, pat)):
            os.remove(f)
            n += 1
    print("removed %d bookkeeping files from %s (what was asked and answered 'not there'). Downloaded files were not touched." % (n, m.state))
    print("Run your download command again: every name will be asked about once more.")


def doctor(m, build):
    out = m.out
    print("cache folder: %s    build: %s" % (out, build))
    root = os.path.join(out, build)
    if not os.path.isdir(root):
        print("\nPROBLEM: there is no '%s' folder inside that cache folder." % build)
        print("  Use the folder that CONTAINS the '%s' folder (the one with %s/imageArrays/ inside it), or download first:" % (build, build))
        print("  python mirror_assets.py --types defs --out %s --build %s" % (out, build))
        return
    problems = []

    print("\n1. definition files the page needs to list animals, items and names")
    packs = {}
    for did, why in sorted(REQUIRED_DEFS.items()):
        path = os.path.join(out, *hashed(build, "defPacks/%d" % did).split("/"))
        if not os.path.isfile(path):
            print("   MISSING  defPack %-5d %s" % (did, why))
            problems.append("defs")
            continue
        try:
            packs[did] = read_def(path)
            print("   ok       defPack %-5d %s (%d entries)" % (did, why, len(packs[did])))
        except Exception as e:
            print("   BROKEN   defPack %-5d %s: %s" % (did, why, e))
            print("            (a download was probably cut off: delete that file, then download again)  %s" % path)
            problems.append("defs")
    pal = os.path.join(out, *hashed(build, "paletteData").split("/"))
    if os.path.isfile(pal) and os.path.getsize(pal) >= 1024:
        print("   ok       paletteData (the 256 game colours)")
    else:
        print("   MISSING  paletteData (the 256 game colours)")
        problems.append("defs")
    if "defs" in problems:
        print("   FIX:  python mirror_assets.py --types defs --out %s --build %s" % (out, build))
        print("         (if it still says missing afterwards, see failed-defs.txt in %s and the note about 403 above)" % m.state)

    print("\n2. pictures of each animal (body, eyes and pattern layers)")
    if not all(k in packs for k in (1000, 1003, 1027)):
        print("   cannot check yet: fix section 1 first")
    else:
        names = {str(k): v for k, v in packs.get(10230, {}).items() if isinstance(v, str)}
        items = {r["id"]: r for r in packs[1000].values()}
        slots = {int(r["id"]): int(r["layerId"]) for r in packs[1027].values()}
        body = min(i for i, sl in slots.items() if sl == 1)
        none, no_eyes, ok_count = [], [], 0
        for r in sorted(packs[1003].values(), key=lambda r: int(r["id"])):
            av = int(r["id"])
            cats = [c for c in range(MAX_CATEGORY_SCAN) if os.path.isfile(os.path.join(root, "imageArrays", hash_name(str(pack_id(av, c, body)))))]
            if not cats:
                none.append(av)
                continue
            ok_count += 1
            eyes = items.get(r.get("baseEyesId", "0"))
            if eyes:
                lay = int(eyes["layerDefId"])
                gaps = [c for c in cats if not os.path.isfile(os.path.join(root, "imageArrays", hash_name(str(pack_id(av, c, lay)))))]
                if gaps:
                    no_eyes.append((av, len(gaps), len(cats)))
        label = lambda a: "%d %s" % (a, names.get(packs[1003][str(a)]["titleStrRef"], "") if str(a) in packs[1003] else "")
        print("   %d of %d animals have pictures." % (ok_count, len(packs[1003])))
        if none:
            print("   NOTHING downloaded for: %s" % ", ".join(str(a) for a in none))
            print("   FIX:  python mirror_assets.py --types avatars --out %s --build %s%s" % (
                out, build, "" if len(none) > 12 else "".join(" --avatar %d" % a for a in none)))
            problems.append("avatars")
        if no_eyes:
            print("   missing their eyes in some animations: %s" % ", ".join("%d (%d of %d)" % x for x in no_eyes))
            print("   FIX:  python mirror_assets.py --types avatars --out %s --build %s%s" % (out, build, "".join(" --avatar %d" % x[0] for x in no_eyes[:12])))
            problems.append("eyes")

    print("\n3. what the downloader remembers")
    import glob
    misses = sum(sum(1 for _ in open(f)) for f in glob.glob(os.path.join(m.state, "misses-*.txt")))
    failed = {os.path.basename(f)[7:-4]: sum(1 for _ in open(f)) for f in glob.glob(os.path.join(m.state, "failed-*.txt"))}
    cats_empty = []
    cf = os.path.join(m.state, "categories-%s.json" % build)
    if os.path.isfile(cf):
        try:
            cats_empty = [k for k, v in json.load(open(cf)).items() if not v]
        except ValueError:
            pass
    print("   %d names are remembered as 'not on the CDN' (404)." % misses)
    for t, n in failed.items():
        print("   %d downloads of type '%s' failed last time (they are retried automatically): %s/failed-%s.txt" % (n, t, m.state, t))
    if cats_empty:
        print("   animals remembered as having NO animations: %s" % ", ".join(cats_empty))
        problems.append("state")
    if problems and ("avatars" in problems or "state" in problems):
        print("   If you downloaded while the CDN was refusing you, or you changed --build, the remembered answers can be wrong.")
        print("   Clear them (downloaded files stay) with:  python mirror_assets.py --forget-misses --out %s" % out)
    print("\nverdict: %s" % ("everything the page needs is here" if not problems else "see the FIX lines above"))


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--types", default="defs")
    ap.add_argument("--out", default="cache")
    ap.add_argument("--build", default="1830")
    ap.add_argument("--avatar", type=int, action="append", default=[])
    ap.add_argument("--category", type=int, action="append", default=[])
    ap.add_argument("--wide", action="store_true")
    ap.add_argument("--media-max", type=int, default=10000)
    ap.add_argument("--den-cats", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-defs-fetch", action="store_true")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--forget-misses", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--has", nargs="+", metavar="WORD")
    ap.add_argument("--find", nargs="+", metavar="WORD")
    ap.add_argument("--get", nargs="+", metavar="WORD")
    ap.add_argument("--results", type=int, default=30)
    return ap


def main():
    a = make_parser().parse_args()

    if a.doctor or a.forget_misses:
        m = Mirror(a.out, 1, create=False)
        forget_state(m) if a.forget_misses else doctor(m, a.build)
        return

    if a.report or a.has or a.find:
        a.no_defs_fetch = True  # looking only: never touches the network
        m = Mirror(a.out, 1, create=False)
        plan = Plan(m, a)
        if a.find:
            find(m, plan, a.find, a.results)
        elif a.has:
            lookup(m, plan, a.has)
        else:
            report(m, plan, a.verbose)
        return

    if a.get:
        m = Mirror(a.out, max(1, a.concurrency))
        plan = Plan(m, a)
        need = [1000, 1003, 1027, 1030, 1011]
        plan.need_defs(need)
        get_one(m, plan, a.get)
        return

    known = ("defs", "avatars", "clothing", "media", "rooms", "icons", "den", "audio", "shell", "streams")
    types = [t.strip() for t in a.types.split(",") if t.strip()]
    if "all" in types:
        types = list(known)
    bad = [t for t in types if t not in known]
    if bad:
        sys.exit("unknown type(s): %s (choose from %s, all)" % (", ".join(bad), ", ".join(known)))
    types = [t for t in known if t in types]

    m = Mirror(a.out, max(1, a.concurrency))
    plan = Plan(m, a)
    print("output: %s   build: %s" % (m.out, a.build))

    needs_defs = {"avatars": [1003, 1027], "clothing": [1000, 1003], "rooms": [1011], "icons": [1027],
                  "den": [1030], "audio": [1030], "streams": [1053]}
    wanted = sorted({i for t in types for i in needs_defs.get(t, [])})
    if wanted:
        plan.need_defs(wanted)

    cats = {}
    if any(t in types for t in ("avatars", "clothing")):
        if a.dry_run:
            cats = plan.load_cats()
            missing = [x for x in plan.avatars() if x not in cats]
            if missing:
                print("(dry run: %d avatars have no learned categories yet; assuming 25 each)" % len(missing))
                for x in missing:
                    cats[x] = list(range(25))
        else:
            cats = plan.discover(plan.avatars())

    if "clothing" in types and not a.no_prune:
        if a.dry_run:
            plan.cloth_cats = plan.load_cloth_cats() or None
        else:
            plan.cloth_cats = plan.learn_clothing_cats(cats)

    streams = {
        "defs": plan.keys_defs,
        "avatars": lambda: plan.keys_avatars(cats),
        "clothing": lambda: plan.keys_clothing(cats),
        "media": plan.keys_media,
        "rooms": plan.keys_rooms,
        "icons": plan.keys_icons,
        "den": plan.keys_den,
        "audio": plan.keys_audio,
        "shell": plan.keys_shell,
        "streams": plan.keys_streams,
    }
    for t in types:
        if a.dry_run:
            n = have = 0
            for k in streams[t]():
                n += 1
                have += os.path.exists(m.dest(k))
            print("%-9s %9d names to ask for (%d already in the output folder)" % (t, n, have))
            continue
        print("== %s" % t)
        m.run(t, streams[t](), limit=a.limit)
    if a.dry_run:
        print("\nOnly a share of the names exist on the CDN (the rest answer 404). "
              "Sample on avatar 8: about 3 in 4 clothing names exist, ~2.8 KB each.")


if __name__ == "__main__":
    main()
