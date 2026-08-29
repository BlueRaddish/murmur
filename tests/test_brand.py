"""Run: python tests/test_brand.py  — no framework, asserts only.

The mark is drawn from geometry with PIL but specified as an SVG path, so the load-bearing check
is that the two agree: a browser rasterises the path, PIL draws the numbers, and the ink must
overlap. Everything else here guards the places where a "small enough" render stops being legible.
"""
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import brand
import murmur

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def ink(img):
    """Dark pixels of an RGB image, as a flat list of 0/1 — the mask both renderers can be
    compared on regardless of antialiasing."""
    return [1 if v < 128 else 0 for v in img.convert("L").get_flattened_data()]


# --- 1. the PIL render is the SVG path -------------------------------------------------------
if CHROME.exists():
    d = Path(tempfile.mkdtemp())
    svg = brand.svg_mark("#000000").replace("viewBox", 'width="256" height="256" viewBox')
    (d / "m.html").write_text(f'<html><body style="margin:0;background:#fff">{svg}</body></html>',
                              encoding="utf-8")
    subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--window-size=256,256",
                    f"--screenshot={d / 'm.png'}", str(d / "m.html")],
                   capture_output=True, timeout=180)
    shot = Image.open(d / "m.png").convert("RGB")
    flat = Image.new("RGB", (256, 256), (255, 255, 255))
    m = brand.mark(256, "#000000")
    flat.paste(m, (0, 0), m)
    a, b = ink(shot), ink(flat)
    inter = sum(1 for x, y in zip(a, b) if x and y)
    union = sum(1 for x, y in zip(a, b) if x or y)
    assert union and inter / union >= 0.985, f"mark(256) vs svg_mark() IoU {inter / union:.4f}"
    print(f"svg/pil agree: IoU {inter / union:.4f}")
else:
    print("skipped svg/pil IoU: Chrome not at", CHROME)

# the tray render is a pixel grid, not a photo: no half-lit edges, legs on whole columns
small = brand.mark(16, "#000000")
alpha = small.getchannel("A")
assert set(alpha.get_flattened_data()) <= {0, 255}, "16 px mark is antialiased"
px = alpha.load()
row = 11                                     # below both spring lines: only the three legs
assert [x for x in range(16) if px[x, row]] == [2, 3, 6, 7, 12, 13], \
    [x for x in range(16) if px[x, row]]
assert not any(px[x, 13] for x in range(16)), "ink below the baseline"
print("16 px mark ok: whole pixels, legs at 2-3 / 6-7 / 12-13")

# --- 2. save_ico round-trip ------------------------------------------------------------------
SIZES = (16, 24, 32, 48, 64, 128, 256)
ico = Path(tempfile.mkdtemp()) / "murmur.ico"
brand.save_ico(ico)
im = Image.open(ico)
assert sorted(im.info["sizes"]) == [(s, s) for s in SIZES], sorted(im.info["sizes"])
for s in SIZES:
    im.size = (s, s)
    f = im.convert("RGBA")
    p = f.load()
    # a point in the bottom-left ground below the baseline, and (where the tile uses the 208/256
    # box) one on the tall left leg: white-on-green means the frame was drawn, not resampled to soup
    if s > 32:
        assert p[round(0.246 * s), round(0.594 * s)] == (255, 255, 255, 255), (s, "leg not white")
    assert p[round(0.12 * s), round(0.90 * s)] == brand._rgb(brand.GREEN) + (255,), (s, "ground")
    assert p[0, 0][3] == 0, (s, "corner not rounded")

# the point of a frame per size: at 16 and 32 the mark sits on whole pixels - three legs of equal
# width, pure white, no half-lit column. (A 13 px inner box at 16 gave legs 2/2/1 and a ragged
# baseline, which is a frame per size buying nothing.)
for s, legs in ((16, [2, 3, 6, 7, 12, 13]), (32, [4, 5, 6, 7, 12, 13, 14, 15, 24, 25, 26, 27])):
    im.size = (s, s)
    p = im.convert("RGBA").load()
    row = round(11 / 16 * s)                 # below both spring lines: only the three legs
    got = [x for x in range(s) if p[x, row][:3] == (255, 255, 255)]
    assert got == legs, (s, "legs", got)
    assert all(p[x, row] == (255, 255, 255, 255) for x in legs), (s, "legs not opaque white")
print("ico 16/32 frames ok: legs on whole pixels, 2 and 4 columns wide")

im.size = (16, 16)
w16 = sum(1 for q in im.convert("RGBA").get_flattened_data() if q[:3] == (255, 255, 255))
assert w16 >= 20, f"16 px ico frame has only {w16} white pixels"
print(f"ico ok: {len(SIZES)} sizes, 16 px frame has {w16} white pixels")

# --- 3. tray_color / make_icon ---------------------------------------------------------------
cfg = {"color": "#00ff00", "color_busy": "#e63c3c"}
assert murmur.tray_color("idle", cfg) == (139, 144, 139)
assert murmur.tray_color("recording", cfg) == (0, 255, 0)
assert murmur.tray_color("persistent", cfg) == (0, 255, 0)      # mic open = accent, as on the bar
assert murmur.tray_color("busy", cfg) == murmur.tray_color("loading", cfg) == (230, 60, 60)
junk = {"color": "not a colour", "color_busy": None}
assert murmur.tray_color("recording", junk) == (220, 50, 50)     # COLORS["recording"]
assert murmur.tray_color("busy", junk) == (255, 170, 50)         # COLORS["busy"]

# at the shipped defaults idle, mic-open and transcribing must be three different icons: 16 px of
# mark a few units apart is one colour, and "the mic is open" reading as "transcribing" costs
STATES = ["idle", "recording", "busy"]
for i, a in enumerate(STATES):
    for b in STATES[i + 1:]:
        ca, cb = murmur.tray_color(a, murmur.DEFAULTS), murmur.tray_color(b, murmur.DEFAULTS)
        assert max(abs(x - y) for x, y in zip(ca, cb)) > 24, (a, b, ca, cb)
for state in murmur.LABELS:
    icon = murmur.make_icon(state, cfg)
    assert icon.mode == "RGBA" and icon.size == (64, 64), (state, icon.mode, icon.size)
    want = murmur.tray_color(state, cfg)
    seen = {q[:3] for q in icon.get_flattened_data() if q[3]}
    assert seen == {want}, (state, want, sorted(seen)[:4])
print("tray_color ok for", ", ".join(murmur.LABELS))

# --- 4. the SVGs are real SVGs ---------------------------------------------------------------
for name, text in (("mark", brand.svg_mark()), ("icon", brand.svg_icon()), ("lockup", brand.svg_lockup())):
    ET.fromstring(text)                       # raises on malformed XML
    assert brand.MARK_64 in text, f"{name} does not carry the rising path"
assert ">murmur<" in brand.svg_lockup(), "lockup lost its wordmark"
assert brand.GREEN in brand.svg_lockup() and brand.GREEN in brand.svg_icon()
# what --assets writes: an <img> cannot inherit a colour or see GitHub's theme toggle, so the
# committed lockup must not rely on either
lk = brand.svg_lockup(text_color=brand.GREEN)
assert "currentColor" not in lk and "prefers-color-scheme" not in lk, lk
print("svgs ok")

print("all brand tests passed")
