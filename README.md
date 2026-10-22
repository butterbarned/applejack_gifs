# Asset tools

Four standalone Python scripts (standard library only; Python 3.8+) for downloading Animal Jam
Classic assets from the official CDN and turning them into pictures. They do not need the server,
Node or its `lib/` folder, so they can be copied to any machine.

| Script | Does |
|---|---|
| `mirror_assets.py` | Downloads assets from the CDN; reports and searches what you have |
| `composite_avatar.py` | Builds avatar animation PNGs (with clothing), icons, raw layers |
| `decode_imagearray.py` | Decodes single imageArray files to PNG frames (coloured or raw) |
| `extract_den_items.py` | Unwraps downloaded den items to `.swf` and PNG (needs ffdec) |
| `asset_gui.py` | A local web page: a form for every script above, plus a GIF maker |
| `anim_encode.py` | GIF / APNG writers used by the GIF maker (no install needed) |

Files to copy to another machine: all six `.py` files, in one folder. `composite_avatar.py`
imports `decode_imagearray.py` and `mirror_assets.py`; `extract_den_items.py` imports
`mirror_assets.py`; `asset_gui.py` imports `anim_encode.py` plus the three above.

---

## The web page (asset_gui.py)

```
python asset_gui.py                         opens your browser
python asset_gui.py --cache cache --build 1830 --no-browser --port 8765
```

* **One tab per script.** The form for each script is built from that script's own options
  (its `make_parser()`), so a new or renamed option shows up the next time you load the page, with
  its help text, dropdown choices, switches and repeatable fields. Any other `.py` file in the same
  folder that has a `make_parser()` gets a tab automatically. Run starts the script, shows its
  output live and has a Stop button; the exact command line is shown above the output. Anything
  the form cannot express goes in "Extra arguments (raw)". Form values are remembered per browser.
* **GIF maker tab.** Pick the avatar, animation, the colours of the body / pattern / eyes (four
  colours each; click a swatch to choose from the game's 256-colour palette or any colour) and
  clothing (search by name; each item can be recoloured), then watch it animate. Export as
  **GIF** (Pillow if it is installed, otherwise a built-in encoder: 256 colours, 1-bit
  transparency), **APNG** (full quality, any machine) or a **zip of PNG frames**. Options: face
  the other way, size, frame time, loop, transparent or solid background. It uses
  `composite_avatar.py`, so changes to that file are picked up without a restart.
* **Downloads what is missing.** With "download missing files automatically" ticked (the default),
  picking an avatar with nothing downloaded fetches its animations, and adding an item fetches that
  item's art for the chosen animation, right then. Only names worked out from the game's own
  definitions are requested (never a typed-in address); names the CDN does not have are remembered
  in `<cache>/.mirror/misses-gui.txt` and the page tells you ("not on the CDN for this
  animation"). Untick the box to stay offline; the page then only uses what is already downloaded.
* **Keep your look across avatars.** Tick "keep clothes and colours when changing avatar" and the
  clothing you added and the colours you changed carry over when you pick another avatar (items the
  new avatar cannot wear are left out, and the page says which). Unticked, a new avatar starts fresh.
* **Reset, undo and redo.** The buttons at the top of the GIF maker: **reset colours** (back to the
  avatar's and each item's own colours), **remove all clothing**, and **Undo / Redo** (up to 25 steps
  each way; also Ctrl+Z, and Ctrl+Y or Ctrl+Shift+Z). A burst of quick edits, such as dragging a
  colour, counts as one step, and changing avatar is one step too. History starts fresh when the page
  loads or the cache folder changes.
* **Colours are Burrow colour codes.** Burrow (like the game) stores a colour set as **four palette
  numbers (0-255) packed into one number**, first slot in the top byte:
  `code = n1*16777216 + n2*65536 + n3*256 + n4` (so palette numbers `0 0 255 1` are code `65281`).
  Avatars have three such numbers (body, pattern, eyes); every clothing item's `colors` is a list of
  them. Each colour row has "codes…": the **Burrow code**, the four **palette numbers**, and
  **hex codes** all edit the same set, and "copy code" gives the number to paste into Burrow's
  tools or item colours. Clicking a swatch opens the game's 256-colour palette (hover for the number).
  Hex codes that are exact game colours become a palette set; anything else (or a colour picked with
  the free colour picker) makes a **custom RGB set**, shown in red as "custom RGB": Burrow stores
  those on the server as a numbered custom colour (ids from `0xFD000000`, see `lib/colours.js`), so
  it has no code of its own here; "snap to game palette" turns it into the closest normal code.
  A code in that custom range cannot be read (the colours live on the server) and is refused with a
  message, and palette number 253 is refused for the first slot because it would look like one.
* **Pattern and eyes.** A "Pattern and eyes" card lists the patterns and eyes the chosen avatar can
  use (from the game's own item list; the avatar's own is shown as "(default)"). Their colours are
  the Pattern and Eyes colour rows.
* **Saved colour sets** keep the four numbers (or the custom RGB values) under a name; the earlier
  list-of-hex saves still load and become palette sets where the colours match exactly.
* **Save and share looks.** The "5 · Save and share" card shows a **share code**: one line of
  text (starts with `ACT1.`) holding the whole look: avatar, animation, flip, size, speed, loop,
  background, export format, the three colour sets (as palette numbers where possible) and every worn
  item with its colours; pattern and eyes choices are included. (Share codes made by the first version
  still load.)
  Paste it into chat; whoever has the page pastes it into "Load a look from a code" and gets the
  same animation (missing files download automatically if that option is on). **Saved outfits**
  keep named looks on this computer. A loaded look is checked first: unknown avatars or items,
  malformed colours and codes from a newer version are rejected with a message, and items that do
  not fit the avatar are loaded but flagged.
* **Where saves live.** `asset-gui-saves/saves.json` next to `asset_gui.py` (change with
  `--saves DIR`). It is plain JSON, so you can back it up, copy it to another machine or edit it.
* **Safety.** It listens on 127.0.0.1 only and every request needs the random token in the URL it
  prints (plus a matching Host header), so other websites and other computers cannot use it. It only
  runs the scripts found in its own folder, as normal programs (no shell).
* The scripts run in the folder you started the page from (shown at the top), so relative paths
  such as `cache` mean the same thing as on the command line. Stop the page with Ctrl+C.

---

## Hosting the GIF maker for other people

`python asset_gui.py --public` is an internet-facing mode (GIF maker only: no script runner, no cache-folder changes,
no server-side saves, no downloads, rate limits and a render queue). `deploy/DEPLOY.md` is a step-by-step guide for an
Oracle (or any Ubuntu) server with Caddy for HTTPS and automatic updates from GitHub, including a rollback if a
new version fails. The page can also be hosted on GitHub Pages while the server only does the drawing (`site/build.py`,
`.github/workflows/pages.yml`, section 9 of the guide). The private page described below is still the default and must never be exposed directly.

---

## Quick start

```
python mirror_assets.py --types defs               # 1. definitions (names, ids). Always first.
python mirror_assets.py --types avatars,icons      # 2. animals and shop icons
python mirror_assets.py --get avatar 8 item 2346   # 3. or just one thing
python composite_avatar.py --avatar 8              # 4. make PNGs
```

Everything lands in `./cache` (override with `--out`), laid out like the server's own cache:
`cache/1830/imageArrays/<hash>`, `cache/1830/defPacks/<hash>`, and so on. File names are hashes;
the scripts work out the right names from the definitions, so you never type a hash.

---

## mirror_assets.py: downloading

The CDN no longer lets anyone list its files, so the script works out exact file names from the
definition packs and asks for each. Names that do not exist answer 404 and are remembered in
`<out>/.mirror/`, so re-runs only ask about what is new. Stop with Ctrl+C and run the same
command again to resume. Nothing is ever deleted or overwritten.

### Types (`--types a,b,c` or `--types all`)

| Type | What it is |
|---|---|
| `defs` | Definition packs and the colour palette (small; fetched automatically when needed) |
| `avatars` | Animal bodies, eyes, patterns and animation-order files, every animation |
| `clothing` | Every wearable item's layer, per avatar allowed to wear it, per animation (**the big one**) |
| `icons` | Shop / dress-up / inventory icons, large and small |
| `den` | Den item art (SWF wrappers; see `extract_den_items.py`) |
| `audio` | Den music and sound items (MP3) |
| `media` | `media/<id>` files: SWFs for icons, cards, wallpapers, UI art, more |
| `rooms` | `.xroom` room files; minigame scenes are rooms too |
| `shell` | `HUDAssets.swf`, `sharedFonts.swf` |
| `streams` | Videos and spoken clips |

Not covered: the game client itself (`ajclient.swf`), masterpieces, subtitles, poll/feedback defs
(their names have not been worked out).

### Download one specific thing (`--get`)

```
--get avatar 8 item 276                every animation of that clothing item for avatar 8
--get avatar 8 item 276 category 7     one animation only
--get avatar 8                         that avatar's body / eyes / pattern files
--get avatar 8 layer 152               a layer id directly (0 = the animation-order file)
--get icon 2346        --get den 52        --get media 44
--get room party/venue_carnival        --get audio NAME        --get stream 12
```

Item ids: find them by name with `--find` (below).

### Something missing, or errors about missing files?

```
python mirror_assets.py --doctor --out cache      checks everything the GIF maker needs and prints the exact fix command
python mirror_assets.py --forget-misses --out cache   forgets "not on the CDN" answers (downloaded files stay)
```
To list animals, items and names the page needs five small files (definition packs 1000, 1003, 1027 and 10230, and
`paletteData`: all fetched by `--types defs`), plus, for each animal you want to pick, its body, eyes and pattern pictures
(`--types avatars`). The cache folder you give the page or the scripts must be the one that **contains** the `1830` folder.
The doctor reports which of these is missing, damaged or wrongly remembered as absent.

### What do I have? (no network)

```
--report                  counts per type, a per-avatar table, files it does not recognise
--report --verbose        also every clothing item id, den item name, track and room
--find fox hat            search items, den items and avatars BY NAME (English); shows whether
                          each is downloaded. --results N for more than 30 matches
--has avatar 8 item 276   is it here? which animations are present / missing?
--has icon 2346 | den 52 | media 44 | room PATH | audio NAME | stream N | def N | shell NAME
```

`--has` takes the same words as `--get`. `key value` and `key=value` both work.

### Other options

| Option | Meaning |
|---|---|
| `--out DIR` | Output folder (default `./cache`) |
| `--build N` | CDN build folder (default `1830`) |
| `--avatar N` / `--category N` | Narrow `avatars` / `clothing` (repeatable) |
| `--wide` | Ask every avatar about every animation any avatar has (slower, more complete) |
| `--concurrency N` | Parallel connections (default 16; try 32 if it is slow) |
| `--dry-run` | Count the names it would ask for; downloads nothing but the small defs |
| `--limit N` | Stop after N requests per type (for a test run) |
| `--no-prune` | Ask about everything, including names likely to 404 (see below) |
| `--media-max N` | Highest media id to try (default 10000; the defs use ids up to 9428) |
| `--den-cats N` | Den categories to try, 1..N (default 8; found: 1-3) |
| `--no-defs-fetch` | Never download definition packs |

### How big is it? (measured / estimated)

| Type | Files | Size |
|---|---|---|
| defs | ~66 | <1 MB |
| avatars | ~15-30k | ~70 MB |
| clothing | up to ~1M | ~3 GB (sampled, not fully downloaded) |
| icons | ~2.3k | 12 MB |
| den | ~6.0k | 62 MB |
| audio | 54 | 48 MB |
| media | ~10k | ~210 MB |
| rooms | 415 | 346 MB |
| shell / streams | 2 / 62 | 1 MB / 92 MB |

A million small files take roughly 4 GB of disk. The server's cache limit is 8 GB
(`AJ_MAX_CACHE_MB`) if you point `--out` at the server's `cache/`.

### Speed and 404s

* Typical speed here was 60-80 requests per second. If it is far slower, look at the status
  line: it prints `THROTTLED n` when the CDN answers 429/5xx (lower `--concurrency`).
* By default the script skips requests likely to 404: eye/pattern layers an avatar cannot use,
  and animations in which sampling found (almost) no clothing. Sampling keeps an animation when
  at least 3 of 12 sample layers exist in it, so an animation used by only a few layers could be
  skipped; use `--no-prune` to be exhaustive.
* Animation categories per avatar are learned by asking for the avatar's body files over 0..63
  (cached in `.mirror/categories-*.json`). Delete `.mirror/` to ask again from scratch.

---

## composite_avatar.py: avatar pictures

Needs a cache with `defs` and the avatar's images (`--types defs,avatars`, plus `clothing` /
`icons` if you use those).

```
python composite_avatar.py --list-avatars
python composite_avatar.py --avatar 8                       every animation, default look
python composite_avatar.py --avatar all
python composite_avatar.py --avatar 8 --category 7          one animation
python composite_avatar.py --avatar 8 --wear 2346           wear a clothing item
python composite_avatar.py --avatar 8 --wear 2346 --wear 1500=ff0000,00ff00,0000ff,ffffff
python composite_avatar.py --avatar 8 --color 1=d9a05b,f4e3c1,8a5a2b,333333    recolour a slot
python composite_avatar.py --avatar 8 --hflip               mirrored  (--both-flips for both)
python composite_avatar.py --find-item 2346                 layer, slot, colours, who can wear it
python composite_avatar.py --icon 2346                      item icons (--icon all is slow)
python composite_avatar.py --avatar 8 --raw                 no colours (see below)
```

Output: `avatar-out/avatar<A>_cat<C>[_wear<ids>][_flip][_raw]/frameNN.png` plus `meta.json` (frame
order, each frame's top-left in the canvas). Options: `--cache DIR`, `--build N`, `--out DIR`,
`--scale N` (default 2; the game stores art at half size).

How a look is built: body layer (slot 1) + the avatar's own eyes and pattern, then `--wear` items,
each into the slot of its layer (replacing what was there); slots stack in order. Slots 1-3 use the
avatar's three default colours; a worn item uses its own default colour (the first of its
`colors` list; the game uses the colour the player picked). Mixing of semi-transparent pixels,
shading and flipping follow the game's `LayerBitmap.paintFrame`.

### Raw mode (`--raw [gray|rainbow]`)

No avatar colours. Writes a false-colour stack (one hue per layer slot, brightness = stored
palette index) and, under `layers/slotS/`, each slot's exact stored values as an indexed PNG, all
on one shared canvas so they line up pixel for pixel. Item colours, `--wear` colours and `--color`
are ignored.

---

## decode_imagearray.py: single files

```
python decode_imagearray.py cache/1830/imageArrays --out frames
python decode_imagearray.py FILE --colors d9a05b f4e3c1 8a5a2b 333333     # hex, no '#'
python decode_imagearray.py cache/1830/imageArrays --raw --out raw-frames # exact stored values
```

Decodes one file or a whole folder (compressed or not; non-imageArray files are skipped and
reported). Colours are not stored in the files: the game builds a 25-colour palette from four base
colours at run time, so without `--colors` four demo colours are used. `--raw [gray|rainbow]`
writes indexed PNGs of the exact stored values (pixel = palette index + 25 x transparency step),
which you can recolour later by editing the PNG palette. Other options: `--palette FILE`,
`--scale`, `-r` (sub-folders), `--force`, `--no-shadow`, `--keep-duplicate-frames`,
`--any-version`. A `manifest.json` in `--out` records what was done and keeps re-runs from
repeating work; use a fresh `--out` folder when you change colours or mode.

The file names in the cache are hashes. To know which avatar/animation/layer a file is, use
`mirror_assets.py --has/--report`, or `composite_avatar.py`, which reads names for you.

---

## extract_den_items.py: den items

Den items are small wrapped SWF movies. This unwraps them and renders PNGs.

```
python mirror_assets.py --types defs,den
python extract_den_items.py                         everything
python extract_den_items.py --item 52 --item 617    only these den item ids
python extract_den_items.py --no-render             just write the .swf files
python extract_den_items.py --ffdec "C:\Program Files (x86)\FFDec\ffdec-cli.exe" --jobs 4
```

Needs [JPEXS ffdec](https://github.com/jindrapetrik/jpexs-decompiler) and Java for the PNGs
(not for `--no-render`); Pillow makes the trimming faster but is optional. Output in `den-out/`:
`swf/<id>_c<cat>_<name>.swf` (exactly what the CDN sent), `png/...png` (transparent, trimmed;
animated items give `_f01`, `_f02`, ... all trimmed to the same box so they line up) and
`meta.json` (the `cx`, `cy`, `so`, `r` placement values, copied untouched; their meaning is not
decoded). About 40 minutes for all ~6,000 files (estimated from a 60-file test).

---

## Things worth knowing

* **Names:** English item, den item and avatar names come from the localization pack
  (defPack 10230). Fetch it with `--types defs` on a new machine.
* **Item ids vs layer ids:** an item (defPack 1000 id, what `--find` shows) points at a layer
  def; several items can share one layer (recolours). Image files are keyed by avatar, animation
  category and layer def.
* **Not decoded / not done:** media and room files (SWFs: the `extract_den_items.py` approach
  would work, not yet written); den item placement values; the 375/365 pixel offset the game
  adds when placing an avatar (recorded in `meta.json`, not applied to the PNGs).
* **Unverified:** `--hflip` mirroring against the game; clothing colours when a player has picked
  a non-default colour; clothing downloads for avatars other than 8 (pruning was only tested
  on avatar 8).
* The CDN is the official game's; use these for personal/archival work, keep concurrency modest.
