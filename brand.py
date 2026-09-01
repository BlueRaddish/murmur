"""murmur's mark, drawn from geometry - the app ships no image assets.

The mark is the "Ripple m", rising build: two half-annuli springing from one line and standing on
one baseline, the small arch's top four units below the big one's, its right leg sitting exactly
on the big arch's left leg. The union reads as an m and as two ripples going out - a quiet sound
made visible, which is the whole product. (The level build, both tops at y=12, shipped for a day
on 2026-08-27 by my misreading; the rising one is what the user picked from the brief.)

Everything visual comes out of the four numbers in _G64/_G16, so the tray icon, the app icon, the
window lockup and the README all draw the same shape and there is nothing on disk to keep in sync.
Small sizes get their own coarser grid (_G16) rather than a shrunk _G64: a 16 px icon that is
merely downsampled turns into grey mush next to the crisp system icons beside it. The tray is the
one place that cannot have that - pystray writes its own ICO from whatever single image it is
handed - and it is handed mark(64) anyway, because on a 200% display Windows asks LoadImage for a
64 px frame and an icon that stops at 32 gets upscaled instead.

CLI:  python brand.py --ico murmur.ico      python brand.py --assets assets
"""
from PIL import Image, ImageDraw

GREEN = "#1f9a3a"          # brand green: the icon tile's ground, the lockup's mark

MARK_64 = ("M8 52 L8 28 A12 12 0 0 1 32 28 L32 52 L24 52 L24 28 A4 4 0 0 0 16 28 L16 52 Z "
           "M24 52 L24 28 A16 16 0 0 1 56 28 L56 52 L48 52 L48 28 A8 8 0 0 0 32 28 L32 52 Z")
# (centre x, spring-line y, inner radius, outer radius) per arch, plus the shared baseline.
# MARK_64 is _G64 written out; keep the two in step.
_G64 = ((20, 28, 4, 12), (40, 28, 8, 16))
_G16 = ((5, 7, 1, 3), (10, 7, 2, 4))
_BASE64, _BASE16 = 52, 13


def _rgb(color):
    """'#rrggbb' / '#rgb' / (r, g, b) -> (r, g, b). Anything unparseable is black, not a crash:
    a bad colour in config must still leave a visible tray icon."""
    if isinstance(color, (tuple, list)):
        return tuple(int(c) for c in color[:3])
    h = str(color).strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return (0, 0, 0)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def _mask(size, ss=4, small=None):
    """Alpha mask (mode L) of the mark at `size` px, drawn at `ss`x and reduced.

    Per arch: a top-half disc of the outer radius, the inner-radius disc taken back out, then the
    two legs from the spring line down to the baseline. Each arch is composited as a unit so the
    big arch's hole can never bite into the small arch's ink where the two overlap.
    """
    if small is None:
        small = size <= 24
    arches, base, unit = (_G16, _BASE16, 16) if small else (_G64, _BASE64, 64)
    s = size * ss / unit                      # user units -> supersampled device px
    w = size * ss
    m = Image.new("L", (w, w), 0)
    for cx, cy, ri, ro in arches:
        a = Image.new("L", (w, w), 0)
        d = ImageDraw.Draw(a)
        # PIL's box coordinates are inclusive, hence the -1 on the far edges
        d.pieslice([(cx - ro) * s, (cy - ro) * s, (cx + ro) * s - 1, (cy + ro) * s - 1], 180, 360, fill=255)
        d.pieslice([(cx - ri) * s, (cy - ri) * s, (cx + ri) * s - 1, (cy + ri) * s - 1], 180, 360, fill=0)
        for x0 in (cx - ro, cx + ri):         # legs last: the hole must not eat them
            d.rectangle([x0 * s, cy * s, (x0 + ro - ri) * s - 1, base * s - 1], fill=255)
        m.paste(a, (0, 0), a)                 # union
    out = m.reduce(ss)
    if size <= 16:
        # at tray size the grid is the design: a half-lit edge pixel reads as a smudge, so snap.
        out = out.point(lambda v: 255 if v >= 128 else 0)
    return out


def mark(size, color=GREEN, ss=4):
    """The m at `size` px as RGBA, keeping the SVG's own margins (8/64 sides, 12/64 top+bottom)."""
    img = Image.new("RGBA", (size, size), _rgb(color) + (0,))
    img.putalpha(_mask(size, ss))
    return img


def tile(size, bg=GREEN, fg="#ffffff", ss=4):
    """The app icon: rounded square in `bg` with the mark in `fg`, laid out as svg_icon() does."""
    plate = Image.new("RGBA", (size, size), _rgb(bg) + (255,))
    r = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(r).rounded_rectangle([0, 0, size * ss - 1, size * ss - 1],
                                        radius=56 / 256 * size * ss, fill=255)
    plate.putalpha(r.reduce(ss))
    # <= 32 px the mark takes the whole tile and its own margins (2/16 at the sides, 3/16 top and
    # bottom) are the padding: the 208/256 box lands between pixels there - 13 px at 16 gave legs
    # 2/2/1 px wide and a baseline at y=11.56 - and a mark on half pixels is the mush the coarse
    # grid exists to avoid. Those sizes also force the coarse grid: the size <= 24 rule in _mask
    # goes by the mark box, which used to be smaller than the tile.
    small = size <= 32
    inner = size if small else max(1, round(208 / 256 * size))
    m = Image.new("RGBA", (inner, inner), _rgb(fg) + (0,))
    m.putalpha(_mask(inner, ss, small=True if small else None))
    plate.paste(m, (0, 0) if small else (round(24 / 256 * size), round(22 / 256 * size)), m)
    return plate


def save_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """Write a real multi-image .ico. Pillow's ICO writer resamples ONE image down to every size,
    which is exactly what makes the 16 px entry mush - so hand it a purpose-drawn image per size
    via append_images (it uses a provided frame whose size matches, and only resamples otherwise).
    The base image must be the largest: sizes above it are skipped."""
    sizes = sorted(set(sizes), reverse=True)
    frames = [tile(s) for s in sizes]
    frames[0].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[1:])


# --- SVG (for the README and anywhere that wants vectors) -----------------------------------
def svg_mark(color="currentColor"):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="%s" '
            'aria-label="murmur"><path d="%s"/></svg>' % (color, MARK_64))


def svg_icon(size=256, bg=GREEN, fg="#ffffff"):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="%d" height="%d" '
            'aria-label="murmur"><rect width="256" height="256" rx="56" ry="56" fill="%s"/>'
            '<path transform="translate(24 22) scale(3.25)" fill="%s" d="%s"/></svg>'
            % (size, size, bg, fg, MARK_64))


def svg_lockup(mark_color=GREEN, text_color="currentColor"):
    """Mark + wordmark. scale 0.7 puts the mark at 28 units - 1.35x the wordmark's x-height - with
    its three feet on the text baseline (y=42) and a 14-unit gap, half the mark's height.

    The default text colour inherits, which is what an inline <svg> wants. An <img> - the README -
    has nothing to inherit from and cannot see its host's theme either: the SVG is a separate
    document, so prefers-color-scheme reads the reader's OS, not GitHub's theme toggle, and a
    dark-theme reader on a light-mode OS gets #16221a on #0d1117. So the committed asset asks for a
    fixed ink instead (main() passes GREEN): no mid-tone clears 4.5:1 on both #ffffff and #0d1117 -
    4.35:1 is the arithmetic ceiling - but the brand green is 3.7:1 and 5.2:1, and this is a
    logotype at 30 px. currentColor keeps the media-query fallback for inline callers."""
    inherit = text_color == "currentColor"
    style = ('<style>@media(prefers-color-scheme:dark){svg{color:#e8efe9}}</style>' if inherit else "")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 64"%s '
            'aria-label="murmur">%s<path transform="translate(6.4 5.6) scale(0.7)" fill="%s" d="%s"/>'
            '<text x="60" y="42" font-family="Segoe UI Semibold, Segoe UI, Inter, sans-serif" '
            'font-size="40" letter-spacing="-1" fill="%s">murmur</text></svg>'
            % (' color="#16221a"' if inherit else "", style, mark_color, MARK_64, text_color))


def main(argv=None):
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser(prog="brand", description=__doc__.split("\n")[0])
    p.add_argument("--ico", type=Path, help="write a multi-size .ico of the app icon")
    p.add_argument("--assets", type=Path, help="write logo.svg / icon.svg / lockup.svg / logo-256.png here")
    a = p.parse_args(argv)
    if not a.ico and not a.assets:
        p.error("nothing to do: pass --ico and/or --assets")
    if a.ico:
        save_ico(a.ico)
        print(f"wrote {a.ico}")
    if a.assets:
        a.assets.mkdir(parents=True, exist_ok=True)
        # the lockup ships with a fixed ink: as an <img> it cannot inherit or see the host's theme
        for name, text in (("logo.svg", svg_mark()), ("icon.svg", svg_icon()),
                           ("lockup.svg", svg_lockup(text_color=GREEN))):
            (a.assets / name).write_text(text + "\n", encoding="utf-8")
        tile(256).save(a.assets / "logo-256.png")
        print(f"wrote {a.assets}/logo.svg, icon.svg, lockup.svg, logo-256.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
