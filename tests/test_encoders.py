#!/usr/bin/env python3
"""Checks the animated-image writers without any game files. Needs Pillow only to read the results back.
   python tests/test_encoders.py"""
import io, os, random, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import anim_encode as E
from PIL import Image


def frames(n=6, w=40, h=30):
    random.seed(1)
    out = []
    for f in range(n):
        px = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                if (x - 5 * f - 10) ** 2 + (y - 15) ** 2 < 60:
                    px[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes((200, 30 * f, 90, 255))
        out.append(bytes(px))
    return out, w, h


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


fr, w, h = frames()
apng = E.apng_encode(fr, w, h, 80)
im = Image.open(io.BytesIO(apng))
check(im.n_frames == len(fr) and im.size == (w, h), "APNG has all %d frames at the right size" % len(fr))
im.seek(3)
check(im.convert("RGBA").tobytes() == fr[3], "APNG frame 3 is pixel-exact")

for pillow in (False, True):
    gif = E.gif_encode(fr, w, h, 80, pillow=pillow)
    g = Image.open(io.BytesIO(gif))
    check(g.n_frames == len(fr) and g.size == (w, h), "GIF (%s encoder) has all frames" % ("Pillow" if pillow else "built-in"))

# the built-in LZW coder must be lossless, including when its code table fills up and resets
random.seed(2)
cols = [(random.randrange(256), random.randrange(256), random.randrange(256)) for _ in range(200)]
W, H = 300, 300
px = bytearray()
for y in range(H):
    for x in range(W):
        px += bytes(cols[(x * 7 + y * 13 + random.randrange(3)) % 200]) + b"\xff"
back = Image.open(io.BytesIO(E.gif_encode([bytes(px)], W, H, 100, pillow=False, transparent=False))).convert("RGBA").tobytes()
check(back == bytes(px), "built-in GIF encoder is lossless on a noisy 200-colour image")
print("all encoder tests passed")
