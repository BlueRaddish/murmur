"""The app window: transcript history, Promptify (a dictation -> a prompt, and the engines that
write it) and settings. Hidden until opened from the tray.

One Toplevel, a sidebar and three views. The look is Fluent 2's layer model in achromatic
neutrals - a grained `base` under the sidebar, the user's pure white `layer` for the content,
`card` for grouped surfaces and `ctl` for anything raised - with the brand green as the only
chroma: the primary fill and the focus ring come from OKLCH at the accent's hue, and a danger
sibling sits on the same curve at hue 25. (Until 2026-08-28 the neutrals carried the bar's
accent hue at 1-2 % chroma; with a red bar colour the whole window read as pink, and the user
wants the window white and the brand green.) Depth is ONE language: a 1 px hairline, the surface
ladder, and on a raised control a 1 px elevation edge (a darkened bottom line; in dark a lit top
too) - zero drop shadows, no glow, no gradients. Tk cannot round a widget corner, so every corner
in here is PIL-rendered and handed to Tk as a PhotoImage: controls carry their shape as their own
image (`rr_png`), keycaps are the same raised recipe at 20 px (`kbd_img`), icons are stroked on a
16 grid (`icon_png`), the sidebar's grain is a tiled noise PNG, and surfaces that must stretch
wear four corner masks (`corner_pngs`) pinned at their corners. Settings autosave - there is no
Save button.
"""
import base64
import bisect
import datetime
import io
import json
import math
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

import brand
import connect
import promptify
import vault

# --- tokens ---------------------------------------------------------------------------------


def oklch_hex(L, C, h) -> str:
    """OKLCH (L 0..1, C, h degrees) -> '#rrggbb', gamut-clipped by reducing chroma."""
    def to_srgb(L, C, h):
        a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
        l_ = L + 0.3963377774 * a + 0.2158037573 * b
        m_ = L - 0.1055613458 * a - 0.0638541728 * b
        s_ = L - 0.0894841775 * a - 1.2914855480 * b
        l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
        r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
        g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
        b2 = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
        return r, g, b2
    for k in range(20):
        lin = to_srgb(L, C * (1 - k / 20), h)
        if all(-0.0005 <= v <= 1.0005 for v in lin):
            break
    def enc(v):
        v = min(1.0, max(0.0, v))
        v = 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
        return round(v * 255)
    return "#%02x%02x%02x" % tuple(enc(v) for v in lin)


def hex_hue(hx, fallback=250.0) -> float:
    """OKLCH hue of an sRGB hex; fallback (a cool grey) when (near) achromatic."""
    r, g, b = (int(hx.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in (r, g, b)]
    l = 0.4122214708 * lin[0] + 0.5363325363 * lin[1] + 0.0514459929 * lin[2]
    m = 0.2119034982 * lin[0] + 0.6806995451 * lin[1] + 0.1073969566 * lin[2]
    s = 0.0883024619 * lin[0] + 0.2817188376 * lin[1] + 0.6299787005 * lin[2]
    l_, m_, s_ = l ** (1 / 3), m ** (1 / 3), s ** (1 / 3)
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b2 = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    if math.hypot(a, b2) < 0.02:
        return fallback
    return math.degrees(math.atan2(b2, a)) % 360


# The accent steps (L, C) at the accent's hue - the only chromatic part of the palette. The
# neutrals are fixed achromatic hexes in Fluent 2's roles (base darker than layer in BOTH themes,
# the "layer on Mica" model); tinting them from the hue was the method's rule and the user's
# "pinkish" verdict overruled it: at 1-2 % chroma a red hue is a visible blush across every
# surface. Every alpha is premixed to a hex with `mix()` against the surface it sits on - Tk has
# no alpha, a token is a hex.
ACCENT = {False: {"ring": (0.60, .17), "primary": (0.55, .17), "primary_hover": (0.50, .17)},
          True: {"ring": (0.62, .15), "primary": (0.545, .15), "primary_hover": (0.59, .15)}}
DANGER_HUE = 25.0
DANGER = {False: {"danger": (0.55, .17), "danger_hover": (0.50, .17), "danger_text": (0.55, .17)},
          True: {"danger": (0.62, .15), "danger_hover": (0.66, .15), "danger_text": (0.72, .14)}}
NEUTRAL = {                     # Fluent 2 roles, achromatic, the user's white as the layer
    False: dict(base="#f3f3f3", layer="#ffffff", card="#ffffff", ctl="#fbfbfb", ctl_hover="#f5f5f5",
                ctl_press="#efefef", ink="#1b1b1b", muted="#646464"),
    True: dict(base="#121212", layer="#1c1c1c", card="#242424", ctl="#2e2e2e", ctl_hover="#353535",
               ctl_press="#292929", ink="#ececec", muted="#a6a6a6")}
ALPHA = {                       # (light, dark): alpha of black (light) / white (dark) over a surface
    "subtle": (.045, .065), "subtle_hover": (.03, .045), "stroke": (.08, .07), "divider": (.06, .06),
    "stroke_edge": (.16, None), "stroke_top": (None, .10), "stroke_field": (.45, .55),
    "stroke_strong": (.28, .30), "disabled": (.36, .36), "card_inset": (None, .04)}


def rgb(hx) -> tuple:
    return tuple(int(hx.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))


def lum(hx) -> float:
    lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in
           (c / 255 for c in rgb(hx))]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a, b, t) -> str:
    """Blend hex a toward hex b by t (0..1). Disabled text is a 45 % blend toward the ground."""
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(rgb(a), rgb(b)))


def norm_hex(v, default=None):
    """'#RGB' / 'rrggbb' -> '#rrggbb' lower-case; `default` when it is not a hex colour."""
    s = str(v or "").strip().lstrip("#")
    if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
        s = "".join(c * 2 for c in s)
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return "#" + s.lower()
    return default


def palette(accent_hex: str, dark: bool) -> dict:
    """One hue -> the whole system. The neutrals are `NEUTRAL[dark]`; the four GROUND-RELATIVE
    tokens are functions on the dict (`p["sub"](ground)`, `sub_hover`, `stroke`, `divider`) so a
    caller always premixes against the surface it draws on; the edges, strokes and `disabled`
    are premixed here over the surface they are defined on (`ctl`, `layer`, `card`). The accent
    (ring, primary) and its danger sibling at hue 25 are OKLCH at the accent's hue, and `on_*`
    is the text colour on a solid fill - white whenever white clears 4.5."""
    h = hex_hue(norm_hex(accent_hex, "#3c8cff"))
    p = dict(NEUTRAL[dark])
    over = "#ffffff" if dark else "#000000"            # what the alphas are alphas OF
    a = lambda name: ALPHA[name][dark]
    p["sub"] = lambda g: mix(g, over, a("subtle"))                 # selected row / nav fill
    p["sub_hover"] = lambda g: mix(g, over, a("subtle_hover"))     # hover on rows, nav, text buttons
    p["stroke"] = lambda g: mix(g, over, a("stroke"))              # a card's or control's outline
    p["divider"] = lambda g: mix(g, over, a("divider"))            # inset rules
    # the elevation edge of a raised control: a darkened bottom line in both themes (black 25 %
    # over `ctl` in dark), a lit top line in dark only
    p["stroke_edge"] = mix(p["ctl"], "#000000", .25) if dark else mix(p["ctl"], over, a("stroke_edge"))
    p["stroke_top"] = mix(p["ctl"], over, a("stroke_top")) if dark else None
    p["stroke_field"] = mix(p["layer"], over, a("stroke_field"))   # a field's underline at rest
    p["stroke_strong"] = mix(p["layer"], over, a("stroke_strong"))  # scroll thumb, slider track
    p["thumb"] = p["stroke_strong"]
    p["disabled"] = mix(p["layer"], over, a("disabled"))           # < 4.5 on purpose (WCAG exempts)
    p["card_inset"] = mix(p["card"], over, a("card_inset")) if dark else None
    for n, (L, C) in ACCENT[dark].items():
        p[n] = oklch_hex(L, C, h)
    for n, (L, C) in DANGER[dark].items():
        p[n] = oklch_hex(L, C, DANGER_HUE)
    on = lambda fill: "#ffffff" if contrast("#ffffff", fill) >= 4.5 else \
        max(("#ffffff", p["layer"], p["ink"]), key=lambda c: contrast(c, fill))
    p["on_primary"], p["on_danger"] = on(p["primary"]), on(p["danger"])
    # the chips' ground: a whisper of the ring over the layer, a little more under the pointer
    # and behind the chosen chip - only a little in light, where the focus ring has to stay 3:1
    # on the chosen chip (16 % holds 3.09)
    p["tint"] = mix(p["layer"], p["ring"], 0.18 if dark else 0.12)
    p["tint_hover"] = mix(p["layer"], p["ring"], 0.24 if dark else 0.16)
    # the sidebar's material, not colours: the grain's sigma at 1x device px and the white
    # alpha of the specular band at its top
    p["grain"], p["specular"] = (2.5, .045) if dark else (3.0, .40)
    return p


def system_dark() -> bool:
    """The Windows apps theme; anything unreadable means light."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return not winreg.QueryValueEx(k, "AppsUseLightTheme")[0]
    except Exception:
        return False


SP = (4, 8, 12, 16, 24, 32, 48)     # the only spacings allowed in this file
PAD_TOP = 16                        # ... except the header's top pad, tuned by eye against the
LH = 3                              # title bar, and the Text line lead that makes ~1.5 line height
R_CTL = 8                           # radius family: controls (button, field, segment, chip,
R_CARD = 12                         # nav pill, row highlight) and grouped cards. Inner radius
R_IN = 2                            # = outer - inset, clamped at 0: the segment's selected cell
R_KBD = 4                           # keycaps
H_CTL = 32                          # control height
H_NAV = 36                          # sidebar nav row (Fluent NavigationViewItem); rows 4 apart
H_ROW = 36                          # history row (one line)
H_ROW2 = 52                         # Promptify's dictation row (two lines)
H_GROUP = 24                        # day-group header row in both lists
H_CHIP = 24                         # question chips, the trigger-key cap
H_KBD = 20                          # keycaps (inline and footer)
H_FOOT = 28                         # the keyboard-hint footer strip
H_MARK = 24                         # sidebar lockup: the mark's IMAGE box (its ink is 40/64 of
GAP_MARK = 16                       # that, ~1.9x the wordmark's x-height) and the air after it.
                                    # Both are up from the 20/10 first cut: at that size the mark
                                    # read as a letter and the sidebar said "m murmur"
W_CHIP = 88                         # trigger-key cap: fits "Previous Track" at 9 pt
W_SIDE = 192                        # sidebar: 8 + pill 176 + 8 (icon 12 + 16 + 8 = label 36, then
                                    # "Promptify" 56-58 + the Ctrl-digit chord 64 + 8 inset leaves
                                    # >= 10 before the chord at every DPI; at 184 the chord overlaps)
W_FIELD = 96                        # the hex and language entries (7 mono characters)
W_DAYS = 64                         # the retention entry
W_COMBO = 200                       # the microphone and model combos
W_TIME = 44                         # the time column: a right-aligned mono9 "17:44" (35 px) at the
                                    # row's inner right; the day lives in the group header above
W_FILTER = 260                      # the History filter field
W_LIST = (176, 0.36, 240)           # Promptify's list pane: clamp(min, share of the main width, max)
W_ACT = 80                          # the engines sheet's action column
ICON = 16                           # icon box (14 inside a chip, 10 for the draft mark in a row)
WIN = (780, 560)
WIN_MIN = (600, 400)
MOTION_FAST, MOTION_NORMAL = 83, 150    # ms (Fluent): a hover's two frames span FAST, the bar's
                                        # three frames NORMAL - a frame is the span over the count
# the body measure: an 80-character line of body text is as wide as a prompt or a transcript
# gets. (`width=80` on a Text is 80 "0"s, which is ~100 average characters - too wide.)
SAMPLE80 = "The quick brown fox jumps over the lazy dog while the band plays on by the pier."

# --- domain ---------------------------------------------------------------------------------

MODELS = ["tiny.en", "base.en", "small.en", "tiny", "base", "small"]   # nothing over ~500 MB: the app stays light (the user's rule)
MIC_DEFAULT = "System default"      # the microphone combo's first entry (cfg["mic"] = None)
PRESETS = ["#ffffff", "#e63c3c", "#ff7a1a", "#ffc82a", "#4ade80", "#22d3ee", "#3c8cff", "#a855f7", "#ff4fa3"]
MEDIA_KEYS = {0xB3: "Play/Pause", 0xB2: "Media Stop", 0xB0: "Next Track", 0xB1: "Previous Track", 0xAD: "Mute",
              0xAE: "Volume Down", 0xAF: "Volume Up", 0xB5: "Media Select", 0xB4: "Mail", 0xA6: "Browser Back"}


def vk_name(vk) -> str:
    if vk is None:
        return "none"
    if vk in MEDIA_KEYS:
        return MEDIA_KEYS[vk]
    try:
        import ctypes
        sc = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
        buf = ctypes.create_unicode_buffer(64)
        if sc and ctypes.windll.user32.GetKeyNameTextW(sc << 16, buf, 64):
            return buf.value
    except Exception:
        pass
    return f"key 0x{vk:02X}"


class History:
    """Append-only JSONL of {"t": epoch, "text": ...}; entries older than the retention
    window are dropped on load and on every append."""

    def __init__(self, path: Path, days: float):
        self.path = path
        self.days = days
        self.items: list = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        self.items = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self.items.append(json.loads(line))
                except ValueError:
                    continue
        self.prune()

    def prune(self) -> bool:
        cutoff = time.time() - self.days * 86400
        kept = [i for i in self.items if i["t"] >= cutoff]
        changed = len(kept) != len(self.items)
        self.items = kept
        if changed:
            self.save()
        return changed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in self.items),
                             encoding="utf-8")

    def append(self, text: str) -> None:
        self.items.append({"t": time.time(), "text": text})
        if not self.prune():
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.items[-1], ensure_ascii=False) + "\n")

    def delete(self, idx: int) -> None:
        del self.items[idx]
        self.save()

    def insert(self, idx: int, item: dict) -> None:
        """Undo of a delete: put the entry back where it was."""
        self.items.insert(idx, item)
        self.save()

    def clear(self) -> None:
        self.items = []
        self.save()


# --- text -----------------------------------------------------------------------------------


def ellipsize(font, text: str, width: int) -> str:
    """The longest prefix of `text` that fits `width` px, closed with a real ellipsis."""
    if width <= 0 or font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:                       # binary search: measure() is the expensive call
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _days_ago(t: float, now: float = None) -> tuple:
    """(whole days between t's date and now's, t's struct_time, now's struct_time)."""
    now = time.time() if now is None else now
    lt, ln = time.localtime(t), time.localtime(now)
    day = lambda s: datetime.date(s.tm_year, s.tm_mon, s.tm_mday)
    return (day(ln) - day(lt)).days, lt, ln


def day_of(t: float, now: float = None) -> str:
    """The day-group header of a list row: 'Today' / 'Yesterday' / 'Monday' (2-6 days back) /
    'Aug 12' / 'Aug 12 2025'. The row's own time is a plain clock beside it."""
    d, lt, ln = _days_ago(t, now)
    if d == 0:
        return "Today"
    if d == 1:
        return "Yesterday"
    if 2 <= d <= 6:
        return time.strftime("%A", lt)
    out = time.strftime("%b ", lt) + str(lt.tm_mday)
    return out if lt.tm_year == ln.tm_year else out + " " + str(lt.tm_year)


# --- PIL-rendered round things --------------------------------------------------------------

SS = 3          # supersampling: Tk's canvas has no anti-aliasing, PIL does


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.reduce(SS).save(buf, "PNG")
    return buf.getvalue()


def compose_png(w: int, h: int, parts: list) -> bytes:
    """Several PNGs (already at device px) alpha-composited in order onto one transparent
    w x h box - a nav row's grain, pill, icon and cap boxes as ONE image (`_nav_img`). Tk
    blends every partial-alpha image it draws by reading the surface back first (~0.2 ms a
    draw, measured 2026-09-08), so a row of four such images repainted on every view switch
    cost 3 ms; one opaque image costs nothing of that."""
    from PIL import Image
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for x, y, png in parts:
        im.alpha_composite(Image.open(io.BytesIO(png)).convert("RGBA"), (x, y))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def dot_png(box: int, d: int, fill: str, ring: str = None, ring_w: int = 2, gap: int = 2,
            edge: str = None, core: tuple = None) -> bytes:
    """A filled circle of diameter d centred in a transparent box; `edge` is its own hairline
    (white would otherwise disappear on the light ground), `ring` the selection halo, `core` =
    (diameter, colour) a second filled circle inside it - the slider thumb's accent core."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (box * SS, box * SS), (0, 0, 0, 0))
    dr, c, r = ImageDraw.Draw(im), box * SS / 2, d * SS / 2
    dr.ellipse([c - r, c - r, c + r, c + r], fill=fill, outline=edge, width=SS if edge else 0)
    if core:
        rc = core[0] * SS / 2
        dr.ellipse([c - rc, c - rc, c + rc, c + rc], fill=core[1])
    if ring:
        # PIL draws an outline INWARD from its box, so the box's radius is the ring's outer
        # edge: dot + gap + ring - a 14 px dot, gap 2, ring 2 fills a 22 box exactly
        rr = r + (gap + ring_w) * SS
        dr.ellipse([c - rr, c - rr, c + rr, c + rr], outline=ring, width=int(ring_w * SS))
    return _png(im)


def pill_png(w: int, h: int, track: str, knob: str, on: bool, outline: str = None,
             knob_d: int = None, t: float = None) -> bytes:
    """The toggle switch: a capsule with the knob at one end. `outline` is a 1 px hairline round
    the track (the off state's), `knob_d` the knob's diameter (default h - 6), `t` the knob's
    position 0.0 .. 1.0 (default: the end `on` says) - the slide's frames pass it."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=h * SS / 2, fill=track,
                         outline=outline, width=SS if outline else 0)
    r, m = (h - 6 if knob_d is None else knob_d) * SS / 2, 3 * SS
    t = (1.0 if on else 0.0) if t is None else t
    cx = m + r + t * (w * SS - 2 * (m + r))
    dr.ellipse([cx - r, h * SS / 2 - r, cx + r, h * SS / 2 + r], fill=knob)
    return _png(im)


def _edges(dr, x0: int, y0: int, x1: int, y1: int, r: int, bottom: str = None,
           top: str = None) -> None:
    """The Fluent elevation border on a rounded box already drawn at SS: a 1 logical px line
    along the very bottom row (`bottom`) and/or the top row (`top`), BETWEEN the corner arcs -
    it replaces the outline's straight run there and the arcs keep the outline's colour.
    x0..x1 / y0..y1 are the box's inclusive pixel bounds, r its radius, all in SS px."""
    if bottom:
        dr.rectangle([x0 + r, y1 - SS + 1, x1 - r, y1], fill=bottom)
    if top:
        dr.rectangle([x0 + r, y0, x1 - r, y0 + SS - 1], fill=top)


def rr_png(w: int, h: int, r: int, fill: str, border: str = None, border_w: int = 1,
           ground: str = None, bar: tuple = None, inner: tuple = None, edge: tuple = None,
           under: tuple = None) -> bytes:
    """A rounded rectangle with an optional hairline - the shape a Tk widget cannot have, drawn
    at SS and reduced so the arc is anti-aliased. `ground` is the colour BEHIND the corners: pass
    it and the PNG is opaque (what a canvas item wants); leave it out and the corners are
    transparent for Tk to composite against a Label's own bg. `edge` = (bottom, top), each a hex
    or None: the elevation edge of a raised control (`_edges`). `under` = (colour, px): a text
    field's underline, `px` tall along the bottom between the arcs. `bar` = (x, w, h, radius,
    colour): an accent pill drawn inside, vertically centred. `inner` = (inset, width, colour):
    a second outline INSIDE the edge, that far in, on the arc of radius r - inset - the focus
    ring of a solid button, whose fill the accent ring would vanish against."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w * SS, h * SS), ground if ground else (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=r * SS, fill=fill,
                         outline=border, width=int(border_w * SS) if border else 0)
    if edge:
        _edges(dr, 0, 0, w * SS - 1, h * SS - 1, r * SS, *edge)
    if under:
        dr.rectangle([r * SS, (h - under[1]) * SS, (w - r) * SS - 1, h * SS - 1], fill=under[0])
    if bar:
        x, bw, bh, br, col = bar
        y = (h - bh) * SS / 2
        dr.rounded_rectangle([x * SS, y, (x + bw) * SS - 1, y + bh * SS - 1], radius=br * SS,
                             fill=col)
    if inner:
        n, iw, col = inner
        dr.rounded_rectangle([n * SS, n * SS, (w - n) * SS - 1, (h - n) * SS - 1],
                             radius=max(0, r - n) * SS, outline=col, width=int(iw * SS))
    return _png(im)


def corner_pngs(r: int, fill: str, ground: str, border: str = None, border_w: int = 1) -> list:
    """The four corner masks of a rounded rect, nw/ne/se/sw: one circle of diameter 2r on an
    opaque `ground`, cut into quadrants. Pinned over the corners of a square frame they round
    it - and because they are fixed-size images at RELATIVE positions, the frame underneath can
    stretch with the window without re-rendering anything."""
    from PIL import Image, ImageDraw
    n = r * SS
    im = Image.new("RGB", (2 * n, 2 * n), ground)
    ImageDraw.Draw(im).ellipse([0, 0, 2 * n - 1, 2 * n - 1], fill=fill, outline=border,
                               width=int(border_w * SS) if border else 0)
    return [_png(im.crop(b)) for b in
            ((0, 0, n, n), (n, 0, 2 * n, n), (n, n, 2 * n, 2 * n), (0, n, n, 2 * n))]


def segment_png(widths: tuple, h: int, r: int, i: int, fills: tuple, ground: str, fill: str,
                border: str, border_w: int = 1, inset: int = R_IN) -> bytes:
    """Slice `i` of a segmented control. The container is rendered WHOLE - so both end caps come
    off the same arc - the picked/hovered cells get an inner pill inset `inset` px, and only then
    is it cut at the cell boundaries. Each slice goes on its own widget, which is how the cells
    stay real widgets (one tab stop on the group, a <Button-1> per option) with round corners.
    A cell in `fills` is None, a hex (the hovered cell: an inset pill) or a tuple
    (fill, outline, bottom, top) - the picked cell as the raised key: a hairline and the
    elevation edge between its inner arcs, the same recipe as `rr_png`'s `edge`."""
    from PIL import Image, ImageDraw
    w = sum(widths)
    im = Image.new("RGBA", (w * SS, h * SS), ground)
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=r * SS, fill=fill,
                         outline=border, width=int(border_w * SS))
    for j, cell in enumerate(fills):
        if cell:
            x = sum(widths[:j])
            box = [(x + inset) * SS, inset * SS, (x + widths[j] - inset) * SS - 1, (h - inset) * SS - 1]
            ri = max(0, r - inset) * SS
            if isinstance(cell, tuple):
                dr.rounded_rectangle(box, radius=ri, fill=cell[0], outline=cell[1], width=SS)
                _edges(dr, *box, ri, cell[2], cell[3])
            else:
                dr.rounded_rectangle(box, radius=ri, fill=cell)
    x0 = sum(widths[:i])
    return _png(im.crop((x0 * SS, 0, (x0 + widths[i]) * SS, h * SS)))


def grain_png(size: int, base: str, sigma: float) -> bytes:
    """The sidebar's material: a `size` px square of gaussian noise (sigma, at 1x device px - no
    supersampling, the grain IS the pixels) around the achromatic `base`, clamped to base +- 5
    so M6 (every sidebar pixel within 5 of base) holds by construction - at sigma 3 a gaussian
    alone leaves ~9 % of the pixels past it, and 2 sigma is 6 anyway. Tiled on a canvas; a
    monitor-high frost would be 190 ms, ~32 of these tiles are 0.4 ms once."""
    from PIL import Image
    b = rgb(base)[0]                       # achromatic: one channel is the colour
    lo, hi = max(0, b - 5), min(255, b + 5)
    im = Image.effect_noise((size, size), sigma).point(lambda v: max(lo, min(hi, v - 128 + b)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def specular_png(w: int, h: int, alpha: float) -> bytes:
    """A white band whose alpha runs linearly from `alpha` at the top row to 0 at the bottom:
    the light on the sidebar's top, composited by the canvas over the grain tiles (Tk 8.6
    alpha-blends a partial-alpha PNG on a Canvas). Device px, no supersampling."""
    from PIL import Image
    im = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    im.putalpha(Image.frombytes("L", (w, h), bytes(round(255 * alpha * (1 - y / h))
                                                    for y in range(h) for _ in range(w))))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# the icon glyphs on a 16 grid, logical px: ("line", points), ("closed", points), ("ellipse",
# bbox) - all stroked - and ("knob", (cx, cy, r)), a stroked circle whose inside is cut out to
# the ground. 1 px safe margin, 1.5 px stroke, round caps and joins, no fills.
GLYPHS = {
    "history": (("ellipse", (2, 2, 14, 14)), ("line", ((8, 8), (8, 4.5))), ("line", ((8, 8), (10.5, 9.5)))),
    "sparkle": (("closed", ((8, 2), (9.6, 6.4), (14, 8), (9.6, 9.6), (8, 14), (6.4, 9.6), (2, 8), (6.4, 6.4))),),
    "options": (("line", ((2.5, 5.5), (13.5, 5.5))), ("line", ((2.5, 10.5), (13.5, 10.5))),
                ("knob", (10, 5.5, 1.75)), ("knob", (6, 10.5, 1.75))),
    "search": (("ellipse", (2.5, 2.5, 11.5, 11.5)), ("line", ((10.3, 10.3), (13.5, 13.5)))),
    "chev": (("line", ((4, 6.5), (8, 10.5), (12, 6.5))),),
    "term": (("line", ((3, 4), (7.5, 8), (3, 12))), ("line", ((8.5, 12.5), (13.5, 12.5)))),
    "globe": (("ellipse", (2, 2, 14, 14)), ("ellipse", (5, 2, 11, 14)), ("line", ((2, 8), (14, 8)))),
    "note": (("closed", ((4, 2), (9.5, 2), (12, 4.5), (12, 14), (4, 14))),
             ("line", ((9.5, 2), (9.5, 4.5), (12, 4.5))),
             ("line", ((6, 8), (10, 8))), ("line", ((6, 10.5), (10, 10.5)))),
    "check": (("line", ((3, 8.5), (6.5, 12), (13, 4.5))),),
    # the three waveform styles in Settings > Appearance
    "wave": (("line", ((2, 8), (4, 4.5), (6.5, 11.5), (9, 4), (11.5, 11), (14, 7.5))),),
    "bars": (("line", ((3, 6.5), (3, 9.5))), ("line", ((6, 3), (6, 13))), ("line", ((9, 5), (9, 11))),
             ("line", ((12, 2.5), (12, 13.5)))),
    "drop": (("closed", ((8, 2.5), (11.3, 7.8), (11.8, 10.4), (10.2, 12.9), (8, 13.6), (5.8, 12.9),
                         (4.2, 10.4), (4.7, 7.8))),),
}


def icon_png(name: str, colour: str, size: int = 16, ground: str = None) -> bytes:
    """One of `GLYPHS` stroked in `colour` on a transparent `size` px box (device px: the 16
    grid scales with it), drawn at SS and reduced. Round joins are PIL's `joint="curve"`; round
    caps are a filled circle of the stroke's diameter at each open end. `ground` fills the
    `options` knobs; left out, their inside is cut to transparent, which reads the same on any
    ground."""
    from PIL import Image, ImageDraw
    k = size * SS / 16                       # grid units -> SS px
    im = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    sw = max(1, round(1.5 * k))              # 4 at SS 3, size 16
    cap = lambda x, y: dr.ellipse([x - sw / 2, y - sw / 2, x + sw / 2, y + sw / 2], fill=colour)
    for kind, geo in GLYPHS[name]:
        if kind == "ellipse":
            dr.ellipse([v * k for v in geo], outline=colour, width=sw)
        elif kind == "knob":
            cx, cy, r = (v * k for v in geo)
            dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ground or (0, 0, 0, 0),
                       outline=colour, width=sw)
        else:
            pts = [(x * k, y * k) for x, y in geo]
            if kind == "closed":             # the seam gets a joint like every other corner
                pts += pts[:2]
            dr.line(pts, fill=colour, width=sw, joint="curve")
            for x, y in ((pts[0], pts[-1]) if kind == "line" else ()):
                cap(x, y)
    return _png(im)


TARGET_UI = {"code": ("term", "Code"), "chat": ("globe", "Web")}   # Promptify's target cells: icon, label


# --- widgets --------------------------------------------------------------------------------


class _Btn:
    """A button is ONE Label carrying a rounded-rect image with its text drawn on top
    (`compound="center"`): the shape is the image, so hover, press and focus are re-renders of
    it - no motion, no weight or size shift. Kinds: primary (the one accent fill), danger,
    secondary (the raised key: `ctl` with a hairline and the elevation edge, ink text; pressed
    = `ctl_press` with the edge gone, the sink), text (bare until hovered; muted, or `ink`) and
    chip (a full pill on the accent tint, see `_Chip`). The focus ring is drawn on the button's
    own edge (2 px of `ring`) instead of around it, so focusing never moves anything; a solid
    button, whose fill the ring would vanish against (1.2:1), wears it INSIDE its edge in its
    own text colour."""

    def __init__(self, ui, parent, text, cmd, kind="secondary", font=None, h=None, pad=None,
                 r=None, ink=False):
        p, self.ui, self.cmd, self.kind = ui.pal, ui, cmd, kind
        g = self.ground = parent["bg"]
        chip = kind == "chip"
        # rest fill, rest text, hover fill, hover text, pressed fill
        self.fill, self.fg, self.hov, self.hov_fg, self.down = {
            "primary": (p["primary"], p["on_primary"], p["primary_hover"], p["on_primary"], p["primary_hover"]),
            "danger": (p["danger"], p["on_danger"], p["danger_hover"], p["on_danger"], p["danger_hover"]),
            "secondary": (p["ctl"], p["ink"], p["ctl_hover"], p["ink"], p["ctl_press"]),
            "text": (g, p["ink"] if ink else p["muted"], p["sub_hover"](g), p["ink"], p["sub"](g)),
            "chip": (p["tint"], p["ink"], p["tint_hover"], p["ink"], p["tint_hover"]),
        }[kind]
        raised = kind == "secondary"                 # the one kind with an outline and an edge
        self.outline = p["stroke"](p["ctl"]) if raised else None
        self.edge = (p["stroke_edge"], p["stroke_top"]) if raised else None
        self.h = ui.px(h or (H_CHIP if chip else H_CTL))
        self.r = ui.px((H_CHIP // 2 if chip else R_CTL) if r is None else r)
        self.pad = ui.px((SP[1] if kind in ("text", "chip") else SP[2]) if pad is None else pad)
        self.state, self.ring, self.on = "idle", False, True
        self.f = self._widget(parent, text, font or ui.F["body"])
        self._size()
        self.w0 = self.w                             # the width the button was born with
        self._paint()
        if kind == "primary":
            ui.primaries.append(self)                # so a check can count them on screen
        self.f.bind("<Enter>", lambda e: self._set("hover"))
        self.f.bind("<Leave>", lambda e: self._set("idle"))
        self.f.bind("<Button-1>", lambda e: self._set("down"))
        self.f.bind("<ButtonRelease-1>", self._release)
        self.f.bind("<FocusIn>", lambda e: self._ring(True))
        self.f.bind("<FocusOut>", lambda e: self._ring(False))
        for k in ("<Return>", "<space>"):
            self.f.bind(k, lambda e: self.on and self.cmd())

    def _widget(self, parent, text, font) -> tk.Widget:
        return tk.Label(parent, text=text, font=font, fg=self.fg, bd=0, bg=self.ground,
                        compound="center", highlightthickness=0, takefocus=1, cursor="hand2",
                        padx=0, pady=0)   # Tk's default 1 px pad would show

    def _size(self) -> None:
        self.f.configure(image="")                     # measure the TEXT, not the image behind it
        self.w = self.f.winfo_reqwidth() + 2 * self.pad

    def _paint(self) -> None:
        p = self.ui.pal
        fill, fg = {"idle": (self.fill, self.fg), "hover": (self.hov, self.hov_fg),
                    "blend": (mix(self.fill, self.hov, .5), self.hov_fg),   # the hover's first frame
                    "down": (self.down, self.hov_fg)}[self.state]
        border, bw, inner = self.outline, 1, None
        edge = None if self.state == "down" else self.edge     # pressed: the edge goes, the sink
        if not self.on:
            fade = lambda c: mix(c, self.ground, .45) if c else c
            fill, fg, border = fade(fill), fade(fg), fade(border)
            edge = tuple(fade(c) for c in edge) if edge else None
        if self.ring and self.kind in ("primary", "danger"):
            inner = (self.ui.px(2), self.ui.px(2), fg)
        elif self.ring:
            border, bw = p["ring"], self.ui.px(2)
        self._draw(self.ui.rr(self.w, self.h, self.r, fill, self.ground, border, bw, inner=inner,
                              edge=edge), fg)

    def _draw(self, img, fg) -> None:
        self.f.configure(image=img, fg=fg)

    def _set(self, state) -> None:
        """Hover on a secondary, text or chip button comes in two frames over `MOTION_FAST`
        (Fluent's 83 ms): the 50 % blend now, the full hover half of that later
        (`ui.jobs[("blend", id)]`, so a rebuild cancels it); only from idle - a release after a
        press paints hover at once - and only with motion on. Every other change, leaving
        included, is instant and drops a pending frame, so a button never holds more than one.
        The frame skips a button destroyed in the meantime (a rebuilt pane)."""
        if not self.on:
            return
        ui, key = self.ui, ("blend", id(self.f))
        ui._cancel(key)
        if (state == "hover" and self.state == "idle" and ui.motion
                and self.kind in ("secondary", "text", "chip")):
            self.state = "blend"
            self._paint()
            ui.jobs[key] = ui.win.after(MOTION_FAST // 2, lambda: (
                ui.jobs.pop(key, None), self.f.winfo_exists() and self._set("hover")))
            return
        self.state = state
        self._paint()

    def _ring(self, on) -> None:
        self.ring = on and getattr(self.ui, "kbd", True)   # focus-visible: keyboard focus only
        self._paint()

    def _release(self, e) -> None:
        if not self.on:
            return
        self._set("hover")
        self.ui.kbd = False
        self.f.focus_set()
        if 0 <= e.x < self.f.winfo_width() and 0 <= e.y < self.f.winfo_height():
            self.cmd()

    def text(self, s, hold=True) -> None:
        """Re-label. `hold` keeps at least the width the button was born with, so "Copy prompt"
        turning into "Copied" for a moment moves nothing; a state change passes False and the
        button is re-sized to its new label."""
        self.f.configure(text=s)
        self._size()
        if hold:
            self.w = max(self.w0, self.w)
        else:
            self.w0 = self.w
        self._paint()

    def enable(self, on) -> None:
        """Disabled: fill and text faded 45 % toward the ground, out of the tab ring, clicks
        ignored. Nothing changes size."""
        self.on = bool(on)
        self.state = "idle"
        self.ui._cancel(("blend", id(self.f)))
        self.f.configure(takefocus=1 if self.on else 0, cursor="hand2" if self.on else "arrow")
        self._paint()

    def mute(self, on) -> None:
        """A text button's rest colour: muted, or ink (the engine name in the draft header)."""
        self.fg = self.ui.pal["muted"] if on else self.ui.pal["ink"]
        self._paint()


class _Chip(_Btn):
    """A question's option: a 24 px pill on the accent tint. The CHOSEN chip - the one whose
    text is the answer - carries a 6 px primary dot before its text, so a chip is a Canvas: a
    compound Label centres its text over the image and cannot start it after a dot. Chips are
    not tab stops; their strip is (`_chips`), and the ring goes on the one under its cursor.
    With `icon` (a glyph name, drawn at 14 px in the text's colour) the dot's slot shows the
    icon instead, always - a Context note chip - and `chosen()` does nothing to it."""

    def __init__(self, ui, parent, text, cmd, icon=None):
        self.label, self.is_chosen, self.icon = text, False, icon
        super().__init__(ui, parent, text, cmd, kind="chip")

    def _widget(self, parent, text, font) -> tk.Widget:
        c = tk.Canvas(parent, bd=0, highlightthickness=0, bg=self.ground, cursor="hand2", takefocus=0)
        self.i_pill = c.create_image(0, 0, anchor="nw")
        self.i_dot = c.create_image(self.ui.px(7), self.h // 2, anchor="w", state="hidden",
                                    image=self.ui.dot(8, 6, self.ui.pal["ring"]))
        self.i_text = c.create_text(self.pad, self.h // 2, anchor="w", text=text, font=font)
        return c

    def _lead(self) -> int:
        """What sits before the text: an icon 14 + gap 6, the chosen dot 6 + gap 6, or nothing."""
        return self.ui.px(14) + self.ui.px(6) if self.icon else self.ui.px(SP[2]) if self.is_chosen else 0

    def _size(self) -> None:
        self.w = self.ui.mf["body"].measure(self.label) + 2 * self.pad + self._lead()

    def _draw(self, img, fg) -> None:
        c = self.f
        c.configure(width=self.w, height=self.h)
        c.itemconfigure(self.i_pill, image=img)
        c.itemconfigure(self.i_text, fill=fg)
        c.coords(self.i_text, self.pad + self._lead(), self.h // 2)
        if self.icon:
            c.itemconfigure(self.i_dot, state="normal", image=self.ui.icon(self.icon, fg, 14))
        else:
            c.itemconfigure(self.i_dot, state="normal" if self.is_chosen else "hidden")

    def text(self, s, hold=True) -> None:
        self.label = s
        self.f.itemconfigure(self.i_text, text=s)
        self._size()
        self._paint()

    def chosen(self, on) -> None:
        if self.icon:
            return
        if on != self.is_chosen:
            self.is_chosen = bool(on)
            self.fill = self.ui.pal["tint_hover" if on else "tint"]
            self._size()
            self._paint()


class _Row:
    """A settings row inside a group card: label (+ description, + "restart to apply") left, the
    control right-aligned in a column as wide as the control, 16 from the text. Rows are
    separated by an INSET hairline - from the label's left edge to the right padding, in a
    lighter step than the card's own outline, so the card reads as one object instead of a
    stack of full-width rules. 12 px above and below the taller column: a label over its
    description makes the row 62 (Fluent's SettingsCard is 68; 60-ish keeps the Indicator card
    on a 560 px window), a 32 px control alone 56. The Labels carry none of Tk's default
    border/padding (2 + 1 px a side), so their text lands on E and the row on the grid."""

    def __init__(self, ui, group, label, desc=None):
        p, bg = ui.pal, group["bg"]
        self.rule = ui.hairline(group, color=p["divider"](bg)) if group.winfo_children() else None
        if self.rule is not None:
            self.rule.pack(fill="x", padx=ui.cpad)
        self.ui, self.shown = ui, True
        self.frame = tk.Frame(group, bg=bg)
        self.frame.pack(fill="x")
        main = tk.Frame(self.frame, bg=bg)
        main.pack(fill="x", padx=ui.cpad)
        self.right = tk.Frame(main, bg=bg)
        self.right.pack(side="right", pady=ui.px(SP[2]))   # the control's own width; the label
        left = self.left = tk.Frame(main, bg=bg)            # column takes what is left
        left.pack(side="left", fill="x", expand=True, pady=ui.px(SP[2]), padx=(0, ui.px(SP[3])))
        self.head = tk.Frame(left, bg=bg)
        self.head.pack(anchor="w")
        self.label = tk.Label(self.head, text=label, font=ui.F["body"], fg=p["ink"], bg=bg, bd=0,
                              padx=0, pady=0, anchor="w")
        self.label.pack(side="left")
        self.chip = None
        self.desc = tk.Label(left, text=desc or "", font=ui.F["meta"], fg=p["muted"], bg=bg,
                             anchor="w", justify="left", bd=0, padx=0, pady=0)
        # the description wraps at the text column's own width - never wider than the column
        # that clips it (a floor of 120 ran it under the control on a 600 px window)
        left.bind("<Configure>", lambda e: self.desc.configure(wraplength=max(e.width, ui.px(48))))
        if desc:
            self.desc.pack(anchor="w", pady=(ui.px(SP[0]), 0))
        self.stacked = False
        main.bind("<Configure>", lambda e: self._fit(e.width))
        self.err = tk.Label(self.frame, text="", font=ui.F["meta"], fg=p["danger_text"], bg=bg,
                            anchor="e", bd=0, padx=0, pady=0)

    def _fit(self, width) -> None:
        """The control column keeps its width. When what is left beside it would not hold the
        label (the colour rows' dots + swatch + hex field, the trigger row's cap + two keys, on
        a 600 px window - the labels rendered as "olou" / "Rec"), the control drops UNDER the
        text on the row's right edge, so the one right edge survives and the label is whole;
        back beside it when the room returns. Change-only: no re-pack on a resize that leaves
        the answer alone."""
        ui = self.ui
        if width <= 1:
            return
        room = width - self.right.winfo_reqwidth() - ui.px(SP[3])
        stack = room < max(self.head.winfo_reqwidth(), ui.px(120))
        if stack == self.stacked:
            return
        self.stacked = stack
        if stack:
            self.left.pack_configure(side="top", fill="x", expand=False, padx=0)
            self.right.pack_configure(side="top", anchor="e", after=self.left, pady=(0, ui.px(SP[2])))
        else:
            self.right.pack_configure(side="right", anchor="center", before=self.left, pady=ui.px(SP[2]))
            self.left.pack_configure(side="left", fill="x", expand=True, padx=(0, ui.px(SP[3])))

    def show(self, on, after=None) -> None:
        """Rows that apply to some settings only come and go; `after` (a _Row) keeps the order.
        The hairline above the row goes with it, or hiding a row would leave a double rule."""
        if on and not self.shown:
            kw = {"after": after.frame} if after is not None else {}
            if self.rule is not None:
                self.rule.pack(fill="x", padx=self.ui.cpad, **kw)
                kw = {"after": self.rule}
            self.frame.pack(fill="x", **kw)
        elif not on and self.shown:
            if self.rule is not None:
                self.rule.pack_forget()
            self.frame.pack_forget()
        self.shown = bool(on)

    def restart(self) -> None:
        """Model / microphone / language changed: say so, and keep saying it - in plain meta
        text after the label (a pill here was the one accent on the page that was not an
        action)."""
        if self.chip is None:
            ui = self.ui
            self.chip = tk.Label(self.head, text="restart to apply", font=ui.F["meta"],
                                 bg=self.head["bg"], fg=ui.pal["muted"], bd=0, padx=0, pady=0)
            self.chip.pack(side="left", padx=(ui.px(SP[1]), 0))

    def error(self, msg) -> None:
        self.err.configure(text=msg or "")
        (self.err.pack(fill="x", padx=self.ui.cpad, pady=(0, self.ui.px(SP[1])))
         if msg else self.err.pack_forget())


class RowList(tk.Canvas):
    """The dictations, newest first, as canvas items - 45 of them as Frames would be slow and
    would each need their own hover bindings - grouped under a day header ("Today",
    "Yesterday", "Monday", "Aug 12") wherever the day changes. Two modes: one line (History:
    text, then the draft mark and a right-aligned mono time at the inner right; `H_ROW`) and
    two lines (Promptify: text over time + mark; `H_ROW2`). `keep(item)` is the pane's filter
    predicate (History's reads the filter field); `rows` holds `(item, y, h)` per canvas row,
    `(None, y, h)` for a header, and `ys` the row tops for a bisect in `row_at`.

    Hover and selection are not per-row rectangles but TWO rounded images moved to the row they
    belong to - a rounded highlight would otherwise cost three rectangles and four corner masks
    per row, and only two rows are ever lit. The selected one carries the 3 x 16 `ring` bar (the
    nav's), hover is the fill alone. The keyboard's row wears the focus ring on the selected
    highlight itself: a hard ring around the whole list would be the one rigid line in the
    view. The selection is the window's (`ui.sel`, shared by both lists); `on_select(item)` is
    told when a row is picked, `keys` are the pane's own bindings (Return, Delete...).

    Ceiling: `draw()` redraws EVERY row on every <Configure>, ~7 font.measure calls each. Fine to
    about 2 000 items (a year at 5 dictations a day); past that the upgrade path is to draw only
    the slice between canvasy(0) and canvasy(0)+height, and redraw on scroll too."""

    def __init__(self, ui, parent, two_line=False, on_select=None, keys=None, keep=None):
        p = ui.pal
        super().__init__(parent, bg=p["layer"], highlightthickness=0, takefocus=1, yscrollincrement=1,
                         width=1, height=1, bd=0)
        self.ui, self.two, self.on_select = ui, two_line, on_select or (lambda it: None)
        self.keep = keep or (lambda it: True)
        self.on_scroll = lambda on: None        # told (once per change) whether the list is scrolled
        self.scrolled = False
        self.H = ui.px(H_ROW2 if two_line else H_ROW)
        self.rows, self.ys, self.hl, self.hl_id, self.hover = [], [], {}, {}, None
        self.sb = ttk.Scrollbar(parent, orient="vertical", style="M.Vertical.TScrollbar",
                                command=self.yview, takefocus=0)
        self.configure(yscrollcommand=self._scrolled)
        self.pack(side="left", fill="both", expand=True)
        self.bind("<Configure>", lambda e: self.draw())
        # ... and the selection back into view once the canvas has its new height: the card
        # grows for a taller transcript AFTER `move()` / `go()` scrolled to the row, through
        # several idle passes (Text -> card -> grid -> this canvas), so an idle job asked too
        # early and the row ended below the shorter list's fold; a window resize re-shows it too
        self.bind("<Configure>", lambda e: self.show_sel(), add="+")
        self.bind("<FocusIn>", lambda e: self.paint())
        self.bind("<FocusOut>", lambda e: self.paint())
        self.bind("<Motion>", lambda e: self.set_hover(self.row_at(e.y)))
        self.bind("<Leave>", lambda e: self.set_hover(None))
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"), add="+")
        self.bind("<Up>", lambda e: self.move(-1))
        self.bind("<Down>", lambda e: self.move(1))
        for k, fn in (keys or {}).items():
            self.bind(k, lambda e, fn=fn: fn())

    def items(self) -> list:
        """The dictations this list shows (oldest first, as the file): the history through
        the pane's `keep` predicate."""
        return [it for it in self.ui.history.items if self.keep(it)]

    def natural(self) -> int:
        """The height of every row drawn, headers included: the scrollregion, and what the
        History view's height rule gives the list before the card takes the rest."""
        return sum(h for _, _, h in self.rows)

    def _scrolled(self, lo, hi) -> None:
        self.sb.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.sb.pack_forget()
        else:
            self.sb.pack(side="right", fill="y")
        if (float(lo) > 0.0) != self.scrolled:   # the pane's scroll-only rule follows
            self.scrolled = not self.scrolled
            self.on_scroll(self.scrolled)

    def _wheel(self, e) -> None:
        if self.ui._visible():
            self.yview_scroll(int(-e.delta / 120) * 3 * self.H, "units")

    def draw(self) -> None:
        ui, p, c = self.ui, self.ui.pal, self
        px = ui.px
        c.delete("all")
        self.rows, self.ys = [], []
        items = self.items()
        w, h = c.winfo_width(), self.H
        x0 = px(SP[3])                          # E, the pane's one text edge
        if not items:
            self.hl_id = {}       # the highlights went with delete("all"): nothing to move
            self.empty(w, filtered=bool(ui.history.items))
            c.configure(scrollregion=(0, 0, 0, 0))
            return
        # the highlight runs from E - 12 to the pane's right edge - 4, like every surface that
        # carries text; the text keeps its edge, 12 px inside it
        hx = px(SP[0])
        hw = max(h, w - 2 * hx)   # w is 1 until Tk has laid the canvas out
        if self.hl.get("w") != hw:
            png = lambda **kw: tk.PhotoImage(data=base64.b64encode(rr_png(
                hw, h, px(R_CTL), ground=p["layer"], **kw)).decode())
            # not in the img() cache: these are as wide as the window and would pile up a copy
            # per pixel of a resize drag. The selected row carries the nav's 3 x 16 bar at x 4
            g = p["layer"]
            bar = (px(SP[0]), px(3), px(16), max(1, px(1.5)), p["ring"])
            self.hl = {"w": hw, "hover": png(fill=p["sub_hover"](g)), "sel": png(fill=p["sub"](g), bar=bar),
                       "focus": png(fill=p["sub"](g), bar=bar, border=p["ring"], border_w=px(2))}
        self.hl_id = {k: c.create_image(hx, 0, anchor="nw", image=self.hl[k], state="hidden")
                      for k in ("hover", "sel")}     # created first: the row text draws over them
        right = hx + hw - px(SP[2])                  # the highlight's inner right edge
        mark, hg = ui.mark(px(10), p["ring"]), px(H_GROUP)   # the draft mark: the m at 10 px
        mono, now, day, y = ui.mf["mono9"], time.time(), None, 0
        for idx in range(len(items) - 1, -1, -1):
            it = items[idx]
            d = day_of(it["t"], now)
            if d != day:          # a day header: meta muted, its text 4 above the first row
                day = d
                c.create_text(x0, y + hg - px(SP[0]), anchor="sw", text=d, font=ui.mf["meta"],
                              fill=p["muted"])
                self.rows.append((None, y, hg))
                self.ys.append(y)
                y += hg
            body = " ".join(it["text"].split())
            t = time.strftime("%H:%M", time.localtime(it["t"]))
            draft = it.get("draft")
            if self.two:      # line 1 y+8..26: the words; line 2 y+28..44: the time, then the mark
                c.create_text(x0, y + px(17), anchor="w", font=ui.mf["body"], fill=p["ink"],
                              text=ellipsize(ui.mf["body"], body, right - x0))
                c.create_text(x0, y + px(36), anchor="w", text=t, fill=p["muted"], font=mono)
                if draft:
                    c.create_image(x0 + mono.measure(t) + px(SP[1]), y + px(36), anchor="w", image=mark)
            else:             # the time right-aligned in its column, the mark 8 before it
                c.create_text(right, y + h / 2, anchor="e", text=t, fill=p["muted"], font=mono)
                room = right - px(W_TIME) - px(SP[2]) - x0 - (px(10) + px(SP[1]) if draft else 0)
                c.create_text(x0, y + h / 2, anchor="w", font=ui.mf["body"], fill=p["ink"],
                              text=ellipsize(ui.mf["body"], body, room))
                if draft:
                    c.create_image(right - px(W_TIME) - px(SP[1]), y + h / 2, anchor="e", image=mark)
            self.rows.append((it, y, h))
            self.ys.append(y)
            y += h
        c.configure(scrollregion=(0, 0, w, self.natural()))
        self.paint()

    def empty(self, w, filtered=False) -> None:
        """Hung on the one left edge, at the top: centred in the whole column it was an orphan
        half a screen below the header it belongs to, and the widest muted line in the app was
        the first thing the eye landed on. The mark at 32 px in `stroke_field` over the line,
        then "Hold [Ctrl] [Win] and talk." with real keycaps, then the retention sentence.
        `filtered`: there are dictations, the filter just matches none - one muted line."""
        ui, p = self.ui, self.ui.pal
        days = ui.cfg.get("retention_days", 0)
        off = not days
        x, f, gap = ui.px(SP[3]), ui.mf["body"], ui.px(SP[0])
        lh = f.metrics("linespace")
        width = min(ui.measure, max(w - 2 * x, ui.px(120)))     # E to E-from-the-right
        if filtered:
            self.create_text(x, 0, anchor="nw", text="No dictations match.", font=f, fill=p["muted"])
            return
        m = ui.mark(ui.px(32), p["stroke_field"])
        self.create_image(x, 0, anchor="nw", image=m)
        y = m.height() + ui.px(SP[2])
        self.create_text(x, y, anchor="nw", text="History is off" if off else "No dictations yet",
                         font=f, fill=p["ink"])
        y += lh + gap
        if off:
            self.create_text(x, y, anchor="nw", font=f, fill=p["muted"], width=width, justify="left",
                             text="Set ‘Keep dictations for’ in Settings to keep dictations.")
            return
        # "Hold", the two caps (opaque on the layer, a mono9 glyph centred on each), "and talk."
        # - on one centre line; when the pane is too narrow for the sentence (the Promptify
        # list at 176) the tail drops to the next line rather than clip at the hairline
        cy = y + lh // 2
        cx = x + f.measure("Hold ")
        self.create_text(x, cy, anchor="w", text="Hold ", font=f, fill=p["muted"])
        for cap in ("Ctrl", "Win"):
            img = ui.kbd_img(cap, ground=p["layer"])
            self.create_image(cx, cy, anchor="w", image=img)
            self.create_text(cx + img.width() // 2, cy, text=cap, font=ui.mf["mono9"], fill=p["muted"],
                             tags=("cap",))
            cx += img.width() + gap
        tail = " and talk."
        if cx + f.measure(tail) > x + width and w > 1:
            y += lh + gap
            cx, cy, tail = x, y + lh // 2, tail.strip()
        self.create_text(cx, cy, anchor="w", text=tail, font=f, fill=p["muted"])
        if self.two:          # the retention sentence is History's; the pane beside it has a header
            return
        self.create_text(x, y + lh + gap, anchor="nw", font=f, fill=p["muted"],
                         width=width, justify="left",
                         text="What you say is typed where your cursor is, and kept here for "
                              f"{days:g} days.")

    def paint(self) -> None:
        ui = self.ui
        for key, want in (("hover", self.hover), ("sel", ui.sel)):
            item = self.hl_id.get(key)
            if item is None:
                continue
            # `want is None` must not find a header row (its item is None too)
            y = None if want is None else next((y for it, y, _ in self.rows if it is want), None)
            if y is None or (key == "hover" and want is ui.sel):
                self.itemconfigure(item, state="hidden")
            else:
                self.coords(item, ui.px(SP[0]), y)
                self.itemconfigure(item, state="normal")
                if key == "sel":     # the keyboard's row wears the focus ring (focus-visible:
                    self.itemconfigure(item, image=self.hl[      # keyboard focus only, `ui.kbd`)
                        "focus" if self.focus_get() is self and ui.kbd else "sel"])

    def row_at(self, y):
        """The item under widget y, or None (a header, the air below the last row)."""
        cy = self.canvasy(y)
        i = bisect.bisect_right(self.ys, cy) - 1
        return self.rows[i][0] if i >= 0 and cy < self.rows[i][1] + self.rows[i][2] else None

    def set_hover(self, it) -> None:
        if it is not self.hover:
            self.hover = it
            self.paint()

    def _click(self, e) -> None:
        self.focus_set()
        it = self.row_at(e.y)
        if it is not None:
            self.on_select(it)

    def move(self, step) -> None:
        """Up/Down: the next item row (headers skipped). Only the keyboard gets here, so the
        selection it lands on wears the focus ring."""
        self.ui.kbd = True
        order = [r[0] for r in self.rows if r[0] is not None]
        if not order:
            return
        i = next((j for j, it in enumerate(order) if it is self.ui.sel), -1)
        self.on_select(order[max(0, min(len(order) - 1, i + step))])
        self.show_sel()

    def show_sel(self) -> None:
        """Scroll the selected row into view - once the canvas has a size: `go()` asks at idle
        time and a not-yet-laid-out canvas is 1 px high, where every row is "below the fold"
        (that used to scroll a fresh list 35 px down before its first paint)."""
        top = next((y for it, y, _ in self.rows if it is self.ui.sel), None)
        total, vis = max(1, self.natural()), self.winfo_height()
        if top is None or vis <= 1:
            return
        if top < self.canvasy(0):
            self.yview_moveto(top / total)
        elif top + self.H > self.canvasy(0) + vis:
            self.yview_moveto((top + self.H - vis) / total)


class AppWindow:
    """root: the (withdrawn) Tk root. cfg: the live config dict shared with the app.
    on_save(cfg): persist settings. links: {"vocab": fn, "folder": fn} for the sidebar footer.
    theme: "light"/"dark" to override the Windows setting (tests and screenshots); a
    "theme" of "light"/"dark" in cfg (Settings > Appearance) wins over both.
    icon: path of an .ico for the title bar (else Tk's feather)."""

    def __init__(self, root: tk.Tk, history: History, cfg: dict, on_save, scale: float = 1.0,
                 get_app=lambda: None, links=None, theme=None, icon=None):
        self.root = root
        self.get_app = get_app
        self.history = history
        self.cfg = cfg
        self.on_save = on_save
        self.scale = scale
        self.links = links or {}
        self.theme = theme
        self.ico = icon           # the title bar's .ico path (`icon()` is the glyph renderer)
        self.win = None
        self.view = "history"     # remembered between show() and hide()
        self.sel = None           # the selected item itself: survives a delete + undo
        self.imgs = {}            # Tk drops an image nobody references
        self.jobs = {}            # named after() ids, so a second flash cancels the first
        self.ctl = {}             # the settings controls, by cfg key (the tests drive these)
        self.foot_links = []      # sidebar footer links (canvas text items), one per entry in `links`
        self.caps_all = []        # every keycap Label (`keycap`): the shortcut-coverage check reads it
        self.motion = True        # Windows' client-area animation setting, read in _build
        self.nav_bar = None       # the sidebar's 3 x 16 indicator bar (a canvas item) once built
        self.nav_over = None      # the nav row name / footer link item under the pointer
        self.filter = None        # the History filter field once it exists (Ctrl+F, Esc clears)
        self.cards = []           # the settings group cards, top to bottom
        self.primaries = []       # every primary _Btn, so a check can count the ones on screen
        self.undo = None          # (index, item) while the undo offer stands
        self.drafting = None      # the item a draft is being written for, while the worker runs
        self.draft_item = None    # ... the last one (kept name)
        self.skel_at = None       # when the drafting skeleton appeared (a landed draft waits its 300 ms)
        self.last_call = None     # (item, prompts, questions, answers) of the last draft: Try again
        self.p_item = None        # the item the Promptify detail pane shows
        self.p_memo = None        # what the pane was last built for: go() skips a rebuild
        self._eng_cache = {}      # engine key -> (connection state, when): the sheet clears it
        self.pstate = "pick"      # ... and in which state (STATES)
        self.p_err = None         # (item, message) after a failed draft, until the next attempt
        self.p_fields = {}        # the draft body's live widgets: prompts, questions, answers...
        self.eng_err = {}         # engine key -> its last failure message (the sheet shows it)
        self.logins = {}          # engine key -> {"login", "ev", "url"} while its sign-in runs

    def px(self, n) -> int:
        return max(1, int(round(n * self.scale)))

    # --- lifecycle ---------------------------------------------------------------------------
    def prebuild(self) -> None:
        """Build the window while nobody is waiting (run_app calls this once the model is
        ready): the first triple-tap then costs a deiconify, not the 1.4 s build."""
        if self.win is None or not self.win.winfo_exists():
            self._build()

    def show(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.refresh()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self) -> None:
        if self.win is not None:
            # no <Leave> is delivered to a withdrawn toplevel, so the wheel binding installed on
            # the root's "all" tag would outlive the window and scroll a canvas nobody can see
            self.win.unbind_all("<MouseWheel>")
            self.win.withdraw()

    def _build(self) -> None:
        chosen = self.cfg.get("theme")
        self.dark = (chosen == "dark") if chosen in ("light", "dark") else \
            (self.theme == "dark") if self.theme else system_dark()
        # the brand green, not the bar's colour: the bar colour is the user's per-take signal and
        # can be anything (their red made the whole window pink); the window is the product's
        p = self.pal = palette(brand.GREEN, self.dark)
        # three hierarchy levels - title 12/600 ink, body 10/400 ink, meta 10/400 MUTED (the
        # same size as body: the colour is the level) - and mono as a register, not a level
        self.F = {"title": ("Segoe UI Semibold", 12),   # 16 px: view titles, the wordmark
                  "body": ("Segoe UI", 10),             # 13.33 px: everything else in ink
                  "meta": ("Segoe UI", 10),             # body's size, muted
                  "mono": ("Cascadia Mono", 10),        # hex field, days, model ids, the slider's digits
                  "mono9": ("Cascadia Mono", 9)}        # times, keycaps, the trigger-key cap
        w = self.win = tk.Toplevel(self.root)
        w.withdraw()                                  # built hidden; show() deiconifies
        w.title("murmur")
        if self.ico and Path(self.ico).exists():
            try:
                w.iconbitmap(str(self.ico))
            except tk.TclError:
                pass
        # the .ico alone leaves the title bar stretching a small frame at high DPI (it read as
        # pixelated at 200%): hand Tk exact-size renders and let it pick per surface
        try:
            import io
            self._icons = []
            for s in (self.px(16), self.px(32)):
                buf = io.BytesIO()
                brand.tile(s).save(buf, "PNG")
                self._icons.append(tk.PhotoImage(master=w, data=buf.getvalue()))
            w.iconphoto(False, *self._icons)
        except Exception:
            pass
        w.configure(bg=p["layer"])
        w.geometry(f"{self.px(WIN[0])}x{self.px(WIN[1])}")
        w.minsize(self.px(WIN_MIN[0]), self.px(WIN_MIN[1]))
        w.protocol("WM_DELETE_WINDOW", self.hide)
        self.mf = {k: tkfont.Font(w, family=f[0], size=f[1]) for k, f in self.F.items()}
        self.measure = self.mf["body"].measure(SAMPLE80)    # the body column's width, device px
        # focus-visible: rings mark keyboard focus. A mouse click that happens to focus a
        # control keeps the ring off - the click already shows its result, and the sudden
        # green box around a whole segment read as a glitch.
        self.kbd = True                                     # Tab (and the tests) count as keyboard
        # on the toplevel's bindtag, which runs BEFORE the "all" tag that moves focus on Tab -
        # so the FocusIn that follows already sees keyboard mode
        w.bind("<Tab>", lambda e: (setattr(self, "kbd", True), self._tab(False))[1], add="+")
        w.bind("<Shift-Tab>", lambda e: (setattr(self, "kbd", True), self._tab(True))[1], add="+")
        w.bind("<Button>", lambda e: setattr(self, "kbd", False), add="+")
        # ... a key press says keyboard, and the highlight-ring widgets (`focus_visible`)
        # repaint their ring from the toplevel's tag, where nothing shadows or replaces it
        ring = lambda e: getattr(e.widget, "ring_upd", lambda: None)()
        w.bind("<KeyPress>", lambda e: (setattr(self, "kbd", True), ring(e)), add="+")
        w.bind("<FocusIn>", ring, add="+")
        # a card starts 12 px before the pane's text edge E (like a row highlight) and spends
        # 1 px on its hairline, so its padding is 12 minus that px - which puts every line of
        # text inside it back on E, cards or no cards
        self.cpad = self.px(SP[2]) - 1
        # reduced motion: the nav bar snaps and a hover skips its blend frame when Windows'
        # client-area animation is off (SPI_GETCLIENTAREAANIMATION); unreadable means on
        self.motion = True
        try:
            import ctypes
            anim = ctypes.c_int(1)
            if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(anim), 0):
                self.motion = bool(anim.value)
        except Exception:
            pass
        self._ttk()
        if self.dark:
            self._dark_titlebar()
        w.grid_columnconfigure(2, weight=1)
        w.grid_rowconfigure(0, weight=1)
        self.side = self._sidebar()
        self.side.grid(row=0, column=0, sticky="ns")
        # the content layer's rule: the only separator between the sidebar and the content
        self.hairline(w, vertical=True, color=p["stroke"](p["layer"])).grid(row=0, column=1, sticky="ns")
        content = tk.Frame(w, bg=p["layer"])
        content.grid(row=0, column=2, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        self.views = {}
        for name, build in (("history", self._build_history), ("promptify", self._build_promptify),
                            ("settings", self._build_settings)):
            f = tk.Frame(content, bg=p["layer"])
            f.grid(row=0, column=0, sticky="nsew")
            self.views[name] = f
            build(f)
        w.bind("<Escape>", lambda e: self._escape())
        w.bind("<Up>", lambda e: self._nav_key(-1))
        w.bind("<Down>", lambda e: self._nav_key(1))
        w.bind("<Control-Return>", lambda e: self.view == "promptify" and self._update())   # Update prompt
        # the views by number (the caps on the nav rows); Settings on the platform's Ctrl+, and
        # close on Ctrl+W (undrawn synonyms of the Settings row and Esc); the filter on Ctrl+F
        for i, name in enumerate(("history", "promptify", "settings"), 1):
            w.bind(f"<Control-Key-{i}>", lambda e, n=name: self.go(n))
        w.bind("<Control-comma>", lambda e: self.go("settings"))
        w.bind("<Control-w>", lambda e: self.hide())
        w.bind("<Control-f>", lambda e: self._focus_filter())
        self.go(self.view)

    def _ttk(self) -> None:
        p, st = self.pal, ttk.Style(self.win)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        # the combobox sits inside a rounded field image (see `_field`), so every edge clam
        # would draw is painted out in the fill colour - the image is the only border, and the
        # focus ring is the image's too
        fld = p["ctl"]
        st.configure("M.TCombobox", fieldbackground=fld, background=fld, foreground=p["ink"],
                     bordercolor=fld, arrowcolor=p["muted"], lightcolor=fld, darkcolor=fld,
                     insertcolor=p["ink"], selectbackground=p["sub_hover"](fld), selectforeground=p["ink"],
                     padding=(0, 4), arrowsize=self.px(SP[1]))   # clam's arrow is a 2 px sliver
        st.map("M.TCombobox", fieldbackground=[("readonly", fld)],
               foreground=[("readonly", p["ink"])], bordercolor=[("focus", fld)],
               arrowcolor=[("active", p["ink"])])
        # the popdown: a `card` with a hairline round it (the Listbox's highlight ring is the
        # one border Tk lets it have), rows selected in the card's `sub`
        for k, v in (("*TCombobox*Listbox.background", p["card"]),
                     ("*TCombobox*Listbox.foreground", p["ink"]),
                     ("*TCombobox*Listbox.selectBackground", p["sub"](p["card"])),
                     ("*TCombobox*Listbox.selectForeground", p["ink"]),
                     ("*TCombobox*Listbox.borderWidth", 0),
                     ("*TCombobox*Listbox.relief", "flat"),
                     ("*TCombobox*Listbox.highlightThickness", 1),
                     ("*TCombobox*Listbox.highlightBackground", p["stroke"](p["card"])),
                     ("*TCombobox*Listbox.highlightColor", p["stroke"](p["card"]))):
            self.root.option_add(k, v)
        self.root.option_add("*TCombobox*Listbox.font", self.mf["body"])
        # clam's scrollbar is as thick as its ARROWS, arrows or no arrows (`width` is ignored),
        # and spends 1 px of that on a `bordercolor` strip either side of the thumb: the strips
        # are painted in the ground's colour and the widget made 2 px wider, so the thumb that
        # shows is the 4 px the spec draws. The thumb's own light/dark edges are its colour.
        # Two styles, one per ground: "M" on the layer, "C" inside a card.
        for name, ground in (("M", p["layer"]), ("C", p["card"])):
            style = f"{name}.Vertical.TScrollbar"
            st.layout(style,     # no arrows: trough + thumb
                      [("Vertical.Scrollbar.trough",
                        {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                                       {"expand": "1", "sticky": "nswe"})]})])
            st.configure(style, troughcolor=ground, background=p["stroke_strong"], bordercolor=ground,
                         darkcolor=p["stroke_strong"], lightcolor=p["stroke_strong"],
                         gripcount=0,           # clam's thumb draws grip ticks unless told not to
                         arrowsize=self.px(SP[0]) + 2)
            st.map(style, background=[("active", p["muted"])],
                   darkcolor=[("active", p["muted"])], lightcolor=[("active", p["muted"])])

    def _dark_titlebar(self) -> None:
        """DWMWA_USE_IMMERSIVE_DARK_MODE; 20 on current Windows 10, 19 on older builds."""
        try:
            import ctypes
            self.win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
            v = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4) == 0:
                    break
        except Exception:
            pass

    # --- small parts -------------------------------------------------------------------------
    def focus_visible(self, wdg, ground) -> None:
        """Wire a highlight-ring widget to show its ring only under keyboard focus; a later
        key press while focused (arrows) brings the ring back. The widget carries only the
        painter (`ring_upd`); the toplevel's own <FocusIn> / <KeyPress> bindings (`_build`)
        call it. On the widget's own tag it was shadowed: Tk fires ONE binding per tag, the
        most specific, so the widget's <Left> / <space> handlers beat a <KeyPress> there, and
        a later plain `bind("<FocusIn>")` on the widget replaced the ring's (the colour strip
        rang on a mouse click)."""
        wdg.ring_upd = lambda: wdg.configure(highlightcolor=self.pal["ring"] if self.kbd else ground)

    def hairline(self, parent, vertical=False, color=None) -> tk.Frame:
        """A 1 px rule; by default an inset `divider` on the layer."""
        n = {"width" if vertical else "height": 1}
        return tk.Frame(parent, bg=color or self.pal["divider"](self.pal["layer"]), **n)

    def img(self, key, make) -> tk.PhotoImage:
        if key not in self.imgs:
            self.imgs[key] = tk.PhotoImage(data=base64.b64encode(make()).decode())
        return self.imgs[key]

    def mark(self, size, color) -> tk.PhotoImage:
        """The brand m drawn in a `size` px box, in `color`, then CROPPED to its ink - brand.mark
        keeps the SVG's own margins, and a lockup has to put the shape's real feet on a baseline.
        (The crop must go by the alpha channel: every pixel carries the colour, only alpha says
        where the ink is.) Cached like every other rendered image; the PhotoImage's width()/
        height() then hand the layout the mark's true size at any DPI."""
        def make():
            import brand
            im = brand.mark(size, color)
            buf = io.BytesIO()
            im.crop(im.getchannel("A").getbbox()).save(buf, "PNG")
            return buf.getvalue()
        return self.img(("mark", size, color), make)

    def rr(self, w, h, r, fill, ground, border=None, bw=1, bar=None, inner=None, edge=None,
           under=None) -> tk.PhotoImage:
        """Cached rounded rect, sizes in device px. Every control's corner comes from here."""
        return self.img(("rr", w, h, r, fill, ground, border, bw, bar, inner, edge, under),
                        lambda: rr_png(w, h, r, fill, border, bw, ground, bar, inner, edge, under))

    def dot(self, box, d, fill, edge=None) -> tk.PhotoImage:
        """A `d` px dot in a `box` px image, sizes in logical px: the state dots. `layer` for
        the fill with a `stroke_field` edge is the hollow one (not connected)."""
        return self.img(("dot", box, d, fill, edge),
                        lambda: dot_png(self.px(box), self.px(d), fill, edge=edge))

    def icon(self, name, colour, size=ICON, ground=None) -> tk.PhotoImage:
        """A `GLYPHS` icon in `colour`, `size` logical px, rendered on first use and cached."""
        return self.img(("ic", name, colour, size, ground),
                        lambda: icon_png(name, colour, self.px(size), ground))

    # --- keycaps: the raised-key recipe at 20 px --------------------------------------------
    def kbd_png(self, label, h=H_KBD, w=None, ground=None, fade=False) -> bytes:
        """The cap's image: `ctl` with a hairline and the elevation edge (the "bottom lip"),
        transparent corners so it sits on the grain, a card or the layer alike - or opaque on
        `ground` where the ground is known (a footer): Tk blends a partial-alpha image by
        reading the surface back first, ~0.2 ms per draw. A single glyph is an h x h square;
        a word is its width in mono9 plus 6 a side; `w` overrides. `fade` is the disabled
        look - fill, hairline and edge 45 % toward the ground, as a button's (`_Btn.enable`):
        a cap fades with the action it names. Bytes, for `compose_png`."""
        p = self.pal
        w = w or max(self.px(h), self.mf["mono9"].measure(label) + 2 * self.px(6))
        fill, border, edge = p["ctl"], p["stroke"](p["ctl"]), (p["stroke_edge"], p["stroke_top"])
        if fade:
            g = ground or p["layer"]
            f = lambda c: mix(c, g, .45) if c else c
            fill, border, edge = f(fill), f(border), tuple(f(c) for c in edge)
        return rr_png(w, self.px(h), self.px(R_KBD), fill, border, 1, ground, edge=edge)

    def kbd_img(self, label, h=H_KBD, w=None, ground=None, fade=False) -> tk.PhotoImage:
        """`kbd_png` as a cached PhotoImage (one per label, size and ground; a cap at a fixed
        `w` is the same image whatever it says - the trigger key's, relabelled in place)."""
        return self.img(("kbd", None if w else label, h, w, ground, fade),
                        lambda: self.kbd_png(label, h, w, ground, fade))

    def keycap(self, parent, label, h=H_KBD, fg=None, w=None) -> tk.Label:
        """A decorative keycap (`kbd` is taken: the focus-visible flag): not focusable, not
        clickable, mono9 `muted` (or `fg`) text centred on its image - rendered opaque on the
        parent's ground (a Label knows it), so no read-back blend per paint. Every cap is
        registered in `caps_all` for the coverage check."""
        l = tk.Label(parent, text=label, font=self.F["mono9"], fg=fg or self.pal["muted"],
                     bg=parent["bg"], compound="center", bd=0, padx=0, pady=0,
                     highlightthickness=0, takefocus=0, image=self.kbd_img(label, h, w, parent["bg"]))
        self.caps_all.append(l)
        return l

    def _corners(self, parent, r, fill, ground, border=None) -> list:
        """Pin the four masks of `corner_pngs` to a frame's corners. Relative placement means
        the frame can stretch with the window and nothing has to be re-rendered."""
        out = []
        for i, (rx, ry, anchor) in enumerate(((0.0, 0.0, "nw"), (1.0, 0.0, "ne"),
                                             (1.0, 1.0, "se"), (0.0, 1.0, "sw"))):
            l = tk.Label(parent, bd=0, highlightthickness=0, bg=ground, padx=0, pady=0,
                         image=self.img(("cn", r, fill, ground, border, i),
                                        lambda i=i: corner_pngs(r, fill, ground, border)[i]))
            l.place(relx=rx, rely=ry, anchor=anchor)
            out.append(l)
        return out

    def _card(self, parent, radius=None) -> tk.Frame:
        """A grouped surface: `card` fill, 1 px `stroke` hairline, rounded. The hairline is the
        outer frame showing through a 1 px margin - a Tk highlight ring cannot be rounded, and
        place() measures from inside it. In dark a 1 px `card_inset` line lies along the inside
        of the top edge (placed, so it costs no height); the corner masks are created after
        `inner` and so sit above it, and the line stops at the arcs. No shadow, no 9-slice,
        nothing re-renders on resize. Rows go in `.body`."""
        p, ground = self.pal, parent["bg"]
        outer = tk.Frame(parent, bg=p["stroke"](ground))
        inner = tk.Frame(outer, bg=p["card"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        if p["card_inset"]:
            tk.Frame(inner, bg=p["card_inset"], height=1).place(x=0, y=0, relwidth=1.0)
        self._corners(outer, self.px(radius or R_CARD), p["card"], ground, p["stroke"](ground))
        outer.body = inner
        return outer

    def link(self, parent, text, cmd, font=None) -> tk.Label:
        p = self.pal
        l = tk.Label(parent, text=text, font=font or self.F["meta"], fg=p["muted"],
                     bg=parent["bg"], cursor="hand2")
        l.bind("<Enter>", lambda e: l.configure(fg=p["ink"]))
        l.bind("<Leave>", lambda e: l.configure(fg=p["muted"]))
        l.bind("<Button-1>", lambda e: cmd())
        return l

    def _cancel(self, key) -> None:
        """Drop a named after() job, if one is pending."""
        job = self.jobs.pop(key, None)
        if job:
            self.win.after_cancel(job)

    def _say(self, label, text, icon=None) -> None:
        """Write a status label. A footer's flash label has a sibling icon Label (`label.icon`,
        `_flash_slot`) that shows before the text only while `icon` names a glyph - so a
        "Deleted ·" written after a "Copied" never keeps the check."""
        label.configure(text=text)
        ic = getattr(label, "icon", None)
        if ic is not None:
            if icon:
                ic.configure(image=self.icon(icon, self.pal["ring"]))
                ic.pack(side="left", before=label, padx=(0, self.px(SP[0])))
            else:
                ic.pack_forget()

    def flash(self, label, text, ms=2000, icon=None) -> None:
        self._say(label, text, icon)
        self._cancel(id(label))
        self.jobs[id(label)] = self.win.after(ms, lambda: self._say(label, ""))

    def _flash_slot(self, parent) -> tk.Label:
        """A footer's flash: a meta muted Label with a hidden check icon (`ring`) before it,
        reachable as `.icon` for `_say`/`flash`."""
        p = self.pal
        l = tk.Label(parent, text="", font=self.F["meta"], fg=p["muted"], bg=parent["bg"],
                     bd=0, padx=0, pady=0)
        l.pack(side="left")
        l.icon = tk.Label(parent, bg=parent["bg"], bd=0, padx=0, pady=0,
                          image=self.icon("check", p["ring"]))
        return l

    def _chord_items(self, c, x, cy, caps, text_tags, img_tags=()) -> tuple:
        """A cap chord as canvas items from x, centred on cy: per cap its `kbd_img` (opaque on
        the canvas's own ground; the image tagged `cap:<label>` too, so a re-render knows its
        cap) and a mono9 muted text item carrying `text_tags` ("cap" among them, for the
        coverage check), 4 apart. Returns (the x after the last cap, the text item ids)."""
        p, ids = self.pal, []
        for j, cap in enumerate(caps):
            x += self.px(SP[0]) if j else 0
            img = self.kbd_img(cap, ground=c["bg"])
            c.create_image(x, cy, anchor="w", image=img, tags=tuple(img_tags) + ("cap:" + cap,))
            ids.append(c.create_text(x + img.width() // 2, cy, text=cap, font=self.F["mono9"],
                                     fill=p["muted"], tags=text_tags))
            x += img.width()
        return x, ids

    def _footer(self, parent, hints, edit_hints=None) -> tk.Canvas:
        """The keyboard-hint strip at the foot of a view: a 28 px row under a `divider` rule,
        hints from E (a cap chord + a meta muted word, 16 apart), the flash slot `.right` at
        the right edge. A hint names only keys NOT already drawn beside an action on that view
        (complementary).

        The footer IS one canvas - the rule a line item, the caps their `kbd_img` + a mono9
        text item tagged "cap" (the coverage check reads those) grouped by a chord tag
        (`view0`, `edit1`...), the words text items - and not a strip of cap Labels: on Windows
        every widget is its own HWND and a raise-based view switch pays ~0.3 ms per HWND in
        the raised stack (measured 2026-09-08: 40 empty Frames = +13 ms), so a footer of ~25
        Labels and Frames cost more than the whole switch did before it. With `edit_hints` a
        second item set is drawn and `.hints("view"|"edit")` shows one and hides the other.
        `.right` is a Frame PLACED over the canvas at its right edge, so in a narrow window the
        hints run on under it and clip there - the flash slot stays. `.fade(k, on)` fades the
        k-th view hint (caps and word, tag `view<k>h`) with the action it names - the
        `disabled` look while that action is - and only on a change, so it costs nothing on
        a pane swap that leaves it alone."""
        p, px = self.pal, self.px
        c = tk.Canvas(parent, bg=p["layer"], height=px(H_FOOT), highlightthickness=0, bd=0)
        rule = c.create_line(0, 0, 1, 0, fill=p["divider"](p["layer"]))
        c.bind("<Configure>", lambda e: c.coords(rule, 0, 0, e.width, 0))
        c.right = tk.Frame(c, bg=p["layer"])
        c.right.place(relx=1.0, x=-px(SP[3]), y=1, relheight=1.0, height=-1, anchor="ne")
        cy = px(H_FOOT) // 2
        for which, items in (("view", hints), ("edit", edit_hints or ())):
            x = px(SP[3])
            for k, (caps, word) in enumerate(items):
                x += px(SP[3]) if k else 0
                grp = f"{which}{k}h"                 # the hint as one group: its caps and word
                x = self._chord_items(c, x, cy, caps, (which, "cap", f"{which}{k}", grp), (which, grp))[0]
                x += px(SP[1])
                c.create_text(x, cy, anchor="w", text=word, font=self.F["meta"], fill=p["muted"],
                              tags=(which, grp))
                x += self.mf["meta"].measure(word)
        c.hints = lambda which="view": [c.itemconfigure(k, state="normal" if k == which else "hidden")
                                        for k in ("view", "edit")]
        c.faded = set()

        def word(k, text):
            """Re-word the k-th view hint (`↵ promptify` reads `↵ copy prompt` while the row's
            Return copies) and shift the hints after it by the difference; change-only."""
            i = next(i for i in c.find_withtag(f"view{k}h") if c.type(i) == "text" and "cap" not in c.gettags(i))
            old = c.itemcget(i, "text")
            if old == text:
                return
            c.itemconfigure(i, text=text)
            dx = self.mf["meta"].measure(text) - self.mf["meta"].measure(old)
            for j in range(k + 1, len(hints)):
                c.move(f"view{j}h", dx, 0)
        c.word = word

        def fade(k, on):
            if (k in c.faded) == bool(on):
                return
            (c.faded.add if on else c.faded.discard)(k)
            for i in c.find_withtag(f"view{k}h"):
                if c.type(i) == "image":
                    cap = next(t[4:] for t in c.gettags(i) if t.startswith("cap:"))
                    c.itemconfigure(i, image=self.kbd_img(cap, ground=c["bg"], fade=bool(on)))
                else:
                    c.itemconfigure(i, fill=p["disabled" if on else "muted"])
        c.fade = fade
        c.hints()
        return c

    def filter_text(self) -> str:
        """The History filter's text, lower-cased; "" without the field."""
        return self.filter.value().strip().lower() if self.filter is not None else ""

    def _focus_filter(self) -> None:
        """Ctrl+F from any view: the History view, with the caret in the filter field (which
        is hidden while there is nothing to filter)."""
        self.go("history")
        if self.filter is not None and self.filter.master.winfo_ismapped():
            self.filter.focus_set()

    # --- sidebar -----------------------------------------------------------------------------
    def _sidebar(self) -> tk.Canvas:
        """ONE canvas: grain tiles under everything, the specular band, the lockup, three nav
        rows, the indicator bar, the footer links - every one a canvas item, so the grain shows
        through each transparent PNG corner and hover is an `itemconfigure`. Stacking is
        creation order.

        The lockup: mark + "murmur" on ONE baseline (both anchored "sw", the text one descent
        lower - where a "sw" text hangs its box). The word sits where a title Label sits in a
        32 px row at y 16, so it shares a baseline with the view titles across the rule; a mark
        taller than that row pokes UP into the top pad by `rise` (kept on `side.rise`) and the
        nav below starts at 64 whatever the mark's size. The mark in the brand green (the user's
        ask 2026-08-29), the word muted: up here the identity is chrome.

        A nav row is ONE image item (`_nav_img`: the row's slice of the grain, the pill, the
        icon and the chord's two cap boxes composed per state - in idle just the grain, the
        icon and the boxes) plus its label and cap-digit text items. The image is the row's
        hit area in every state, so the whole 176 x 36 row is clickable; a canvas hit-tests an
        image by its bounding box whatever its alpha (checked with a real click on a fully
        transparent one, 2026-09-08). Hover is tracked by geometry on <Motion> rather than
        per-item <Enter>/<Leave>: crossing from the pill onto the label inside one row would
        otherwise leave-then-enter the row and replay the blend frame as a dip."""
        p, px, F = self.pal, self.px, self.F
        base = p["base"]
        s = self.side = tk.Canvas(self.win, bg=base, width=px(W_SIDE), highlightthickness=0, bd=0,
                                  takefocus=0)
        # 1. the grain, tiled once to the screen's height (the canvas clips; a taller monitor
        # later gets more rows from `_side_resized`)
        tile = px(96)
        s.grain = grain_png(tile, base, p["grain"])   # the bytes too: `_nav_img` bakes the grain in
        s.tile = self.img(("grain", tile, base, p["grain"]), lambda: s.grain)
        s.tiled = 0
        self._side_tiles(s, self.win.winfo_screenheight() + tile)
        # 2. the light on the top: white at .40 / .045 fading to nothing over 120 px
        s.spec = specular_png(px(W_SIDE), px(120), p["specular"])
        s.create_image(0, 0, anchor="nw", image=self.img(("spec", px(W_SIDE), px(120), p["specular"]),
                                                         lambda: s.spec))
        # 3. the lockup
        f = self.mf["title"]
        m = self.mark(px(H_MARK), brand.GREEN)
        lh, desc, row = f.metrics("linespace"), f.metrics("descent"), px(H_CTL)
        base_y0 = (row - lh) // 2 + lh - desc             # the baseline's y inside the title row
        s.rise = max(0, m.height() - base_y0)
        base_y = px(PAD_TOP) + base_y0
        s.create_image(px(SP[3]), base_y, anchor="sw", image=m, tags="lock_mark")
        s.create_text(px(SP[3]) + m.width() + px(GAP_MARK), base_y + desc, anchor="sw",
                      text="murmur", font=F["title"], fill=p["muted"], tags="lock_text")
        # 4. the nav rows: the row image (the hit area), label, the Ctrl-digit chord's texts at
        # the right; the chord's boxes are the same for every row (a digit's cap is a square)
        x, w, h = px(SP[1]), px(W_SIDE - 2 * SP[1]), px(H_NAV)
        y0 = px(PAD_TOP) + px(H_CTL) + px(SP[3])
        wd, wc, cap_y = self.kbd_img("1").width(), self.kbd_img("Ctrl").width(), (h - px(H_KBD)) // 2
        x_d = w - px(SP[1]) - wd                          # right-aligned, 8 inside the pill
        x_c = x_d - px(SP[0]) - wc
        self.nav_boxes = [(x_c, cap_y, self.kbd_png("Ctrl")), (x_d, cap_y, self.kbd_png("1"))]
        self.nav = {}
        for i, (name, label, glyph) in enumerate((("history", "History", "history"),
                                                  ("promptify", "Promptify", "sparkle"),
                                                  ("settings", "Settings", "options"))):
            y, tag = y0 + i * (h + px(SP[0])), "nav_" + name
            self.nav[name] = row = dict(y=y, glyph=glyph, state="idle")
            row["pill"] = s.create_image(x, y, anchor="nw", image=self._nav_img(name, "idle"), tags=(tag,))
            # the label 8 after the icon's box (12 + 16): "Promptify" then ends >= 10 before the
            # chord at every DPI (at 40 the 200 % render left 6 - the word measures 58 there)
            row["label"] = s.create_text(x + px(36), y + h // 2, anchor="w", text=label,
                                         font=F["body"], fill=p["muted"], tags=(tag,))
            row["caps"] = tuple(s.create_text(x + cx + cw // 2, y + h // 2, text=text, font=F["mono9"],
                                              fill=p["muted"], tags=(tag, "cap"))
                                for cx, cw, text in ((x_d, wd, str(i + 1)), (x_c, wc, "Ctrl")))
            self._nav_img(name, "sel")                   # rendered now, not on the first switch
            s.tag_bind(tag, "<Button-1>", lambda e, n=name: self.go(n))
        # 5. the indicator bar: its own item, so go() can `coords` it; (36 - 16) / 2 = 10 down
        self.nav_bar = s.create_image(x + px(SP[0]), self.nav[self.view]["y"] + px(10), anchor="nw",
                                      image=self.rr(px(3), px(16), max(1, px(1.5)), p["ring"], None))
        # 6. the footer links, hung from the bottom edge (`_side_resized` sets their y)
        made = [("vocab", "vocab.txt"), ("folder", "config folder")]
        lx, s.foot = px(SP[3]), []
        for i, (key, text) in enumerate([m for m in made if m[0] in self.links]):
            if i:                                    # the dot, 4 px of air either side
                s.foot.append(s.create_text(lx + px(SP[0]), 0, anchor="sw", text="·", font=F["meta"],
                                            fill=p["stroke_field"]))
                lx += self.mf["meta"].measure("·") + 2 * px(SP[0])
            item = s.create_text(lx, 0, anchor="sw", text=text, font=F["meta"], fill=p["muted"],
                                 tags=("link_" + key,))
            s.tag_bind("link_" + key, "<Button-1>", lambda e, fn=self.links[key]: fn())
            s.foot.append(item)
            self.foot_links.append(item)
            lx += self.mf["meta"].measure(text)
        s.bind("<Configure>", lambda e: self._side_resized(e.height))
        s.bind("<Motion>", lambda e: self._side_track(self._nav_at(e.x, e.y) or self._link_at(e.x, e.y)))
        s.bind("<Leave>", lambda e: self._side_track(None))
        return s

    def _side_tiles(self, s, height) -> None:
        """Grain tiles from `s.tiled` down to `height`, under everything already drawn."""
        tile = self.px(96)
        while s.tiled < height:
            for x in range(0, self.px(W_SIDE), tile):
                s.create_image(x, s.tiled, anchor="nw", image=s.tile, tags=("frost",))
            s.tiled += tile
        s.tag_lower("frost")

    def _side_resized(self, height) -> None:
        s = self.side
        if height > s.tiled:                             # a monitor taller than the one at build
            self._side_tiles(s, height)
        for item in s.foot:
            s.coords(item, s.coords(item)[0], height - self.px(SP[3]))

    def _nav_at(self, x, y):
        """The nav row whose pill holds (x, y), or None."""
        px = self.px
        if px(SP[1]) <= x < px(W_SIDE - SP[1]):
            for name, row in self.nav.items():
                if row["y"] <= y < row["y"] + px(H_NAV):
                    return name
        return None

    def _link_at(self, x, y):
        """The footer link item under (x, y), or None."""
        for item in self.foot_links:
            x0, y0, x1, y1 = self.side.bbox(item)
            if x0 <= x < x1 and y0 <= y < y1:
                return item
        return None

    def _side_track(self, over) -> None:
        """The pointer moved: `over` is a nav row name, a link item id or None. Only a CHANGE
        does anything - the row lights (through its blend), the old one goes idle at once,
        links swap ink/muted, the cursor follows."""
        if over == self.nav_over:
            return
        was, self.nav_over = self.nav_over, over
        for target, on in ((was, False), (over, True)):
            if isinstance(target, str):
                self._nav_hover(target, on)
            elif target is not None:
                self.side.itemconfigure(target, fill=self.pal["ink" if on else "muted"])
        self.side.configure(cursor="hand2" if over is not None else "")

    def _nav_img(self, name, state) -> tk.PhotoImage:
        """A nav row's one image for `state` in idle / blend / hover / sel: the pill (none in
        idle; `sub_hover`, its 50 % blend with base, `sub`), the icon (`ink` when lit, else
        `muted`) and the chord's cap boxes, composed OVER the row's own slice of the grain and
        the specular - so the image is opaque (an opaque image skips Tk's read-back blend, the
        last ~1 ms of a switch) and pixel-for-pixel what the transparent one would show: the
        grain runs on under the pill's corners. Cached per (row, state)."""
        p, px, row, s = self.pal, self.px, self.nav[name], self.side
        w, h, base = px(W_SIDE - 2 * SP[1]), px(H_NAV), p["base"]
        gx, gy, tile = px(SP[1]), row["y"], px(96)

        def make():
            from PIL import Image
            under = Image.new("RGBA", (w, h))
            grain = Image.open(io.BytesIO(s.grain))
            for ty in range(gy // tile * tile, gy + h, tile):
                for tx in range(0, gx + w, tile):
                    under.paste(grain, (tx - gx, ty - gy))
            spec = Image.open(io.BytesIO(s.spec)).convert("RGBA")
            if gy < spec.height:
                under.alpha_composite(spec.crop((gx, gy, gx + w, gy + h)))
            buf = io.BytesIO()
            under.save(buf, "PNG")
            fill = {"idle": None, "hover": p["sub_hover"](base), "sel": p["sub"](base),
                    "blend": mix(base, p["sub_hover"](base), .5)}[state]
            parts = [(0, 0, buf.getvalue())]
            if fill:
                parts.append((0, 0, rr_png(w, h, px(R_CTL), fill)))
            parts.append((px(12), (h - px(ICON)) // 2,
                          icon_png(row["glyph"], p["ink"] if fill else p["muted"], px(ICON))))
            return compose_png(w, h, parts + self.nav_boxes)
        return self.img(("nav", name, state), make)

    def _nav_paint(self, name, state) -> None:
        """`state` in idle / blend / hover / sel: the row's image and the label's colour (ink
        whenever the row is lit). A row already in that state is left alone - an
        `itemconfigure` damages the row and the canvas repaints it."""
        row, p, s = self.nav[name], self.pal, self.side
        if row["state"] == state:
            return
        row["state"] = state
        s.itemconfigure(row["pill"], image=self._nav_img(name, state))
        s.itemconfigure(row["label"], fill=p["ink"] if state != "idle" else p["muted"])

    def _nav_hover(self, name, on) -> None:
        """Hover in two frames over `MOTION_FAST` (Fluent's 83 ms): the 50 % blend now, the
        full hover half of that later; leaving is instant and drops a frame still due. The
        selected row does not hover; reduced motion snaps."""
        self._cancel(("blend", name))
        if name == self.view:
            return
        if not on:
            self._nav_paint(name, "idle")
        elif not self.motion:
            self._nav_paint(name, "hover")
        else:
            self._nav_paint(name, "blend")
            self.jobs[("blend", name)] = self.win.after(MOTION_FAST // 2, lambda: (
                self.jobs.pop(("blend", name), None), self._nav_paint(name, "hover")))

    def _slide_bar(self, y0, y1) -> None:
        """The indicator's slide: three frames over `MOTION_NORMAL` (150 ms) after a 40 ms
        delay (the switch has painted by then - the slide is chrome after the switch, never on
        its path), ease-out (0.2, 0, 0, 1) sampled at .55 / .88 / 1."""
        self._cancel("slide")
        x = self.side.coords(self.nav_bar)[0]
        ys = [round(y0 + (y1 - y0) * k) for k in (.55, .88, 1.0)]

        def step(i=0):
            self.side.coords(self.nav_bar, x, ys[i])
            if i + 1 < len(ys):
                self.jobs["slide"] = self.win.after(MOTION_NORMAL // 3, lambda: step(i + 1))
            else:
                self.jobs.pop("slide", None)
        self.jobs["slide"] = self.win.after(40, step)

    def go(self, name) -> None:
        """Every view stays built and gridded in the one cell; a switch raises the target.
        Measured: 16-25 ms against 90-160 ms for grid_remove/grid, which re-laid the whole
        subtree out on every switch. The Promptify pane is rebuilt only when what it shows has
        changed (`p_memo`). Covered views are still in Tk's focus ring, so `_tab` keeps Tab
        inside the shown view. The nav: three `itemconfigure` rows and the bar's `coords` (or
        one `after` that starts its slide) - nothing else is added to a switch."""
        self.view = name
        self._cancel(("blend", name))          # a hover frame due on the row just selected
        for n in self.nav:
            self._nav_paint(n, "sel" if n == name else "idle")
        if isinstance(self.nav_over, str) and self.nav_over != name:
            self._nav_paint(self.nav_over, "hover")       # the pointer is still on that row
        x, y0 = self.side.coords(self.nav_bar)
        y1 = self.nav[name]["y"] + self.px(10)
        if y0 != y1 and self.motion:
            self._slide_bar(y0, y1)
        elif y0 != y1:
            self.side.coords(self.nav_bar, x, y1)
        lst = self._list()
        if lst is not None:                    # both lists share the selection: the one shown
            lst.paint()                        # catches up and brings the row into view
        if name == "promptify":
            state = self._state_for(self.sel)
            if self._pane_key(state) != self.p_memo:
                self._show(state)
        self.views[name].tkraise()
        if lst is not None:                    # named like every job, so `_rebuild` cancels it
            self.jobs["show_sel"] = self.win.after_idle(lst.show_sel)

    def _pane_key(self, state) -> tuple:
        it = self.sel
        return (state, id(it), (it or {}).get("text"), id((it or {}).get("draft")), self.sheet_open)

    def _in_shown(self, w) -> bool:
        s = str(w)
        for r in (str(self.views[self.view]), str(self.side)):
            if s == r or s.startswith(r + "."):    # "." - a bare prefix would match every sibling
                return True
        return False

    def on_screen(self, w) -> bool:
        """Mapped AND in the shown view or the sidebar: every view is mapped now (a switch is
        a raise), so `winfo_ismapped` alone also says yes for a covered widget."""
        return bool(w.winfo_ismapped()) and self._in_shown(w)

    def _tab(self, back: bool):
        """Focus traversal that never lands in a covered view: step Tk's own ring until the
        stop is inside the shown view or the sidebar, then take it. Returns "break" only when
        it moved the focus itself, so a field's own Tab handling still works."""
        cur = self.win.focus_get()
        if cur is None:
            return None
        nxt = cur
        for _ in range(300):
            nxt = nxt.tk_focusPrev() if back else nxt.tk_focusNext()
            if nxt is None or nxt is cur:
                return None
            if self._in_shown(nxt):
                nxt.focus_set()
                return "break"
        return None

    # --- history view ------------------------------------------------------------------------
    def _build_history(self, f) -> None:
        """Rows: 0 head · 1 the filter field · 2 the scroll-only rule · 3 the list · 4 the
        transcript card · 5 the footer. The filter (Ctrl+F) narrows the list as you type; Esc
        clears it."""
        p, pad = self.pal, self.px(SP[3])
        f.grid_rowconfigure(3, weight=1)
        f.grid_columnconfigure(0, weight=1)

        head = tk.Frame(f, bg=p["layer"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[2])))
        head.pack_propagate(False)                 # the title row is 32 high with or without a button
        tk.Label(head, text="History", font=self.F["title"], fg=p["ink"], bg=p["layer"], bd=0, padx=0,
                 pady=0).pack(side="left")
        self.count = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["layer"], bd=0,
                              padx=0, pady=0)
        self.count.pack(side="left", padx=(self.px(SP[2]), 0))
        self.hdr_r = tk.Frame(head, bg=p["layer"])
        self.hdr_r.pack(side="right")
        self.b_clear = _Btn(self, self.hdr_r, "Clear all", self._clear_ask)
        self.b_clear.f.pack(side="right")
        self.confirm = tk.Frame(head, bg=p["layer"])
        self.c_label = tk.Label(self.confirm, text="", font=self.F["body"], fg=p["ink"], bg=p["layer"])
        self.c_label.pack(side="left", padx=(0, self.px(SP[2])))
        _Btn(self, self.confirm, "Clear", self._clear_do, kind="danger").f.pack(side="left")
        _Btn(self, self.confirm, "Keep", self._clear_cancel).f.pack(side="left", padx=(self.px(SP[1]), 0))

        # the filter: a field with the search glyph, 260 wide on E; every key release redraws
        # the list through its `keep` predicate
        self.filter = self._entry(f, "", 20, self.F["body"], lambda: None, w=W_FILTER,
                                  placeholder="Filter dictations", icon="search")
        self.filter.master.grid(row=1, column=0, sticky="w", padx=pad, pady=(0, self.px(SP[2])))
        self.filter.bind("<KeyRelease>", lambda e: self._filter_changed())
        self._filtered = ""                        # the text the list was last drawn for
        # no rule under the header at rest (a full-bleed hairline over a list of rounded
        # highlights is the most rigid line on the screen); the one under the filter shows
        # only while the list is scrolled, where a row or a day header clips right under the
        # field - the Settings and draft bodies' rule (`_body_scrolled`)
        self.hrule = self.hairline(f, color=p["layer"])
        self.hrule.grid(row=2, column=0, sticky="ew")
        box = self.box = tk.Frame(f, bg=p["layer"])
        box.grid(row=3, column=0, sticky="nsew")
        self.list = RowList(self, box, on_select=self._select, keys={
            "<Return>": self.copy_selected, "<Control-c>": self.copy_selected,
            "<Double-Button-1>": self.copy_selected, "<Delete>": self.delete_selected,
            "<Control-d>": self._to_promptify, "<Control-Return>": self._edit_toggle},
            keep=lambda it: self.filter_text() in it["text"].lower())
        self.list.on_scroll = lambda on: self.hrule.configure(bg=p["divider"](p["layer"]) if on else p["layer"])

        f.bind("<Configure>", self._fit_detail)
        # the selected transcript and its two actions are ONE grouped surface, from E - 12 to
        # the pane's right - 4 like the row highlights above it; its text lands on E like theirs
        self.card = self._card(f)
        self.card.grid(row=4, column=0, sticky="ew", padx=self.px(SP[0]), pady=self.px(SP[3]))
        card = self.card.body
        trow = tk.Frame(card, bg=p["card"])
        trow.pack(fill="x", padx=self.cpad, pady=(self.cpad, 0))
        # the line lead: `LH` above and below each paragraph (spacing1/3) AND between the
        # display lines of a wrapped one (spacing2) - Tk requests `height` lines at linespace +
        # spacing1 + spacing3 but lays a paragraph's wrapped lines at linespace + spacing2, so
        # without spacing2 a 6-line card held 7.8 lines and cut the last one mid-glyph
        self.detail = tk.Text(trow, height=1, width=1, wrap="word", relief="flat", bd=0,
                              highlightthickness=0, bg=p["card"], fg=p["ink"],
                              font=self.mf["body"], padx=0, pady=0, state="disabled",
                              spacing1=self.px(LH), spacing2=2 * self.px(LH), spacing3=self.px(LH),
                              cursor="xterm", selectbackground=p["sub"](p["card"]),
                              selectforeground=p["ink"], inactiveselectbackground=p["sub"](p["card"]))
        # no fill: stretched, the width request is ignored and a maximised window gives the
        # transcript ~200-character lines - `_fit_detail` holds it to the body measure
        self.detail.pack(side="left")
        # the transcript's own thumb, at the card's inner right, only while it has more lines
        # than the card shows (`_fit_height`)
        self.dsb = ttk.Scrollbar(trow, orient="vertical", style="C.Vertical.TScrollbar",
                                 command=self.detail.yview, takefocus=0)
        self.detail.configure(yscrollcommand=self.dsb.set)
        self.detail.bind("<Escape>", lambda e: (self._edit_abort(), self._select(self.sel))
                         if self.editing else None)
        self.detail.bind("<Control-Return>", lambda e: (self._edit_toggle(), "break")[1]
                         if self.editing else None)

        # the action row: the primary, then three text buttons (ink: actions, not meta), each
        # with its key drawn as a cap chord 8 after it, the actions 16 apart. The row IS one
        # canvas - the caps are items on it (`_chord_items`, as in the footers) and the four
        # buttons are placed over it: a chord of a Frame and cap Labels is an HWND each, and a
        # raise-based switch pays ~0.35 ms per HWND in the raised view (measured 2026-09-08:
        # four chord Frames + six cap Labels were +3.5 ms on the History switch)
        act = self.act = tk.Canvas(card, bg=p["card"], height=self.px(H_CTL), highlightthickness=0, bd=0)
        act.pack(fill="x", padx=self.cpad, pady=(self.px(SP[2]), self.cpad))
        self.b_copy = _Btn(self, act, "Copy", self.copy_selected, kind="primary")
        # the way into the Promptify view from here: the same row, drafting at once (Ctrl+D)
        self.b_prompt = _Btn(self, act, "Promptify", self._to_promptify, kind="text", ink=True)
        # fix what Whisper misheard before it goes anywhere ("para" once landed as "power")
        self.editing = False
        self.b_edit = _Btn(self, act, "Edit", self._edit_toggle, kind="text", ink=True)
        self.b_del = _Btn(self, act, "Delete", self.delete_selected, kind="text", ink=True)
        # Edit reads Save while editing: born as wide as the wider word, so nothing after it moves
        self.b_edit.w = self.b_edit.w0 = max(self.b_edit.w, self.mf["body"].measure("Save") + 2 * self.b_edit.pad)
        self.b_edit._paint()
        x, cy, self.act_xs, self.act_cw, self.act_at = 0, self.px(H_CTL) // 2, [], {}, {}
        self.act_btns = ((self.b_copy, "copy"), (self.b_prompt, "prompt"), (self.b_edit, "edit"), (self.b_del, "del"))
        for (b, name), caps in zip(self.act_btns, (("↵",), ("Ctrl", "D"), ("Ctrl", "↵"), ("Del",))):
            self.act_xs.append(x)                      # the button's x with its caps drawn
            x0 = x + b.w + self.px(SP[1])              # its chord's x (the cap images anchor "w" there)
            # the chord's items - images and texts - share `grp_<name>`, so `_layout_act` can
            # move a whole chord; the texts alone carry `<name>` (the coverage check reads them)
            x, ids = self._chord_items(act, x0, cy, caps, ("cap", "caps", name, "grp_" + name), ("caps", "grp_" + name))
            setattr(self, "caps_" + name, ids)       # the chord's text items: caps_copy, caps_prompt...
            self.act_cw[name], self.act_at[name] = x - x0, (x0, cy)   # the chord's width; where it is
            x += self.px(SP[3])
        self.act_need = x - self.px(SP[3])             # the row's width with the caps
        self.act_mode = None
        act.bind("<Configure>", lambda e: self._layout_act(e.width))
        self._layout_act(self.act_need, refit=False)
        # the footer: the keys this view answers to that no action already shows, and at its
        # right the status ("✓ Copied", "Deleted · Undo", "Editing") in the flash slot
        self.foot_h = self._footer(f, [(("↑", "↓"), "move"), (("Ctrl", "F"), "filter"), (("Esc",), "close")],
                                   edit_hints=[(("Ctrl", "↵"), "save"), (("Esc",), "cancel")])
        self.foot_h.grid(row=5, column=0, sticky="ew")
        self.status = self.foot_h.right
        self.s_text = self._flash_slot(self.status)
        # underlined so the one clickable word in the status line does not read as more meta
        self.s_undo = self.link(self.status, "Undo", self._undo, font=self.F["meta"] + ("underline",))

    def _layout_act(self, width=None, refit=True) -> None:
        """Place the action row's buttons for `width` (the row's own when not given): one row
        with their caps (16 apart, a chord 8 after each) when the whole row fits; else - a
        shortcut keeps its key drawn, the user's rule for small windows - two rows of two,
        Copy · Promptify over Edit · Delete at 32 + 8, while the view is 480 tall or more;
        else (600 x 400) one row of the four buttons 8 apart with the caps hidden, where a
        second row would leave the list a row and a half. Measured, not hard-coded, and
        change-only; a change re-runs the height rule, whose `chrome` counts the second row."""
        px = self.px
        width = self.act.winfo_width() if width is None else width
        if width <= 1:
            return
        mode = ("row" if self.act_need <= width else
                "two" if self.views["history"].winfo_height() >= px(480) else "bare")
        if mode == self.act_mode:
            return
        self.act_mode = mode
        y2 = px(H_CTL) + px(SP[1])
        self.act.configure(height=px(H_CTL) + (y2 if mode == "two" else 0))
        self.act.itemconfigure("caps", state="hidden" if mode == "bare" else "normal")
        x = 0
        for k, ((b, name), x_full) in enumerate(zip(self.act_btns, self.act_xs)):
            if mode == "row":
                bx, by = x_full, 0
            elif mode == "bare":
                bx, by = x, 0
                x += b.w + px(SP[1])
            else:
                x = 0 if k % 2 == 0 else x
                bx, by = x, (k // 2) * y2
                x += b.w + px(SP[1]) + self.act_cw[name] + px(SP[3])
            b.f.place(x=bx, y=by)
            at = (bx + b.w + px(SP[1]), by + px(H_CTL) // 2)      # the chord's place, anchor "w"
            if at != self.act_at[name]:
                self.act.move("grp_" + name, at[0] - self.act_at[name][0], at[1] - self.act_at[name][1])
                self.act_at[name] = at
        if refit:
            self._fit_height()

    @property
    def rows(self) -> list:
        """The History list's item rows, newest first (`RowList.rows` carries the day headers
        too; a `(None, y, h)` row is one of those)."""
        return [r for r in self.list.rows if r[0] is not None]

    def _filter_changed(self) -> None:
        """A key released in the filter: redraw the list when its text changed (a Tab or an
        arrow releases too), show the card only while something matches, and when the filter
        hid the selected row move the selection to the newest match - the card always shows
        a row that is in the list."""
        text = self.filter_text()
        if text == self._filtered:
            return
        self._filtered = text
        self.list.draw()
        items = self.list.items()
        self.card.grid() if items else self.card.grid_remove()
        if items and not any(it is self.sel for it in items):
            self._select(items[-1])
        else:
            self._fit_height()

    def _list(self):
        """The row list of the view on screen, if it has one."""
        return {"history": self.list, "promptify": self.plist}.get(self.view)

    def _visible(self) -> bool:
        """The wheel handlers hang off the root's "all" tag: refuse to act once this window is
        gone or withdrawn, whatever else in the process owns the pointer."""
        return bool(self.win and self.win.winfo_exists() and self.win.winfo_viewable())

    def _fit_detail(self, e) -> None:
        """The transcript's width: the body measure (an 80-character line), or what the card
        can hold when the window is narrower - a Text is measured in "0"s, so the nearest
        count of those."""
        px = self.px
        inner = e.width - 2 * px(SP[0]) - 2 - 2 * self.cpad          # the card's padding box
        w = min(self.measure, inner - px(SP[0]) - px(SP[1]))         # room for the thumb + 8
        self.detail.configure(width=max(20, w // max(1, self.mf["body"].measure("0"))))
        self._layout_act(refit=False)          # the view's height decides the row's shape too
        self._fit_height()

    def _fit_height(self) -> None:
        """The height rule. The list takes its natural height (n rows) when it fits, and the
        card gets the rest of the pane - up to the whole transcript, never under 3 lines. When
        the list is taller than that room, the card shows at most 6 lines (3 in a window under
        480) and the list scrolls instead. The thumb beside the text appears only when lines
        are hidden."""
        if getattr(self, "_fitting", False):
            return
        self._fitting = True             # configure -> relayout -> configure, one level deep
        try:
            px = self.px
            self.detail.update_idletasks()
            lines = (self.detail.count("1.0", "end", "displaylines") or (1,))[0]
            H = self.views["history"].winfo_height()
            lh = self.mf["body"].metrics("linespace") + 2 * px(LH)
            chrome = 2 + 2 * self.cpad + px(SP[2]) + px(H_CTL)      # hairlines, padding, gap, actions
            if self.act_mode == "two":
                chrome += px(H_CTL) + px(SP[1])                     # the action row's second line
            # head (16, 12), filter (0, 12), the rule, the list's natural height, the card's 16
            # either side and its chrome, the footer
            room = (H - px(PAD_TOP) - px(H_CTL) - px(SP[2]) - px(H_CTL) - px(SP[2]) - 1 - self.list.natural()
                    - px(SP[3]) - chrome - px(SP[3]) - px(H_FOOT))
            fit = room // lh
            cap = fit if fit >= 3 else (6 if H >= px(480) else 3)
            h = max(1, min(lines, cap))
            if int(self.detail.cget("height")) != h:
                self.detail.configure(height=h)      # the list follows: its <Configure> re-shows the selection
            if lines > h:
                self.dsb.pack(side="right", fill="y")
            else:
                self.dsb.pack_forget()
                self.detail.yview_moveto(0)
        finally:
            self._fitting = False

    def _edit_toggle(self) -> None:
        """Edit makes the transcript card writable; the same button saves. Esc abandons,
        leaving the row abandons. A changed text drops the entry's draft - it was made FROM
        the old words."""
        if self.sel is None:
            return
        if not self.editing:
            self.editing = True
            self.b_edit.text("Save")            # born wide enough for both words: nothing shifts
            self.s_undo.pack_forget()
            self._say(self.s_text, "Editing")        # the footer's edit strip names the keys
            self.foot_h.hints("edit")
            self.detail.configure(state="normal")
            self.detail.focus_set()
            return
        text = " ".join(self.detail.get("1.0", "end-1c").split())
        it, self.editing = self.sel, False
        self.b_edit.text("Edit")
        self._say(self.s_text, "")
        self.foot_h.hints("view")
        if text and text != it["text"]:
            it["text"] = text
            had = it.pop("draft", None) is not None
            self.history.save()
            self.refresh()
            self.s_undo.pack_forget()
            self.flash(self.s_text, "Saved · draft cleared" if had else "Saved", icon="check")
        self._select(it)

    def _edit_abort(self) -> None:
        self.editing = False
        self.b_edit.text("Edit")
        self._say(self.s_text, "")
        self.foot_h.hints("view")

    def _select(self, it) -> None:
        """The one selection, shared by both lists; the History card and the Promptify detail
        pane follow it."""
        if getattr(self, "editing", False):
            self._edit_abort()               # leaving the row abandons the edit
        self.sel = it
        if self.p_err and self.p_err[0] is not it:
            self.p_err = None            # a failure belongs to the dictation it happened on
        self.list.paint()
        self.plist.paint()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if it is not None:
            self.detail.insert("1.0", it["text"])
        self.detail.configure(state="disabled")
        self._fit_height()
        if self.view == "promptify":
            self._show(self._state_for(it))

    def _nav_key(self, step) -> None:
        """Up/Down reach the view's list before it has focus - but never while a field has it."""
        f, lst = self.win.focus_get(), self._list()
        if lst is None or f is lst or isinstance(f, (tk.Entry, tk.Text, ttk.Combobox)):
            return
        lst.focus_set()
        lst.move(step)

    def _to_promptify(self) -> None:
        """Ctrl+D in History: the same row in the Promptify view, drafting at once when it has
        no draft yet."""
        self.go("promptify")
        if self.sel is not None and not self.sel.get("draft"):
            self.promptify()

    def _idx(self):
        for i, it in enumerate(self.history.items):
            if it is self.sel:
                return i
        return None

    def copy_selected(self) -> None:
        if self.sel is None:
            return
        import pyperclip
        pyperclip.copy(self.sel["text"])
        self.s_undo.pack_forget()
        self.flash(self.s_text, "Copied", icon="check")

    def delete_selected(self) -> None:
        i = self._idx()
        if i is None:
            return
        item = self.history.items[i]
        self.history.delete(i)
        self.sel = None
        self.undo = (i, item)
        self.refresh()
        self._say(self.s_text, "Deleted ·")
        self.s_undo.pack(side="left", padx=(self.px(SP[0]), 0))
        self._cancel(id(self.s_text))
        self.jobs[id(self.s_text)] = self.win.after(8000, self._undo_expire)   # the offer lapses

    def _undo_expire(self) -> None:
        self.undo = None
        self.s_undo.pack_forget()
        self._say(self.s_text, "")

    def _undo(self) -> None:
        if not getattr(self, "undo", None):
            return
        i, item = self.undo
        self.history.insert(min(i, len(self.history.items)), item)
        self.sel = item
        self._undo_expire()
        self.refresh()

    def _clear_ask(self) -> None:
        n = len(self.history.items)
        if not n:
            return
        self.c_label.configure(text=f"Clear {n} dictation{'s' if n != 1 else ''}?")
        self.hdr_r.pack_forget()
        self.confirm.pack(side="right")

    def _clear_cancel(self) -> None:
        if self.win and self.confirm.winfo_ismapped():
            self.confirm.pack_forget()
            self.hdr_r.pack(side="right")

    def _clear_do(self) -> None:
        self.history.clear()
        self.sel = self.undo = None
        self._clear_cancel()
        self.refresh()

    # --- settings view -----------------------------------------------------------------------
    def _build_settings(self, f) -> None:
        """Title row, the scroll-only rule at 56, the groups in a body canvas from 64 (the
        Promptify pane's `_scroller`)."""
        p, pad = self.pal, self.px(SP[3])
        f.grid_rowconfigure(2, weight=1)
        f.grid_columnconfigure(0, weight=1)
        head = tk.Frame(f, bg=p["layer"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[1])))
        head.pack_propagate(False)
        tk.Label(head, text="Settings", font=self.F["title"], fg=p["ink"], bg=p["layer"], bd=0, padx=0,
                 pady=0).pack(side="left")
        # the footer: the keys the controls answer to; the Saved flash in its right slot
        self.foot_s = self._footer(f, [(("Tab",), "next"), (("Space",), "toggle"),
                                       (("←", "→"), "adjust"), (("Esc",), "close")])
        self.foot_s.grid(row=3, column=0, sticky="ew")
        self.saved = self._flash_slot(self.foot_s.right)
        rule = self.hairline(f, color=p["layer"])
        rule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(f, bg=p["layer"])
        box.grid(row=2, column=0, sticky="nsew")
        self.sc, self.ssb, inner = self._scroller(box, rule)
        # the cards start 12 px before E like the row highlights and end 4 px before the pane's
        # right edge, where the thumb's column is; their padding (`cpad`) puts the row labels
        # back on E and the controls on the header's right edge
        body = tk.Frame(inner, bg=p["layer"])
        body.pack(fill="x", padx=self.px(SP[0]))
        self._appearance(body)
        self._indicator(body)
        self._listening(body)
        self._history_group(body)

    def _group(self, parent, title, first=False) -> tk.Frame:
        """A settings group is a card; its title (meta, muted: a section label, not a second
        heading) sits above it on the ground, on the same left edge as the row labels."""
        tk.Label(parent, text=title, font=self.F["meta"], fg=self.pal["muted"], bg=self.pal["layer"],
                 bd=0, padx=0, pady=0).pack(
            anchor="w", padx=self.px(SP[2]), pady=(0 if first else self.px(SP[4]), self.px(SP[1])))
        card = self._card(parent)
        card.pack(fill="x")
        self.cards.append(card)
        body = tk.Frame(card.body, bg=card.body["bg"])
        body.pack(fill="x", pady=self.px(SP[0]))   # 12 (row) + 4 = the card's 16 px top padding
        return body

    WAVE_UI = (("Ribbon", "ribbon", "wave"), ("Equaliser", "equaliser", "bars"), ("Liquid", "liquid", "drop"))

    def _appearance(self, parent) -> None:
        g = self._group(parent, "Appearance", first=True)
        r = _Row(self, g, "Theme", "System follows the Windows setting")
        v = self.cfg.get("theme")
        self._segment(r.right, "theme", [("System", "system"), ("Light", "light"), ("Dark", "dark")],
                      v if v in ("light", "dark") else "system", self._set_theme)
        r = _Row(self, g, "Waveform", "How the bar moves while you talk")
        w = self.cfg.get("wave")
        self._segment(r.right, "wave", [(t, v) for t, v, _ in self.WAVE_UI],
                      w if w in [v for _, v, _ in self.WAVE_UI] else "ribbon", self._set_wave,
                      icons={v: ic for _, v, ic in self.WAVE_UI})

    def _set_wave(self, v) -> None:
        self.cfg["wave"] = v
        self.save()                           # run_app's on_save hands the config to the overlay: next frame

    def _set_theme(self, v) -> None:
        self.cfg["theme"] = v
        self.save()
        self._rebuild_when_idle()

    def _rebuild_when_idle(self) -> None:
        """The palette is baked into every widget, so a theme change rebuilds the window in
        place - after a running draft has landed (its ticker lives in the old window)."""
        if self.drafting is not None:
            self.jobs["theme"] = self.win.after(300, self._rebuild_when_idle)
            return
        self._rebuild()

    def _cancel_all(self) -> None:
        """Every pending job, before the window they were scheduled on is destroyed (a
        rebuild; a test's teardown): Tk's `after` invokes its callback by NAME, the name
        goes with the window's commands, and the next window's bindings can be handed the
        freed name (`id()` reuse) - which the stale timer then calls with no event."""
        for job in list(self.jobs.values()):
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
        self.jobs.clear()

    def _rebuild(self) -> None:
        geo, view, sel = self.win.geometry(), self.view, self.sel
        self._cancel_all()
        self.win.unbind_all("<MouseWheel>")
        self.win.destroy()
        self.imgs.clear()
        for lst in (self.cards, self.primaries, self.foot_links, self.caps_all):
            lst.clear()
        self.ctl.clear()
        self.p_memo = None                     # the pane is gone with the old widgets
        self.nav_bar = self.nav_over = None    # canvas items of the old sidebar
        self._build()
        self.win.geometry(geo)
        self.show()
        self.go(view)
        if sel is not None:
            self._select(sel)
        if self.logins:
            self._login_tick()

    def _indicator(self, parent) -> None:
        g = self._group(parent, "Indicator")
        self.r_color = _Row(self, g, "Colour", "Recording and persistent mode")
        self._colors(self.r_color, "color", "#e63c3c")
        r = _Row(self, g, "Transcribing colour", "The pulse while text is being typed")
        self._colors(r, "color_busy", "#ffaa32")

        r = _Row(self, g, "Opacity", "Of the bar and its glow")
        self._slider(r.right, float(self.cfg.get("opacity", 0.9)))
        r = _Row(self, g, "Haze", "A wide soft glow around the bar")
        self._toggle(r.right, bool(self.cfg.get("haze", False)))

    def _listening(self, parent) -> None:
        p = self.pal
        g = self._group(parent, "Listening")
        r = _Row(self, g, "Trigger key", "A headset button, media key or F13; double-tap records, triple-tap opens this, a tap stays the key's own")
        # three widgets in one right-aligned group: packed straight into r.right they hugged the
        # LEFT edge of the control column and broke the one right edge every other row shares
        ground = r.right["bg"]
        grp = tk.Frame(r.right, bg=ground)
        grp.pack(side="right")
        # the key IS a keycap (the raised recipe at 24 px, rendered at the fixed W_CHIP), in a
        # frame that does not propagate: a Label sized by its own image would grow when the
        # text turns into "Press a key…" and shove the row. Ink while a key is set, muted for
        # "none"; the capture state writes `ring` (`capture_key`)
        chip = tk.Frame(grp, bg=ground, width=self.px(W_CHIP), height=self.px(H_CHIP))
        chip.pack_propagate(False)
        self.l_trig = self.keycap(chip, vk_name(self.cfg.get("trigger_vk")), h=H_CHIP,
                                  fg=p["ink"] if self.cfg.get("trigger_vk") else p["muted"],
                                  w=self.px(W_CHIP))
        self.l_trig.pack(fill="both", expand=True)
        self.b_rm = _Btn(self, grp, "Remove", self.clear_key)
        self.b_change = _Btn(self, grp, "Change…", self.capture_key)
        chip.pack(side="left")
        self.b_change.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.b_rm.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self._trig_buttons()

        self.r_mic = _Row(self, g, "Microphone", "Applies after restart")
        self.mics = self._list_mics()
        names = [MIC_DEFAULT] + [f"{i}: {n}" for i, n in self.mics]
        cur, sel = self.cfg.get("mic"), None
        for i, n in self.mics:
            if cur == i or (isinstance(cur, str) and cur and cur.lower() in n.lower()):
                sel = f"{i}: {n}"
                break
        if sel is None and cur is not None:   # configured but not in the list: keep it, don't reset
            sel = f"{cur}: (as configured)"
            names.insert(1, sel)
        self.v_mic = tk.StringVar(value=sel or names[0])
        self._combo(self.r_mic, self.v_mic, names, self._set_mic)

        self.r_model = _Row(self, g, "Model", "Bigger is slower and more accurate · applies after restart")
        self.v_model = tk.StringVar(value=self.cfg.get("model", MODELS[0]))
        self._combo(self.r_model, self.v_model, MODELS, self._set_model, font=self.mf["mono"])

        self.r_lang = _Row(self, g, "Language", "Blank = auto (en for *.en models) · applies after restart")
        self.e_lang = self._entry(self.r_lang.right, self.cfg.get("language") or "", 6,
                                  self.F["body"], self._set_lang, w=W_FIELD, placeholder="auto")
        self.e_lang.master.pack(side="right")

    def _history_group(self, parent) -> None:
        g = self._group(parent, "History")
        self.r_days = _Row(self, g, "Keep dictations for", "0 keeps nothing")
        tk.Label(self.r_days.right, text="days", font=self.F["body"], fg=self.pal["muted"],
                 bg=self.r_days.right["bg"]).pack(side="right", padx=(self.px(SP[1]), 0))
        self.e_days = self._entry(self.r_days.right, f"{self.cfg.get('retention_days', 0):g}", 5,
                                  self.F["mono"], self._set_days, w=W_DAYS)
        self.e_days.master.pack(side="right")

    # --- controls ----------------------------------------------------------------------------
    def _field(self, parent, w, h, r=None, icon=None):
        """The rounded field a text control lives in: a Label whose image IS the field, with the
        control placed inside it. Inset by the RADIUS horizontally and by the hairline
        vertically (`.slot`), which is the whole trick - a square widget placed that far in can
        never cover a corner, and its inset is exactly the padding the text wanted anyway.
        `icon` names a glyph drawn in muted 8 in from the left edge (the filter's search) and
        the slot then starts 8 after it; the glyph is composed INTO the field's image rather
        than carried by a Label of its own - one widget fewer to paint on a view switch.

        A Canvas + create_window would look identical and be wrong: Tk unmaps a canvas's window
        items while the canvas is scrolled out of sight, so a field below the fold would drop
        out of the tab ring until someone scrolled to it."""
        p, ground, r = self.pal, parent["bg"], r or self.px(R_CTL)
        fill, border = p["ctl"], p["stroke"](p["ctl"])
        box = tk.Label(parent, bd=0, highlightthickness=0, bg=ground, padx=0, pady=0)

        # the Fluent TextBox: the raised recipe's outline stays 1 px in every state, and the
        # UNDERLINE between the arcs is the cue - `stroke_field` 1 px at rest (a field, not a
        # button, at a squint), `ring` 2 px focused, `danger` 2 px in error
        def face(under):
            if not icon:
                return self.rr(w, h, r, fill, ground, border, 1, under=under)
            ic = self.px(ICON)
            return self.img(("field", w, h, r, fill, ground, border, icon, under), lambda: compose_png(
                w, h, [(0, 0, rr_png(w, h, r, fill, border, 1, ground, under=under)),
                       (self.px(SP[1]), (h - ic) // 2, icon_png(icon, p["muted"], ic))]))

        def paint(state="idle"):
            box.configure(image=face({"idle": (p["stroke_field"], 1), "focus": (p["ring"], self.px(2)),
                                      "error": (p["danger"], self.px(2))}[state]))
        box.paint, box.fill = paint, fill
        x = self.px(SP[1]) + self.px(ICON) + self.px(SP[1]) if icon else r
        box.slot = lambda child: child.place(x=x, y=1, relwidth=1.0, width=-(x + r),
                                             relheight=1.0, height=-2)
        paint()
        return box

    def _entry(self, parent, value, width, font, commit, w=None, placeholder=None, icon=None) -> tk.Entry:
        """A one-line field `width` characters wide - or `w` logical px. Empty and unfocused it
        shows `placeholder` in muted; `.value()` is "" then (`.get()` would hand it back)."""
        p = self.pal
        if w:
            w = self.px(w)
        else:
            probe = tk.Entry(parent, width=width, font=font)     # what this many characters measure
            w = probe.winfo_reqwidth() + 2 * self.px(SP[1])
            probe.destroy()
        box = self._field(parent, w, self.px(H_CTL), icon=icon)
        e = tk.Entry(box, width=width, font=font, bg=box.fill, fg=p["ink"], relief="flat", bd=0,
                     insertbackground=p["ink"], highlightthickness=0, justify="left",
                     selectbackground=p["sub"](p["ctl"]), selectforeground=p["ink"])
        e.insert(0, value)
        box.slot(e)
        e.err, e.ph = [False], False

        def show_ph():
            if placeholder and not e.ph and not e.get():
                e.insert(0, placeholder)
                e.configure(fg=p["muted"])
                e.ph = True

        def hide_ph():
            if e.ph:
                e.delete(0, "end")
                e.configure(fg=p["ink"])
                e.ph = False
        e.value = lambda: "" if e.ph else e.get()
        # empty the field (Esc on the History filter); the placeholder comes back unless focused
        e.clear = lambda: (hide_ph(), e.delete(0, "end"), e is not self.win.focus_get() and show_ph())
        e.paint = lambda error=None: (e.err.__setitem__(0, e.err[0] if error is None else error),
                                      box.paint("error" if e.err[0] else
                                                "focus" if e is self.win.focus_get() else "idle"))
        e.bind("<FocusIn>", lambda ev: (hide_ph(), box.paint("error" if e.err[0] else "focus")))
        e.bind("<FocusOut>", lambda ev: (box.paint("error" if e.err[0] else "idle"), commit(), show_ph()))
        e.bind("<Return>", lambda ev: commit())
        show_ph()
        return e

    def _combo(self, row, var, values, commit, restart=True, font=None) -> None:
        """ttk cannot round a combobox, so it goes inside the same rounded field as an entry
        with its own border painted out (`bordercolor`/`lightcolor`/`darkcolor` = the fill) -
        the shape underneath is the only edge you see."""
        box = self._field(row.right, self.px(W_COMBO), self.px(H_CTL))
        box.pack(side="right")
        c = ttk.Combobox(box, textvariable=var, values=values, state="readonly",
                         style="M.TCombobox", font=font or self.mf["body"])
        box.slot(c)
        c.bind("<FocusIn>", lambda e: box.paint("focus"))
        c.bind("<FocusOut>", lambda e: box.paint())
        c.bind("<<ComboboxSelected>>", lambda e: (commit(), restart and row.restart(), c.selection_clear()))

    def _segment(self, parent, key, options, value, on_pick, icons=None) -> None:
        """One rounded container - a `ctl_press` track, no hairline - with the picked option as
        the raised key (`ctl`, its hairline and elevation edge: the one cell that is "up") and
        the hovered one an inset pill. Each option is a Canvas carrying its own SLICE of that
        one rendered container (see `segment_png`) as `.i_bg`, its label as `.i_text` and, with
        `icons` ({value: glyph}), a 16 px icon before the label - so the group is still one
        control and one tab stop: the container takes focus (a 2 px `ring` on the track's edge,
        as a field's) and Left/Right move between the options. No separators: with one option
        always filled they would never be seen anyway.

        The icon is composed INTO the slice image (`.i_icon` is None): a partial-alpha image
        item costs the canvas a read-back blend on every paint, and this control repaints on
        every Promptify switch. Cell width 12 + [16 + 6] + text + 12."""
        p, ground = self.pal, parent["bg"]
        icons = icons or {}
        f = tk.Frame(parent, bg=ground, height=self.px(H_CTL), highlightthickness=0, takefocus=1)
        f.pack(side="right")
        f.pack_propagate(False)
        h, r, pad, ic, gap = self.px(H_CTL), self.px(R_CTL), self.px(SP[2]), self.px(ICON), self.px(6)
        ws = tuple(2 * pad + (ic + gap if v in icons else 0) + self.mf["body"].measure(t) for t, v in options)
        cells, focus, hover = [], [False], [None]
        for i, (text, val) in enumerate(options):
            c = tk.Canvas(f, width=ws[i], height=h, bg=ground, highlightthickness=0, bd=0, cursor="hand2")
            c.pack(side="left")
            c.i_bg = c.create_image(0, 0, anchor="nw")
            c.i_icon = None
            c.i_text = (c.create_text(pad + ic + gap, h // 2, anchor="w", text=text, font=self.F["body"])
                        if val in icons else
                        c.create_text(ws[i] // 2, h // 2, text=text, font=self.F["body"]))
            cells.append((c, val))
        f.configure(width=sum(ws))
        key_up = (p["ctl"], p["stroke"](p["ctl"]), p["stroke_edge"], p["stroke_top"])
        # the hovered cell in dark stays darker than the key's lit top, so the key still reads
        # as the one that is up
        hov_fill = mix(p["ctl_press"], "#ffffff", .03) if self.dark else p["ctl_hover"]

        def slice_(i, fills, border, bw, icon, fg):
            png = lambda: segment_png(ws, h, r, i, fills, ground, p["ctl_press"], border, bw, self.px(R_IN))
            if not icon:
                return self.img(("seg", ws, i, fills, ground, border, bw), png)
            return self.img(("seg", ws, i, fills, ground, border, bw, icon, fg), lambda: compose_png(
                ws[i], h, [(0, 0, png()), (pad, (h - ic) // 2, icon_png(icon, fg, ic))]))

        def paint():
            fills = tuple(key_up if v == value[0] else
                          hov_fill if i == hover[0] else None for i, (_, v) in enumerate(cells))
            ringed = focus[0] and self.kbd            # focus-visible: keyboard focus only
            border = p["ring"] if ringed else p["ctl_press"]      # idle: the track's own colour
            bw = self.px(2) if ringed else 1
            for i, (c, val) in enumerate(cells):
                fg = p["ink"] if val == value[0] else p["muted"]
                c.itemconfigure(c.i_text, fill=fg)
                c.itemconfigure(c.i_bg, image=slice_(i, fills, border, bw, icons.get(val), fg))

        value = [value]
        f.set = lambda v: (value.__setitem__(0, v), paint())   # set from outside, silently

        def pick(v):
            value[0] = v
            paint()
            f.focus_set()
            on_pick(v)

        def hov(i):
            hover[0] = i
            paint()

        for i, (c, val) in enumerate(cells):
            c.bind("<Enter>", lambda e, i=i: hov(i))
            c.bind("<Leave>", lambda e: hov(None))
            c.bind("<Button-1>", lambda e, v=val: (setattr(self, "kbd", False), pick(v)))
        step = lambda d: (setattr(self, "kbd", True),
                          pick(cells[(next(i for i, c in enumerate(cells) if c[1] == value[0])
                                      + d) % len(cells)][1]))
        f.bind("<Left>", lambda e: step(-1))
        f.bind("<Right>", lambda e: step(1))
        f.bind("<FocusIn>", lambda e: (focus.__setitem__(0, True), paint()))
        f.bind("<FocusOut>", lambda e: (focus.__setitem__(0, False), paint()))
        self.ctl[key] = cells
        paint()
        return f

    def _colors(self, row, key, default) -> None:
        """Nine presets plus a hex field - the field is the escape hatch, the dots are the taste."""
        p, cur = self.pal, [norm_hex(self.cfg.get(key), default)]
        ground = row.right["bg"]
        # a 14 px dot in a 22 box (dots 8 apart): the chosen one's 2 px `ink` ring at a 2 px gap
        # fills the box exactly, so ringing a dot moves nothing
        box, d, ring = self.px(22), self.px(14), self.px(2)
        # one tab stop for the whole strip (nine would bury the hex field): Left/Right move a
        # cursor, Space/Return picks - so arrowing past a dot never writes cfg. The cursor
        # starts on the chosen dot (else its halo sat on the white one beside the ringed pick)
        kb = [PRESETS.index(cur[0]) if cur[0] in PRESETS else 0, False]   # cursor index, strip has focus
        dots = tk.Frame(row.right, bg=ground, takefocus=1, highlightthickness=ring,
                        highlightbackground=ground, highlightcolor=p["ring"])
        dots.pack(side="left")
        self.focus_visible(dots, ground)

        def paint():
            for i, (l, hx) in enumerate(cells):
                on, at = hx == cur[0], kb[1] and i == kb[0]
                halo = p["ink"] if on else p["stroke_field"] if at else None
                l.configure(image=self.img((hx, "sel" if on else "cur" if at else "off"),
                                           lambda hx=hx, halo=halo:
                                           dot_png(box, d, hx, halo, ring,
                                                   edge=p["stroke_strong"])))

        cells = []
        for hx in PRESETS:
            l = tk.Label(dots, bg=ground, cursor="hand2", bd=0)
            l.pack(side="left")
            cells.append((l, hx))
            l.bind("<Enter>", lambda e, l=l, hx=hx: hx != cur[0] and l.configure(
                image=self.img((hx, "hov"), lambda hx=hx: dot_png(box, d, hx, p["stroke_field"], ring,
                                                                 edge=p["stroke_strong"]))))
            l.bind("<Leave>", lambda e: paint())
            l.bind("<Button-1>", lambda e, hx=hx: (setattr(self, "kbd", False),
                                                   dots.focus_set(), pick(hx)))

        def cursor(step):
            kb[0] = (kb[0] + step) % len(cells)
            paint()

        for k, fn in (("<Left>", lambda e: cursor(-1)), ("<Right>", lambda e: cursor(1)),
                      ("<space>", lambda e: pick(PRESETS[kb[0]])),
                      ("<Return>", lambda e: pick(PRESETS[kb[0]])),
                      ("<FocusIn>", lambda e: (kb.__setitem__(1, True), paint())),
                      ("<FocusOut>", lambda e: (kb.__setitem__(1, False), paint()))):
            dots.bind(k, fn)
        e = self._entry(row.right, cur[0], 7, self.F["mono"], lambda: pick(e.get(), typed=True),
                        w=W_FIELD)
        e.master.pack(side="right")
        sw = tk.Label(row.right, bg=ground, bd=0)
        sw.pack(side="right", padx=(self.px(SP[2]), self.px(SP[2])))

        def swatch():      # 16 x 16, r4, the value itself (the one non-token colour on screen)
            sw.configure(image=self.rr(self.px(16), self.px(16), self.px(SP[0]), cur[0], ground,
                                       p["stroke_strong"]))

        def pick(v, typed=False):
            hx = norm_hex(v)
            if hx is None:
                row.error("Use a hex colour like #e63c3c")
                e.paint(True)
                return
            row.error("")
            e.paint(False)
            same = hx == cur[0] and self.cfg.get(key) == hx
            cur[0] = self.cfg[key] = hx
            if hx in PRESETS:
                kb[0] = PRESETS.index(hx)     # the keyboard cursor follows the mouse and the field
            if not typed or e.get() != hx:
                e.delete(0, "end")
                e.insert(0, hx)
            paint()
            swatch()
            if not same:
                self.save()

        self.ctl[key] = {"dots": [c[0] for c in cells], "entry": e, "pick": pick}
        paint()
        swatch()

    def _slider(self, parent, value) -> None:
        """160 x 32: a 4 px `stroke_strong` track with round caps, the `ring` fill over it, an
        18 px `ctl` knob (its hairline, a 10 px `ring` core) that is the raised thing on the
        track. The value reads `90%`: the digits in mono ink, the `%` in the body font muted,
        two Labels with no space between (a mono space is a full cell wide)."""
        p, ring, ground = self.pal, self.px(2), parent["bg"]
        W, H = self.px(160) - 2 * ring, self.px(H_CTL) - 2 * ring   # the ring is inside the 160x32
        val = [round(value, 2)]
        tk.Label(parent, text="%", font=self.F["body"], fg=p["muted"], bg=ground, bd=0, padx=0,
                 pady=0).pack(side="right")
        lab = tk.Label(parent, text="", font=self.F["mono"], fg=p["ink"], bg=ground, bd=0, padx=0,
                       pady=0)
        lab.pack(side="right")
        c = tk.Canvas(parent, width=W, height=H, bg=ground, cursor="hand2", takefocus=1,
                      highlightthickness=ring, highlightbackground=ground, highlightcolor=p["ring"])
        c.pack(side="right", padx=(0, self.px(SP[2])))
        self.focus_visible(c, ground)
        r = self.px(18) / 2
        c.create_line(r, H / 2, W - r, H / 2, fill=p["stroke_strong"], width=self.px(4), capstyle="round")
        fill = c.create_line(r, H / 2, r, H / 2, fill=p["ring"], width=self.px(4), capstyle="round")
        face, core = p["ctl"], p["ring"]
        # the 18 px dot in a 20 box: 1 px of air keeps its hairline off the box's edge
        knob = c.create_image(r, H / 2, image=self.img(
            ("knob", face, core), lambda: dot_png(self.px(20), self.px(18), face, edge=p["stroke"](face),
                                                  core=(self.px(10), core))))

        def paint():
            x = r + (val[0] - 0.2) / 0.8 * (W - 2 * r)
            c.coords(fill, r, H / 2, x, H / 2)
            c.coords(knob, x, H / 2)
            lab.configure(text=f"{val[0] * 100:.0f}")

        def at(ev, commit):
            x = min(W - r, max(r, c.canvasx(ev.x)))   # canvasx: the focus ring offsets ev.x
            set_(round(0.2 + (x - r) / (W - 2 * r) * 0.8, 2), commit)

        def set_(v, commit):
            val[0] = min(1.0, max(0.2, round(v, 2)))
            paint()
            if commit:
                self.cfg["opacity"] = val[0]
                self.save()

        self.ctl["opacity"], self.ctl["opacity_label"] = c, lab
        c.bind("<Button-1>", lambda e: (setattr(self, "kbd", False), c.focus_set(), at(e, False)))
        c.bind("<B1-Motion>", lambda e: at(e, False))
        c.bind("<ButtonRelease-1>", lambda e: at(e, True))
        c.bind("<Left>", lambda e: set_(val[0] - 0.01, True))     # keyboard: one step, committed
        c.bind("<Right>", lambda e: set_(val[0] + 0.01, True))
        paint()

    def _toggle(self, parent, value, key="haze") -> None:
        """36 x 20. Off: the ground with a `stroke_field` hairline round it and a 12 px `muted`
        knob (ink under the pointer); on: a `ring` track (a touch of ink under the pointer)
        with a 14 px white knob. A flip slides the knob in three frames (t = 0, .5, 1, 33 ms
        apart, `jobs[("toggle", key)]`), each a cached render of the NEW state's look;
        reduced motion shows the last frame at once. Focus: the 2 px `ring` highlight."""
        p, w, h = self.pal, self.px(36), self.px(20)
        ground = parent["bg"]
        on, hov, pos = [bool(value)], [False], [1.0 if value else 0.0]   # state, pointer, knob t
        l = tk.Label(parent, bg=ground, bd=0, cursor="hand2", takefocus=1,
                     highlightthickness=self.px(2), highlightbackground=ground,
                     highlightcolor=p["ring"])
        l.pack(side="right")
        self.focus_visible(l, ground)

        def frame(is_on, hover, t):
            if is_on:
                track, knob, edge, d = (mix(p["ring"], p["ink"], .10) if hover else p["ring"]), "#ffffff", None, 14
            else:
                track, knob, edge, d = ground, (p["ink"] if hover else p["muted"]), p["stroke_field"], 12
            return self.img(("toggle", w, h, track, knob, edge, d, t),
                            lambda: pill_png(w, h, track, knob, is_on, outline=edge, knob_d=self.px(d), t=t))

        def paint(hover=None, t=None):
            if hover is not None:
                hov[0] = hover
            if t is not None:
                pos[0] = t
            l.configure(image=frame(on[0], hov[0], pos[0]))

        def slide(steps, i=0):
            self.jobs.pop(("toggle", key), None)
            if not l.winfo_exists():                 # the sheet's rows can be rebuilt mid-slide
                return
            paint(t=steps[i])
            if i + 1 < len(steps):
                self.jobs[("toggle", key)] = self.win.after(33, lambda: slide(steps, i + 1))

        def toggle(*_):
            on[0] = not on[0]
            self.cfg[key] = on[0]
            self._cancel(("toggle", key))
            steps = (0.0, 0.5, 1.0) if on[0] else (1.0, 0.5, 0.0)
            slide(steps if self.motion else steps[-1:])
            self.save()

        self.ctl[key] = l
        l.bind("<Enter>", lambda e: paint(True))
        l.bind("<Leave>", lambda e: paint(False))
        l.bind("<Button-1>", toggle)
        l.bind("<space>", toggle)
        l.bind("<Return>", toggle)
        paint()

    # --- settings actions --------------------------------------------------------------------
    def _trig_buttons(self) -> None:
        set_ = self.cfg.get("trigger_vk") is not None
        (self.b_rm.f.pack(side="left", padx=(self.px(SP[1]), 0)) if set_ else self.b_rm.f.pack_forget())

    def capture_key(self) -> None:
        app = self.get_app()
        if app is None:
            self.l_trig.configure(text="loading…", fg=self.pal["muted"])
            return
        self.l_trig.configure(text="Press a key…", fg=self.pal["ring"])
        app.capture = lambda vk: self.root.after(0, lambda: self._captured(vk))

    def _captured(self, vk: int) -> None:
        self.cfg["trigger_vk"] = int(vk)
        self.l_trig.configure(text=vk_name(vk), fg=self.pal["ink"])
        self._trig_buttons()
        self.save()

    def clear_key(self) -> None:
        app = self.get_app()
        if app is not None:
            app.capture = None
        self.cfg["trigger_vk"] = None
        self.l_trig.configure(text="none", fg=self.pal["muted"])
        self._trig_buttons()
        self.save()

    def _set_mic(self) -> None:
        mic = self.v_mic.get()
        if mic == MIC_DEFAULT:
            self.cfg["mic"] = None
        elif not mic.endswith("(as configured)"):
            # the name, not the index: indices shift when Windows re-enumerates audio devices
            self.cfg["mic"] = dict(self.mics)[int(mic.split(":")[0])]
        self.save()

    def _set_model(self) -> None:
        self.cfg["model"] = self.v_model.get().strip() or self.cfg.get("model")
        self.save()

    def _set_lang(self) -> None:
        v = self.e_lang.value().strip()
        if v and not (v.isalpha() and len(v) <= 5):
            self.r_lang.error("Use a language code like en, or leave it blank")
            self.e_lang.paint(True)
            return
        self.r_lang.error("")
        self.e_lang.paint(False)
        if (self.cfg.get("language") or "") != v:
            self.cfg["language"] = v or None
            self.r_lang.restart()
            self.save()

    def _set_days(self) -> None:
        try:
            days = float(self.e_days.get().strip())
            assert days >= 0
        except (ValueError, AssertionError):
            self.r_days.error("Use a number of days, like 7")
            self.e_days.paint(True)
            return
        self.r_days.error("")
        self.e_days.paint(False)
        if self.cfg.get("retention_days") != days:
            self.cfg["retention_days"] = days
            self.history.days = days
            self.save()
            self.refresh()

    def save(self) -> None:
        """The shared writer. Autosave: every control writes its key into cfg and calls this -
        cfg is the live dict the app reads, so headset/retention apply at once."""
        self.on_save(self.cfg)
        if self.win is not None and self.win.winfo_exists():
            self.flash(self.saved, "Saved", 1500, icon="check")

    @staticmethod
    def _list_mics():
        try:
            import sounddevice as sd
            out = []
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and d["hostapi"] == 0:  # first host API (MME): one entry per mic
                    out.append((i, d["name"]))
            return out
        except Exception:
            return []


    # --- promptify view: the dictations left, the draft (or the engines sheet) right ----------
    STATES = ("pick", "noengine", "ack", "empty", "draft", "drafting", "error")
    WORDS = {"connected": "Connected", "none": "Not connected", "missing": "Not installed",
             "expired": "Login expired"}

    def _build_promptify(self, f) -> None:
        """Two panes. Left, the same dictations as History in two-line rows, a dot on the drafted
        ones, at a share of the window's width clamped by `W_LIST`. Right, a detail pane whose
        header carries the engine control, the target and THE primary, and whose body is one of
        seven states (`_show`) - or the engines sheet swapped in over it (`_build_sheet`)."""
        p, pad = self.pal, self.px(SP[3])
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(2, weight=1)
        lp = tk.Frame(f, bg=p["layer"], width=self.px(W_LIST[0]))
        lp.grid(row=0, column=0, sticky="nsew")
        lp.grid_propagate(False)
        self.hairline(f, vertical=True).grid(row=0, column=1, sticky="ns")
        self.lw = 0

        def fit(e):
            lw = max(self.px(W_LIST[0]), min(self.px(W_LIST[2]), int(W_LIST[1] * e.width)))
            if lw != self.lw:
                self.lw = lw
                lp.configure(width=lw)
                f.grid_columnconfigure(0, minsize=lw)
        f.bind("<Configure>", fit)
        lp.grid_rowconfigure(2, weight=1)
        lp.grid_columnconfigure(0, weight=1)
        head = tk.Frame(lp, bg=p["layer"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), pad))
        head.pack_propagate(False)
        # no Tk default border/padding on any Label in this view (2 + 1 px a side): the text
        # sits on E like the rows' and the buttons' glyphs
        tk.Label(head, text="Dictations", font=self.F["title"], fg=p["ink"], bg=p["layer"], bd=0, padx=0,
                 pady=0).pack(side="left")
        self.pcount = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["layer"], bd=0,
                               padx=0, pady=0)
        self.pcount.pack(side="left", padx=(self.px(SP[2]), 0))
        # the scroll-only rule under the head (the History filter's, `hrule`): a scrolled row
        # clips right under it, and the rule says so
        self.prule = self.hairline(lp, color=p["layer"])
        self.prule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(lp, bg=p["layer"])
        box.grid(row=2, column=0, sticky="nsew")
        self.plist = RowList(self, box, two_line=True, on_select=self._select, keys={
            "<Return>": self._plist_go, "<Double-Button-1>": self._plist_go,
            "<Control-c>": self.copy_selected})
        self.plist.on_scroll = lambda on: self.prule.configure(bg=p["divider"](p["layer"]) if on else p["layer"])

        d = self.dpane = tk.Frame(f, bg=p["layer"])
        d.grid(row=0, column=2, sticky="nsew")
        d.grid_rowconfigure(0, weight=1)
        d.grid_columnconfigure(0, weight=1)
        d.bind("<Configure>", lambda e: self._layout_head())
        self._build_draft(d)
        self._build_sheet(d)
        # the footer runs under both panes; its right slot stays empty - "Copied" is the
        # primary's own relabel. `↵` names what the list's Return does for the row (`_show`
        # re-words it `copy prompt` in the draft state); `Ctrl C` copies the dictation itself
        self.foot_p = self._footer(f, [(("↵",), "promptify"), (("Ctrl", "C"), "copy dictation"), (("Esc",), "back")])
        self.foot_p.grid(row=1, column=0, columnspan=3, sticky="ew")

    def _build_draft(self, parent) -> None:
        """The draft frame: header row 1 (engine dot + name, [target segment], the primary),
        row 2 (the segment, when row 1 cannot hold it), the scroll-only rule, the body canvas."""
        p, pad = self.pal, self.px(SP[3])
        df = self.draft_f = tk.Frame(parent, bg=p["layer"])
        df.grid(row=0, column=0, sticky="nsew")
        df.grid_rowconfigure(2, weight=1)
        df.grid_columnconfigure(0, weight=1)
        head = self.head = tk.Frame(df, bg=p["layer"])
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[1])))
        head.grid_columnconfigure(2, weight=1)
        eng = tk.Frame(head, bg=p["layer"])
        eng.grid(row=0, column=0, sticky="w")
        self.eng_dot = tk.Label(eng, bg=p["layer"], bd=0, padx=0, pady=0)
        self.eng_dot.pack(side="left")
        # the name's own 8 px pad is the gap after the dot
        self.b_eng = _Btn(self, eng, "Claude Code", self._open_sheet, kind="text", ink=True)
        self.b_eng.f.pack(side="left")
        self.eng_label = "Claude Code"       # the full name; `_layout_head` may show it shorter
        # a chevron after the name says the control opens something (the sheet). The dot, the
        # name and the chevron are ONE control under the pointer: the name lights (through its
        # blend) and the chevron goes ink together, and both stay lit while the pointer is
        # anywhere inside `eng`'s box - the 4 px gap included, so crossing from the name to the
        # chevron never dips through idle and replays the blend. A Leave's coordinates are the
        # pointer's, relative to the widget that lost it: that says whether it left the box.
        self.eng_chev = tk.Label(eng, image=self.icon("chev", p["muted"]), bg=p["layer"], bd=0,
                                 padx=0, pady=0, cursor="hand2")
        self.eng_chev.pack(side="left", padx=(self.px(SP[0]), 0))
        chev = lambda on: self.eng_chev.configure(image=self.icon("chev", p["ink" if on else "muted"]))

        def inside(e) -> bool:
            w = e.widget
            x, y = e.x + (0 if w is eng else w.winfo_x()), e.y + (0 if w is eng else w.winfo_y())
            return 0 <= x < eng.winfo_width() and 0 <= y < eng.winfo_height()

        def lit(on):
            chev(on)
            if not on:
                self.b_eng._set("idle")
            elif self.b_eng.state not in ("blend", "hover"):     # lit already: no replayed frame
                self.b_eng._set("hover")
        self.eng_chev.bind("<Button-1>", lambda e: self._open_sheet())
        for wdg in (eng, self.eng_dot, self.b_eng.f, self.eng_chev):   # the name's own hover
            wdg.bind("<Enter>", lambda e: lit(True))                     # bindings give way
            wdg.bind("<Leave>", lambda e: lit(inside(e)))
        self.seg_host = tk.Frame(head, bg=p["layer"])
        self.p_target = [self.cfg.get("prompt_target") or "code"]
        # the target as an icon and a short word (`>_ Code`, `globe Web`): the engine's name is
        # already in the control before it; `promptify.TARGETS` keeps the long labels for the
        # prompt's own text
        self.seg = self._segment(self.seg_host, "target", [(TARGET_UI[v][1], v) for _, v in promptify.TARGETS],
                                 self.p_target[0], self._set_target,
                                 icons={v: TARGET_UI[v][0] for _, v in promptify.TARGETS})
        self.b_main = _Btn(self, head, "Promptify", self.promptify, kind="primary")
        self.b_main.f.grid(row=0, column=3, sticky="e")
        self.head_rows, self.main_shown, self.head_padx = 0, True, (pad, pad)
        self.rule = self.hairline(df, color=p["layer"])
        self.rule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(df, bg=p["layer"])
        box.grid(row=2, column=0, sticky="nsew")
        self.pc, self.psb, self.p_inner = self._scroller(box, self.rule, cap=True)

    def _scroller(self, box, rule, cap=False) -> tuple:
        """The settings pattern: one inner Frame in one Canvas, so fields keep their place in the
        tab ring wherever they are scrolled to. `rule` is the hairline under the pane header: it
        shows only while the body is scrolled, so scrolled text clips right under it. `cap`
        holds the content to the body measure (an 80-character line) with air at the right.
        Returns (canvas, scrollbar, the frame to fill) - the frame has the body's 7/16 padding."""
        p, g = self.pal, self.px(SP[0])
        c = tk.Canvas(box, bg=p["layer"], highlightthickness=0, yscrollincrement=1, bd=0, width=1, height=1)
        c.pack(side="left", fill="both", expand=True)
        # the thumb's column: the outer 4 px of the pane's 16 px right padding, OVER the canvas
        # (a sibling placed on top of it), so the canvas never changes width when the thumb
        # comes and goes and the body's right edge stays the header's. The scrollbar is 2 px
        # wider than the column and 1 px left of it: the column clips clam's `bordercolor`
        # strips (`_ttk`) and the 4 px that show are all thumb.
        gut = tk.Frame(box, bg=p["layer"], width=g)
        gut.place(relx=1.0, y=0, anchor="ne", relheight=1.0)
        gut.lift()
        sb = ttk.Scrollbar(gut, orient="vertical", style="M.Vertical.TScrollbar", command=c.yview,
                           takefocus=0)
        sb.show = lambda on: (sb.place(x=-1, y=0, width=g + 2, relheight=1.0) if on else sb.place_forget())
        c.configure(yscrollcommand=lambda lo, hi: self._body_scrolled(sb, rule, lo, hi))
        gut.bind("<Enter>", lambda e: c.bind_all("<MouseWheel>", lambda ev: self._wheel(c, ev)))
        gut.bind("<Leave>", lambda e: c.unbind_all("<MouseWheel>"))
        win = tk.Frame(c, bg=p["layer"])
        wid = c.create_window((0, 0), window=win, anchor="nw")
        win.bind("<Configure>", lambda e: c.configure(scrollregion=c.bbox("all")))
        c.bind("<Configure>", lambda e: c.itemconfigure(
            wid, width=min(e.width, self.measure + 2 * self.px(SP[3])) if cap else e.width))
        c.bind("<Enter>", lambda e: c.bind_all("<MouseWheel>", lambda ev: self._wheel(c, ev)))
        c.bind("<Leave>", lambda e: c.unbind_all("<MouseWheel>"))
        inner = tk.Frame(win, bg=p["layer"])
        inner.pack(fill="both", expand=True, pady=(self.px(7), self.px(SP[3])))
        return c, sb, inner

    def _body_scrolled(self, sb, rule, lo, hi) -> None:
        sb.set(lo, hi)
        sb.show(not (float(lo) <= 0.0 and float(hi) >= 1.0))
        rule.configure(bg=self.pal["divider"](self.pal["layer"]) if float(lo) > 0.0 else self.pal["layer"])

    def _wheel(self, c, e) -> None:
        # yscrollincrement=1 makes "units" mean pixels, so one notch moves the same distance in
        # every pane instead of a tenth of whatever the viewport happens to be
        if self._visible():
            c.yview_scroll(int(-e.delta / 120) * 3 * self.px(H_CTL), "units")

    def _layout_head(self) -> None:
        """One header row when the engine control, the target segment and the primary fit side
        by side (engine + 12 + segment + 12 + primary within the pane's 16 px padding), else
        the segment drops to a second row. Measured on the real widgets - on every resize and
        every re-label of the primary - never a hard-coded width."""
        w = self.dpane.winfo_width()
        if w <= 1:
            return
        # the header ends where the body does: the body is capped at the measure (`_scroller`,
        # cap=True) with air at the right of a wide pane, and the primary shares that right
        # edge - the Assumed bar, the prompt and the answer lines end at the primary's, never
        # short of it under the thumb. From here `w` is the capped width
        pad = self.px(SP[3])
        cap = min(w, self.measure + 2 * pad)
        padx = (pad, w - cap + pad)
        if padx != self.head_padx:
            self.head_padx = padx
            self.head.grid_configure(padx=padx)
        w = cap
        if self.pstate == "noengine":
            return
        # the parts, not their frames: a Label's request is known at once, a frame's aggregate
        # only after Tk's next idle pass - and no <Configure> follows a re-labelled engine name
        # to make good a decision taken on the stale one
        eng = self.eng_dot.winfo_reqwidth() + self.px(SP[0]) + self.eng_chev.winfo_reqwidth()
        # when even the engine control and the primary cannot share the row (a 600 px window),
        # the name gives way: ellipsized to what is left beside the primary
        room = w - 2 * self.px(SP[3]) - eng - self.px(SP[2]) - self.b_main.w - 2 * self.b_eng.pad
        label = ellipsize(self.mf["body"], self.eng_label, room)
        if self.b_eng.f.cget("text") != label:
            self.b_eng.text(label, hold=False)
        need = (eng + self.b_eng.w + 2 * self.px(SP[2])
                + self.seg.winfo_reqwidth() + self.b_main.w)        # counted even while hidden
        rows = 1 if need <= w - 2 * self.px(SP[3]) else 2
        if rows != self.head_rows:
            self.head_rows = rows
            if rows == 1:
                self.seg_host.grid(row=0, column=1, columnspan=1, sticky="w",
                                   padx=(self.px(SP[2]), 0), pady=0)
            else:
                self.seg_host.grid(row=1, column=0, columnspan=4, sticky="w", padx=0,
                                   pady=(self.px(SP[1]), 0))

    def _wrap(self, label, pad, maxw=None) -> None:
        """A label wraps to its parent's width less `pad` (at most `maxw`), whatever the window
        does - less its own border and padding too, or a full line loses its last letters."""
        own = 2 * (int(label.cget("bd")) + int(label.cget("padx")))
        label.master.bind("<Configure>", lambda e: label.winfo_exists() and label.configure(
            wraplength=max(min(e.width - pad, maxw or e.width) - own, self.px(48))), add="+")

    def _line(self, parent, text, level, pady=(0, 0), fg=None) -> tk.Label:
        """One left-aligned line at E, wrapping at the body measure: T2 ("body", ink unless
        `fg`) or T3 ("meta", muted)."""
        p = self.pal
        l = tk.Label(parent, text=text, font=self.F[level], bg=parent["bg"], anchor="w",
                     justify="left", fg=fg or (p["muted"] if level == "meta" else p["ink"]),
                     bd=0, padx=0, pady=0)
        l.pack(fill="x", padx=self.px(SP[3]), pady=pady)
        self._wrap(l, 2 * self.px(SP[3]), self.measure)
        return l

    # --- the detail pane's states --------------------------------------------------------------
    def _engine_info(self) -> tuple:
        """(key, spec, connection state) of the engine cfg names - from the credential files,
        no process and no network. Cached per engine for the session (the status walk costs
        ~7 ms - a PATH scan - and ran on every view switch); the sheet's actions clear it and a
        60 s TTL catches a CLI installed while the app runs."""
        spec = promptify.engine_spec(self.cfg)
        key, now = spec["key"], time.monotonic()
        hit = self._eng_cache.get(key)
        if hit is None or now - hit[1] > 60:
            hit = (connect.status(key)[0], now)
            self._eng_cache[key] = hit
        return key, spec, hit[0]

    def _state_for(self, it) -> str:
        if it is None:
            return "pick"
        if self.drafting is it:
            return "drafting"
        if self.p_err and self.p_err[0] is it:
            return "error"
        if it.get("draft"):
            return "draft"
        if self._engine_info()[2] != "connected":
            return "noengine"
        return "empty"

    def _show(self, state) -> None:
        """One state of the detail pane for the selected dictation. The header keeps its one
        primary `_Btn` and re-labels it in place (nothing re-flows); the engine control and the
        segment follow; the body is rebuilt. Edits to the prompt on screen are kept first."""
        assert state in self.STATES, state
        it = self.sel
        self._save_edits()
        self._clear_body()
        # drafting keeps the primary that was showing (Promptify, Copy prompt or Try again),
        # faded in place - unless the pane showed another dictation, or "Copied" mid-flash
        cur = self.b_main.f.cget("text") if self.main_shown and self.p_item is it else None
        cur = ("Copy prompt" if cur == "Copied" else cur) or ("Copy prompt" if (it or {}).get("draft") else "Promptify")
        self.pstate, self.p_item = state, it
        self.p_memo = self._pane_key(state)
        self._head_engine()
        main = {"pick": None, "noengine": ("Connect an engine", self._open_sheet),
                "ack": ("Promptify", self._ack_go), "empty": ("Promptify", self.promptify),
                "draft": ("Copy prompt", self._copy_prompt), "error": ("Try again", self._retry),
                "drafting": (cur, lambda: None)}[state]
        self.main_shown = main is not None
        if main:
            self.b_main.text(main[0], hold=False)
            self.b_main.cmd = main[1]
            self.b_main.enable(state != "drafting")
            self.b_main.f.grid()
        else:
            self.b_main.f.grid_remove()          # the only state without a primary
        self.foot_p.fade(0, state == "drafting")   # the footer's `↵` names the primary: fades with it
        if state != "drafting":                    # ... and says what the row's Return does: a
            self.foot_p.word(0, "copy prompt" if state == "draft" else "promptify")   # draft is copied
        if state == "noengine":
            self.seg_host.grid_remove()          # nothing to target yet
            self.head_rows = 0
        else:
            self._layout_head()
        getattr(self, "_body_" + state)(it)
        self.pc.yview_moveto(0)

    def _head_engine(self) -> None:
        """The engine control: a 6 px state dot and the engine's name (a text button that opens
        the sheet). Danger is the dot's alone - text is never red."""
        p = self.pal
        key, spec, est = self._engine_info()
        named = est == "connected" or self.pstate == "error"
        col = p["danger"] if self.pstate == "error" else p["ring"] if est == "connected" else None
        self.eng_dot.configure(image=self.dot(8, 6, col) if col else
                               self.dot(8, 6, p["layer"], edge=p["stroke_field"]))
        self.eng_label = spec["label"] if named else "No engine"
        self.b_eng.text(self.eng_label, hold=False)
        self.b_eng.mute(not named)
        self.eng_chev.configure(image=self.icon("chev", p["muted"]))   # rest: muted, named or not

    def _clear_body(self) -> None:
        for key in ("skel", "track"):            # the drafting body's pending skeleton / its loop
            self._cancel(key)
        self.skel_at = None
        for w in self.p_inner.winfo_children():
            w.destroy()
        self.p_inner.unbind("<Configure>")       # the wrap bindings of the labels just destroyed
        self.p_fields = {"prompts": [], "questions": [], "answers": [], "chips": [], "cur": 0,
                         "assume": [], "text": None}

    def _body_pick(self, it) -> None:
        self._line(self.p_inner, "Pick a dictation on the left." if self.history.items else
                   "Dictate something first.", "meta")

    def _body_noengine(self, it) -> None:
        self._line(self.p_inner, "No engine connected.", "body")
        self._line(self.p_inner, "Connect Claude Code, Codex, Gemini CLI or OpenRouter — each signs in "
                   "on its own; murmur keeps no keys.", "meta", pady=(self.px(SP[0]), 0))

    def _body_ack(self, it) -> None:
        """First use: say where the words go - this is the one thing murmur does that sends
        them off the machine."""
        label = promptify.engine_spec(self.cfg)["label"]
        self._line(self.p_inner, f"Promptify sends this dictation to {label}.", "body")
        self._line(self.p_inner, "Only the dictation you pick, only when you press Promptify. Engines "
                   "sign in on their own; murmur keeps no keys.", "meta", pady=(self.px(SP[0]), 0))
        if (self.cfg.get("vault_path") or "").strip():
            self._line(self.p_inner, "With your vault turned on, short excerpts from matching notes "
                       "travel with that one dictation too.", "meta", pady=(self.px(SP[0]), 0))
        self.b_ack_no = _Btn(self, self.p_inner, "Not now",
                             lambda: self._show(self._state_for(self.sel)), kind="text")
        self.b_ack_no.f.pack(anchor="w", padx=(self.px(SP[3]) - self.px(SP[1]), 0),
                             pady=(self.px(SP[2]), 0))

    def _body_empty(self, it) -> None:
        label = promptify.engine_spec(self.cfg)["label"]
        self._line(self.p_inner, f"No draft yet · Promptify writes a prompt for {label} from this "
                   "dictation.", "meta")
        self._line(self.p_inner, it["text"], "body", pady=(self.px(SP[2]), 0), fg=self.pal["muted"])

    def _body_error(self, it) -> None:
        msg = self.p_err[1] if self.p_err else "the engine did not answer."
        self._line(self.p_inner, f"Couldn’t draft — {msg}", "body")
        # the way out is an action, not advice: the sheet, one click away (Try again is the
        # primary in the header)
        self.b_pick = _Btn(self, self.p_inner, "Pick another engine", self._open_sheet, kind="text")
        self.b_pick.f.pack(anchor="w", padx=(self.px(SP[3]) - self.px(SP[1]), 0), pady=(self.px(SP[0]), 0))

    def _body_drafting(self, it) -> None:
        """A 2 px track with a 64 px `ring` bar running along it and three skeleton bars where
        the prompt will be, then the line - the engine and the seconds (the ticker in
        `_run_draft` keeps them going) - and Cancel, all on one left edge. The track and the
        skeleton appear only after 300 ms (`jobs["skel"]`): a draft that lands sooner never
        flashes them, and once shown they stay >= 300 ms (`_after_skel`). Header and list do
        not move."""
        p, px, pad, label = self.pal, self.px, self.px(SP[3]), promptify.engine_spec(self.cfg)["label"]
        inner = self.p_inner
        self.track = tk.Canvas(inner, height=px(2), bg=p["layer"], highlightthickness=0, bd=0, width=1)
        self.track.bar = self.track.create_rectangle(0, 0, px(64), px(2), fill=p["ring"], outline="")
        # the bars are as wide as the prompt's measure (or the pane), re-rendered only when
        # that width changes - the `hl` pattern
        self.skel = [tk.Label(inner, bg=p["layer"], bd=0, padx=0, pady=0) for _ in range(3)]

        def fit(e=None):
            w = min(self.measure, inner.winfo_width() - 2 * pad)
            if w > 1 and w != getattr(self, "skel_w", None):
                self.skel_w = w
                for l, k in zip(self.skel, (1.0, .85, .60)):
                    l.configure(image=self.rr(round(w * k), px(SP[2]), px(SP[0]), p["sub"](p["layer"]), p["layer"]))
        self.skel_w = None
        inner.bind("<Configure>", fit, add="+")
        fit()
        row = tk.Frame(inner, bg=p["layer"])
        row.pack(fill="x", padx=pad)
        self.l_draft = tk.Label(row, text=f"Drafting with {label} · 0 s", font=self.F["body"],
                                fg=p["muted"], bg=p["layer"], bd=0, padx=0, pady=0)
        self.l_draft.pack(side="left")
        # Cancel: 4 after the line while the two share the row, else under it on E (a 600 px
        # window: the line alone is wider than the pane, and Cancel clipped to "Ca" at its
        # edge) - a child of `inner` packed INTO the row, so it moves between the two without
        # a rebuild; re-decided on every resize and every re-label of the line (the ticker's
        # "· still working"). Change-only.
        self.b_cancel = _Btn(self, inner, "Cancel", self._cancel_draft, kind="text")
        self.cancel_under = None

        def fit_cancel(e=None):
            if not row.winfo_exists():
                return
            w = row.winfo_width()
            under = w > 1 and self.l_draft.winfo_reqwidth() + px(SP[0]) + self.b_cancel.w > w
            if under == self.cancel_under:
                return
            self.cancel_under = under
            self.b_cancel.f.pack_forget()
            if under:
                self.b_cancel.f.pack(after=row, anchor="w", padx=(pad - px(SP[1]), 0), pady=(px(SP[0]), 0))
            else:
                self.b_cancel.f.pack(in_=row, side="left", padx=(px(SP[2]) - px(SP[1]), 0))
        fit_cancel()
        row.bind("<Configure>", fit_cancel)
        self.l_draft.bind("<Configure>", fit_cancel)
        self.skel_at = None
        self.jobs["skel"] = self.win.after(300, lambda: self._skel_show(row))

    def _skel_show(self, row) -> None:
        """300 ms in: the track and the skeleton bars above the line, and the bar's run."""
        self.jobs.pop("skel", None)
        if not row.winfo_exists():
            return
        pad = self.px(SP[3])
        self.track.pack(fill="x", padx=pad, before=row)
        for l in self.skel:
            l.pack(anchor="w", padx=pad, pady=(self.px(SP[2]), 0), before=row)
        row.pack_configure(pady=(pad, 0))
        self.skel_at = time.monotonic()
        if self.motion:
            self._track_step(0)

    def _track_step(self, i) -> None:
        """One of 75 `coords` steps 16 ms apart: the 64 px bar crosses the track and wraps -
        a linear 1.2 s loop (`jobs["track"]`)."""
        c = self.track
        if not c.winfo_exists():
            return
        w, b = c.winfo_width(), self.px(64)
        x = -b + (w + b) * i / 75
        c.coords(c.bar, x, 0, x + b, self.px(2))
        self.jobs["track"] = self.win.after(16, lambda: self._track_step((i + 1) % 75))

    def _after_skel(self, fn) -> None:
        """Run `fn` (the body swap a landed draft wants) now - or, when the skeleton has shown
        for less than 300 ms, after the rest of it: bars that flash for a frame read as a
        glitch, a beat reads as work done."""
        left = .3 - (time.monotonic() - self.skel_at) if self.skel_at else 0
        if left > 0 and self.motion:
            self.jobs["skel_hold"] = self.win.after(int(left * 1000), lambda: (self.jobs.pop("skel_hold", None), fn()))
        else:
            fn()

    def _body_draft(self, it) -> None:
        """The draft: the dictation folded behind "Original ›" (the work was made FROM it), the
        prompt as the hero - an editable Text on the bare ground, no card, no label - the
        notes, one block per question (question · why · chips · an underlined answer), the
        "Drafted by" meta and Update prompt."""
        p, px, pad, inner = self.pal, self.px, self.px(SP[3]), self.p_inner
        d, F = it["draft"], self.p_fields
        F["prompts"] = list(d.get("prompts") or [""])
        F["questions"] = list(d.get("questions") or [])
        self.p_target[0] = d.get("target") or self.cfg.get("prompt_target") or "code"
        self.seg.set(self.p_target[0])
        orow = tk.Frame(inner, bg=p["layer"])
        orow.pack(fill="x", padx=(pad - px(SP[1]), pad))     # the glyph lands on E
        self.orig_open = False
        self.b_orig = _Btn(self, orow, "Original ›", self._toggle_orig, kind="text",
                           font=self.F["meta"], h=H_CHIP, r=6)
        self.b_orig.f.pack(side="left")
        if len(F["prompts"]) > 1:          # the dictation split into independent asks
            self._segment(orow, "which", [(f"{i + 1} of {len(F['prompts'])}", i)
                                          for i in range(len(F["prompts"]))], 0, self._switch_prompt)
        self.l_orig = tk.Label(inner, text=it["text"], font=self.F["body"], fg=p["muted"],
                               bg=p["layer"], anchor="w", justify="left", bd=0, padx=0, pady=0)
        self._wrap(self.l_orig, 2 * pad)
        F["text"] = t = self._textbox(inner, F["prompts"][0], (4, 200))
        # the document: a 1 px `divider` rule above and below the prompt, from E to the right
        # edge. The rules are the host's own ground showing 1 px past the Text (whose 8 px of
        # inner pady keep the lines off the letters); the ring bar spans them at E - 2, so at
        # rest each rule starts on E. Two hairline Frames would draw the same and cost the
        # Promptify switch two HWNDs.
        t.host.configure(bg=p["divider"](p["layer"]))
        t.configure(pady=px(SP[1]))
        t.pack_configure(pady=1)
        t.host.pack(fill="x", padx=(pad - px(2), pad), pady=(px(SP[1]), 0))   # the ring bar before E
        if d.get("notes"):
            self._line(inner, "Notes · " + d["notes"], "meta", pady=(px(SP[2]), 0))
        vn = d.get("vault_notes") or []
        if vn:
            # the notes the engine saw, as note chips 8 apart, clickable into Obsidian
            crow = tk.Frame(inner, bg=p["layer"])
            crow.pack(anchor="w", padx=pad, pady=(px(SP[0]), 0))
            tk.Label(crow, text="Context ·", font=self.F["meta"], fg=p["muted"], bg=p["layer"],
                     bd=0, padx=0, pady=0).pack(side="left")
            F["context"] = [_Chip(self, crow, n.get("title") or n.get("path", ""),
                                  lambda path=n.get("path", ""): self._open_note(path), icon="note")
                            for n in vn[:3]]
            for c in F["context"]:
                c.f.pack(side="left", padx=(px(SP[1]), 0))
        qs = F["questions"]
        self.hairline(inner).pack(fill="x", padx=pad, pady=(px(SP[4]), 0))
        self._line(inner, f"Questions · {len(qs)}" if qs else
                   "No questions · the dictation was specific enough.", "meta", pady=(pad, 0))
        for i, q in enumerate(qs):
            blk = tk.Frame(inner, bg=p["layer"])
            blk.pack(fill="x", padx=pad, pady=(px(SP[2]) if i == 0 else pad, 0))
            l = tk.Label(blk, text=q["q"], font=self.F["body"], fg=p["ink"], bg=p["layer"],
                         anchor="w", justify="left", bd=0, padx=0, pady=0)
            l.pack(fill="x")
            self._wrap(l, 0)
            if q.get("why"):
                w = tk.Label(blk, text=q["why"], font=self.F["meta"], fg=p["muted"], bg=p["layer"],
                             anchor="w", justify="left", bd=0, padx=0, pady=0)
                w.pack(fill="x")
                self._wrap(w, 0)
            # the strip is made before the answer so Tab reaches the chips first
            strip = tk.Frame(blk, bg=p["layer"], takefocus=1, highlightthickness=0,
                             height=px(H_CHIP)) if q.get("options") else None
            arow = tk.Frame(blk, bg=p["layer"])
            a = self._uline(arow, "", (1, 3), "Type an answer")
            if strip is not None:
                self._chips(strip, q["options"], a)
                strip.pack(fill="x", pady=(px(SP[1]), 0))
            F["answers"].append(a)
            F["chips"].append(strip)
            self._assume(blk, arow, a)     # packs the button, then the expanding field
        # the foot: the "Drafted by" meta line (its model id in mono), 8 under it Update prompt
        # with its chord `Ctrl` `↵` 8 after (so the footer need not list it). ONE canvas - the
        # texts and the caps are items, the button is placed on it (the History action row's
        # pattern): a Frame of four Labels is an HWND each, and a raise-based switch pays
        # ~0.3 ms per HWND in the raised view
        lh = self.mf["meta"].metrics("linespace")
        foot = self.p_foot = tk.Canvas(inner, bg=p["layer"], highlightthickness=0, bd=0, width=1,
                                       height=lh + px(SP[1]) + px(H_CTL))
        foot.pack(fill="x", padx=(pad - px(SP[1]), 0), pady=(px(SP[4]), 0))
        label = promptify.ENGINES.get(d.get("engine"), {}).get("label", d.get("engine", ""))
        model, wall = d.get("model") or "", f" · {d.get('wall', 0):.0f} s"
        x, self.meta_items = px(SP[1]), []           # the button's own 8 px pad puts both on E
        for text, font in ((f"Drafted by {label} · ", "meta"), (model, "mono9"), (wall, "meta")):
            self.meta_items.append(foot.create_text(x, lh // 2, anchor="w", text=text, font=self.F[font],
                                                    fill=p["muted"]))
            x += self.mf[font].measure(text)

        def fit_meta(e):
            """The line ends at E from the right (at 600 it ran 60 px past the pane): "Drafted
            by Claude Code · " gives way first ("Claude Code · ", then nothing), then the model
            id ellipsizes; the wall time always shows."""
            meta, mono = self.mf["meta"], self.mf["mono9"]
            room = e.width - px(SP[1]) - px(SP[3]) - meta.measure(wall)
            head = next((t for t in (f"Drafted by {label} · ", f"{label} · ", "")
                         if meta.measure(t) + mono.measure(model) <= room), "")
            mid = ellipsize(mono, model, room - meta.measure(head))
            x = px(SP[1])
            for i, (text, font) in enumerate(((head, meta), (mid, mono), (wall, meta))):
                foot.itemconfigure(self.meta_items[i], text=text)
                foot.coords(self.meta_items[i], x, lh // 2)
                x += font.measure(text)
        foot.bind("<Configure>", fit_meta)
        self.b_update = _Btn(self, foot, "Update prompt", self._update, kind="text")
        self.b_update.f.place(x=0, y=lh + px(SP[1]))
        self.caps_update = self._chord_items(foot, self.b_update.w + px(SP[1]), lh + px(SP[1]) + px(H_CTL) // 2,
                                             ("Ctrl", "↵"), ("cap", "caps", "update"), ("caps",))[1]

    def _toggle_orig(self) -> None:
        self.orig_open = not self.orig_open
        self.b_orig.text("Original ▾" if self.orig_open else "Original ›", hold=False)
        if self.orig_open:      # 8 below the line, 24 (16 + the prompt's own 8) above the prompt
            self.l_orig.pack(fill="x", padx=self.px(SP[3]), pady=(self.px(SP[1]), self.px(SP[3])),
                             after=self.b_orig.f.master)
        else:
            self.l_orig.pack_forget()

    # --- fields ------------------------------------------------------------------------------
    def _uline(self, parent, text, lines=(1, 3), placeholder="", font=None, commit=None) -> tk.Text:
        """An underlined field: an unboxed Text over a 2 px band of two stacked hairlines. Idle,
        the 16 % band over the ground; focused, both `ring` - the band's height never changes, so
        focusing shifts nothing. Empty and unfocused it shows `placeholder` in muted under the
        tag "ph", and `.value()` is "" then. Grows with its content between `lines`; a
        one-line field commits on Return. Tab moves on; Ctrl+Return is Update prompt. `.host`
        is the frame to pack; `.set(s)` writes it; `.changed` is told after every edit."""
        p, ground = self.pal, parent["bg"]
        host = tk.Frame(parent, bg=ground)
        t = tk.Text(host, height=lines[0], wrap="word", relief="flat", bd=0, highlightthickness=0,
                    bg=ground, fg=p["ink"], font=font or self.mf["body"], insertbackground=p["ink"],
                    selectbackground=p["sub"](ground), selectforeground=p["ink"], undo=True,
                    padx=0, pady=0)
        t.pack(fill="x", pady=(self.px(7), self.px(6)))
        rest = mix(ground, "#ffffff" if self.dark else "#000000", .16)    # a field, not a rule
        band = [tk.Frame(host, bg=rest, height=1), tk.Frame(host, bg=ground, height=1)]
        for b in band:
            b.pack(fill="x")
        t.tag_configure("ph", foreground=p["muted"])
        t.ph, t.host, t.changed = False, host, lambda: None

        def fit(e=None):
            if not t.winfo_exists():         # the idle fit of a field a state change destroyed
                return
            n = t.count("1.0", "end", "displaylines") or (1,)
            h = max(lines[0], min(lines[1], n[0]))
            if int(t.cget("height")) != h:
                t.configure(height=h)

        def show_ph():
            if placeholder and not t.ph and not t.get("1.0", "end-1c") and t.focus_get() is not t:
                t.insert("1.0", placeholder, "ph")
                t.ph = True

        def hide_ph():
            if t.ph:
                t.delete("1.0", "end")
                t.ph = False

        def set_(s):
            hide_ph()
            t.delete("1.0", "end")
            t.insert("1.0", s)
            show_ph()
            fit()
            t.changed()

        def focus(on):
            for b in band:
                b.configure(bg=p["ring"] if on else (rest if b is band[0] else ground))
            (hide_ph if on else show_ph)()
            if not on and commit:
                commit()
        t.fit, t.set = fit, set_
        t.value = lambda: "" if t.ph else t.get("1.0", "end-1c").strip()
        t.bind("<FocusIn>", lambda e: focus(True))
        t.bind("<FocusOut>", lambda e: focus(False))
        t.bind("<KeyRelease>", lambda e: (fit(), t.changed()))
        t.bind("<Configure>", fit)
        t.bind("<Tab>", lambda e: (t.tk_focusNext().focus_set(), "break")[1])
        t.bind("<Shift-Tab>", lambda e: (t.tk_focusPrev().focus_set(), "break")[1])
        t.bind("<Control-Return>", lambda e: (self._update(), "break")[1])
        if lines[1] == 1:
            t.bind("<Return>", lambda e: (commit and commit(), "break")[1])
        t.insert("1.0", text)
        show_ph()
        self.win.after_idle(fit)
        return t

    def _textbox(self, parent, text, lines) -> tk.Text:
        """The prompt: an editable Text that grows with its content between `lines` (min, max) -
        the canvas around it is the only scroller. Tab moves on instead of inserting a tab: a
        form field, not an editor. Keyboard focus shows as a 2 px `ring` bar on its left edge,
        in the padding before E (a band under it, like the answers', would sit below the fold
        of a 30-line prompt) - ground-coloured until then, so nothing moves. The caller packs
        `.host` 2 px left of E."""
        p, ground = self.pal, parent["bg"]
        host = tk.Frame(parent, bg=ground)
        t = tk.Text(host, height=lines[0], wrap="word", relief="flat", bd=0, highlightthickness=0,
                    bg=ground, fg=p["ink"], font=self.mf["body"], insertbackground=p["ink"],
                    selectbackground=p["sub"](ground), selectforeground=p["ink"], undo=True,
                    padx=0, pady=0)
        t.host, t.ring = host, tk.Frame(host, bg=ground, width=self.px(2))
        t.ring.pack(side="left", fill="y")
        t.pack(side="left", fill="both", expand=True)
        t.bind("<FocusIn>", lambda e: t.ring.configure(bg=p["ring"]))
        t.bind("<FocusOut>", lambda e: t.ring.configure(bg=ground))
        t.insert("1.0", text)

        def fit(e=None):
            if not t.winfo_exists():         # the idle fit of a prompt a state change destroyed
                return
            n = t.count("1.0", "end", "displaylines") or (1,)
            h = max(lines[0], min(lines[1], n[0]))
            if int(t.cget("height")) != h:
                t.configure(height=h)
        t.fit = fit
        t.bind("<KeyRelease>", fit)
        t.bind("<Configure>", fit)
        t.bind("<Tab>", lambda e: (t.tk_focusNext().focus_set(), "break")[1])
        t.bind("<Shift-Tab>", lambda e: (t.tk_focusPrev().focus_set(), "break")[1])
        t.bind("<Control-Return>", lambda e: (self._update(), "break")[1])
        self.win.after_idle(fit)
        return t

    def _assume(self, blk, arow, a) -> None:
        """The deliberate way to defer a question: Assume hands it to the engine, which picks
        the most consistent reading and writes an Assumed line into the prompt. On, a tint bar
        covers the whole answer row; clicking it (or Space) hands the question back."""
        p, px = self.pal, self.px
        on = [False]
        self.p_fields["assume"].append(on)
        # the button first: a packed-earlier expanding field would leave it no cavity at all
        btn = _Btn(self, arow, "Assume", lambda: set_on(True), kind="text")
        btn.f.pack(side="right", anchor="s", padx=(px(SP[1]), 0), pady=(0, px(2)))
        a.host.pack(side="left", fill="x", expand=True)
        BAR_TEXT = "Assumed · the engine decides and says so · click to answer instead"
        # a canvas, not a compound Label: the text sits on the left at the 12 px inset whatever
        # the width (a Label centred it over the image once it fit - the one centred text in
        # the app - and left it only when ellipsized)
        bar = tk.Canvas(blk, height=px(H_CTL), bg=p["layer"], highlightthickness=0, bd=0,
                        cursor="hand2", takefocus=1)
        bar.i_bg = bar.create_image(0, 0, anchor="nw")
        bar.i_text = bar.create_text(px(SP[2]), px(H_CTL) // 2, anchor="w", text=BAR_TEXT,
                                     font=self.F["meta"], fill=p["ink"])

        def paint(e=None):
            w = bar.winfo_width()
            if w > 1:
                bar.itemconfigure(bar.i_bg, image=self.rr(w, px(H_CTL), px(R_CTL), p["tint"], p["layer"]))
                bar.itemconfigure(bar.i_text, text=ellipsize(self.mf["meta"], BAR_TEXT, w - 2 * px(SP[2])))

        bar.bind("<Configure>", paint)

        def set_on(v):
            if on[0] == bool(v):
                return
            on[0] = bool(v)
            if on[0]:
                arow.pack_forget()
                bar.pack(fill="x", pady=(px(SP[1]), 0))
                bar.focus_set()
            else:
                bar.pack_forget()
                arow.pack(fill="x", pady=(px(SP[1]), 0))
                a.focus_set()

        for seq in ("<Button-1>", "<space>", "<Return>"):
            bar.bind(seq, lambda e: set_on(False))

        def changed(prev=a.changed):     # a chip picked while assumed hands the question back
            if on[0] and a.value():
                set_on(False)
            prev()

        a.changed = changed
        self.p_fields.setdefault("assume_ui", []).append((btn, bar, set_on))
        arow.pack(fill="x", pady=(px(SP[1]), 0))

    def _chips(self, strip, options, answer) -> tk.Frame:
        """A question's options as chips in `strip` (a focusable Frame), wrapping. ONE tab stop
        for the strip: Left/Right move a cursor that wears the ring, Space/Return pick - which
        writes the chip's text into the answer and focuses it. The chosen chip is the one whose
        text IS the answer: derived from the field every time it changes, never stored, so
        typing something else un-chooses it."""
        gap = self.px(SP[1])
        kb = [0, False]                                   # cursor index, strip has focus
        chips = [_Chip(self, strip, o, lambda o=o: pick(o)) for o in options]

        def flow(e=None):
            w = strip.winfo_width()
            if w <= 1:
                return
            x = y = 0
            h = chips[0].h
            for c in chips:
                if x and x + c.w > w:
                    x, y = 0, y + h + gap
                c.f.place(x=x, y=y)
                x += c.w + gap
            strip.configure(height=y + h)

        def paint():
            cur = answer.value()
            for i, c in enumerate(chips):
                c.chosen(c.label == cur)
                c._ring(kb[1] and i == kb[0])
            flow()

        def pick(o):
            answer.set(o)
            answer.focus_set()

        def cursor(step):
            kb[0] = (kb[0] + step) % len(chips)
            paint()
        strip.bind("<Configure>", flow)
        for k, fn in (("<Left>", lambda e: cursor(-1)), ("<Right>", lambda e: cursor(1)),
                      ("<space>", lambda e: pick(options[kb[0]])),
                      ("<Return>", lambda e: pick(options[kb[0]])),
                      ("<FocusIn>", lambda e: (kb.__setitem__(1, True), paint())),
                      ("<FocusOut>", lambda e: (kb.__setitem__(1, False), paint())),
                      ("<Control-Return>", lambda e: (self._update(), "break")[1])):
            strip.bind(k, fn)
        strip.chips, strip.kb, answer.changed = chips, kb, paint
        paint()
        return strip

    # --- drafting ----------------------------------------------------------------------------
    def promptify(self) -> None:
        """The primary of an undrafted dictation (and Ctrl+D, Return on the list): show the
        entry's draft if it has one, else make one - after the first-use acknowledgement."""
        it = self.sel
        if it is None or self.drafting is not None:
            return
        if it.get("draft"):
            self._show("draft")
            return
        ok, _ = promptify.available(self.cfg)
        if not ok:
            self._show("noengine")
            return
        if not self.cfg.get("prompt_ack") or (
                (self.cfg.get("vault_path") or "").strip() and not self.cfg.get("vault_ack")):
            self._show("ack")
        else:
            self._run_draft(it)

    def _ack_go(self) -> None:
        self.cfg["prompt_ack"] = True
        if (self.cfg.get("vault_path") or "").strip():
            self.cfg["vault_ack"] = True
        self.save()
        self._run_draft(self.sel)

    def _set_target(self, v) -> None:
        self.p_target[0] = v
        self.cfg["prompt_target"] = v
        self.save()

    def _run_draft(self, it, prompts=None, questions=None, answers=None) -> None:
        """Pass 1 (no prompts) or pass 2, on a worker thread. The worker only fills `res`; the
        ticker on the Tk thread shows the elapsed time and consumes the result when the thread
        ends - Tk calls from the worker (even after()) are not safe outside mainloop."""
        spec = promptify.engine_spec(self.cfg)
        self.drafting, self.draft_item = it, it
        self.last_call, self.p_err = (it, prompts, questions, answers), None
        self.draft_cancel = cancel = threading.Event()
        if self.view == "promptify" and self.sel is it:
            self._show("drafting")
        t0, res = time.monotonic(), {}

        def work():
            try:
                # pass 2 folds against the same vault facts pass 1 saw; pass 1 searches anew
                vc = (it.get("draft") or {}).get("vault_ctx") if prompts is not None else None
                res["ok"] = promptify.draft(self.cfg, it["text"], self.p_target[0], prompts, questions,
                                            answers, cancel=cancel, workdir=self.history.path.parent,
                                            vault_ctx=vc)
            except promptify.Cancelled:
                res["cancelled"] = True
            except promptify.PromptifyError as e:
                res["err"] = str(e)
            except Exception as e:                       # a bug must not take the app down
                res["err"] = f"Promptify failed: {e}"

        def tick():
            if not (self.win and self.win.winfo_exists()):
                return
            if thread.is_alive():
                s = int(time.monotonic() - t0)
                l = getattr(self, "l_draft", None)
                if l is not None and l.winfo_exists():
                    l.configure(text=f"Drafting with {spec['label']} · {s} s"
                                     + (" · still working" if s >= 45 else ""))
                self.jobs["tick"] = self.win.after(200, tick)
            elif "ok" in res:
                self._draft_done(it, res["ok"])
            elif "err" in res:
                self._draft_failed(it, res["err"])
            else:
                self._cancel_draft()
        thread = self.draft_thread = threading.Thread(target=work, daemon=True)
        thread.start()
        tick()

    def _stop_draft(self) -> None:
        """The worker's cancel flag and the ticker; what the pane shows next is the caller's."""
        if getattr(self, "draft_cancel", None) is not None:
            self.draft_cancel.set()
        job = self.jobs.pop("tick", None)
        if job:
            self.win.after_cancel(job)
        for key in ("skel", "track"):        # a skeleton still due, or the bar's run
            self._cancel(key)
        self.drafting = None

    def _cancel_draft(self) -> None:
        """Cancel, Esc: back to the state the dictation was in."""
        was = self.drafting
        self._stop_draft()
        if was is not None and self.view == "promptify" and self.sel is was:
            self._show(self._state_for(was))

    def _draft_failed(self, it, msg) -> None:
        if not (self.win and self.win.winfo_exists()):
            return
        self._stop_draft()
        self.eng_err[promptify.engine_spec(self.cfg)["key"]] = msg
        self.p_err = (it, msg)
        # the swap waits out a skeleton shown < 300 ms; by then the pane may show another row
        self._after_skel(lambda: self.view == "promptify" and self.sel is it and self._show("error"))

    def _draft_done(self, it, res) -> None:
        if not (self.win and self.win.winfo_exists()):
            return
        self._stop_draft()
        it["draft"] = res                   # the draft lives on the History entry, on disk
        self.history.save()
        self.eng_err.pop(res.get("engine"), None)
        self.list.draw()                    # the dot
        self.plist.draw()

        def show():                         # after a skeleton's 300 ms, if one is showing
            if self.view == "promptify" and self.sel is it:
                self._show("draft")
                self.b_main.f.focus_set()   # ... which now says Copy prompt
        self._after_skel(show)

    def _retry(self) -> None:
        """Try again: the same call that failed - and the engine's failure marker goes first."""
        if self.last_call and self.drafting is None:
            self.eng_err.pop(promptify.engine_spec(self.cfg)["key"], None)
            self._run_draft(*self.last_call)

    def _switch_prompt(self, i) -> None:
        F = self.p_fields
        self._save_edits()
        F["cur"] = i
        F["text"].delete("1.0", "end")
        F["text"].insert("1.0", F["prompts"][i])
        F["text"].fit()

    def _save_edits(self) -> None:
        """The prompt field is the truth: whatever the user typed rides into pass 2 and into
        the copy, and is kept on the entry."""
        F = self.p_fields
        t = F.get("text")
        if t is None or not t.winfo_exists():
            return
        F["prompts"][F["cur"]] = t.get("1.0", "end").strip()
        d = (self.p_item or {}).get("draft")
        if d is not None and d.get("prompts") != F["prompts"]:
            d["prompts"] = list(F["prompts"])
            self.history.save()

    def _copy_prompt(self) -> None:
        self._save_edits()
        F = self.p_fields
        if not F.get("prompts"):
            return
        import pyperclip
        pyperclip.copy(F["prompts"][F["cur"]])
        self.b_main.text("Copied")          # at its held width: nothing moves
        job = self.jobs.pop("copied", None)
        if job:
            self.win.after_cancel(job)
        self.jobs["copied"] = self.win.after(
            1500, lambda: self.pstate == "draft" and self.b_main.text("Copy prompt"))

    def _update(self) -> None:
        """Pass 2 with the answers as they stand; with none given, pass 1 again."""
        if self.drafting is not None or self.pstate != "draft":
            return
        ok, _ = promptify.available(self.cfg)
        if not ok:
            self._show("noengine")
            return
        self._save_edits()
        F = self.p_fields
        answers = [promptify.ASSUME if on[0] else a.value()
                   for a, on in zip(F["answers"], F["assume"]) if a.winfo_exists()]
        if any(answers):
            self._run_draft(self.p_item, list(F["prompts"]), list(F["questions"]), answers)
        else:
            self._run_draft(self.p_item)

    def _plist_go(self) -> None:
        """Return / double-click on a dictation: its prompt to the clipboard, or a draft."""
        if self.sel is None:
            return
        if self.sel.get("draft") and self.pstate == "draft":
            self._copy_prompt()
        else:
            self.promptify()

    def receive(self, text: str) -> bool:
        """A take that arrived while this window was in front: into the focused prompt or
        answer field of the Promptify view, at the caret. Anything else on screen is not ours
        to type into."""
        if not (self.view == "promptify" and self._visible()):
            return False
        f = self.win.focus_get()
        if not isinstance(f, tk.Text) or not str(f).startswith(str(self.dpane)):
            return False
        before = f.get("1.0", "insert")
        f.insert("insert", ("" if not before or before.endswith((" ", "\n")) else " ") + text)
        getattr(f, "fit", lambda: None)()
        getattr(f, "changed", lambda: None)()
        f.see("insert")
        return True

    def _escape(self) -> None:
        """Esc, one step back at a time: the sheet closes; a draft in progress is cancelled; a
        filter with text under the caret is cleared (as any search field); a field - or
        anything in the Promptify detail pane, chip strips and header buttons included - hands
        focus back to the view's list; the Clear confirm is dismissed; a filter with text left
        behind is cleared; and with nothing left to undo the window hides (the footers' "Esc
        close")."""
        if self.view == "promptify":
            if self.sheet_open:
                self._close_sheet()
                return
            if self.drafting is not None and self.drafting is self.sel:
                self._cancel_draft()
                return
        f, lst = self.win.focus_get(), self._list()
        if f is self.filter and self.filter_text():
            self.filter.clear()
            self._filter_changed()
            return
        if lst is not None and f is not None and (isinstance(f, (tk.Entry, tk.Text, ttk.Combobox))
                                                  or str(f).startswith(str(self.dpane) + ".")):
            lst.focus_set()
            return
        if self.confirm.winfo_ismapped():
            self._clear_cancel()
            return
        if self.filter_text():
            self.filter.clear()
            self._filter_changed()
            return
        self.hide()

    # --- the engines sheet -------------------------------------------------------------------
    def _build_sheet(self, parent) -> None:
        """Swapped in over the draft frame (grid / grid_remove; the list pane stays). Four rows,
        one per engine, rebuilt from the engine table on every change of state
        (`_engine_rows`). No key fields anywhere: each engine signs in on its own, in the
        browser, and murmur only ever reads whether it did (`connect.status`)."""
        p, pad = self.pal, self.px(SP[3])
        s = self.sheet = tk.Frame(parent, bg=p["layer"])
        s.grid(row=0, column=0, sticky="nsew")
        s.grid_remove()
        self.sheet_open, self.e_rows = False, {}
        s.grid_rowconfigure(2, weight=1)
        s.grid_columnconfigure(0, weight=1)
        head = tk.Frame(s, bg=p["layer"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=(pad - self.px(SP[1]), pad),
                  pady=(self.px(PAD_TOP), self.px(SP[1])))
        head.pack_propagate(False)
        self.b_back = _Btn(self, head, "‹ Draft", self._close_sheet, kind="text")
        self.b_back.f.pack(side="left")
        tk.Label(head, text="Engines", font=self.F["title"], fg=p["ink"], bg=p["layer"], bd=0, padx=0,
                 pady=0).pack(side="left", padx=(self.px(SP[0]), 0))   # the button's 8 + 4 = 12
        self.srule = self.hairline(s, color=p["layer"])
        self.srule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(s, bg=p["layer"])
        box.grid(row=2, column=0, sticky="nsew")
        self.ec, self.esb, self.e_inner = self._scroller(box, self.srule)

    def _open_sheet(self) -> None:
        self._eng_cache.clear()
        self.sheet_open = True
        self.draft_f.grid_remove()
        self.sheet.grid()
        self._engine_rows()
        self.ec.yview_moveto(0)
        self.b_back.f.focus_set()

    def _close_sheet(self) -> None:
        if not self.sheet_open:
            return
        self.sheet_open = False
        self.sheet.grid_remove()
        self.draft_f.grid()
        self._show(self._state_for(self.sel))      # the engine may have changed
        self.b_eng.f.focus_set()

    def _engine_row(self, key) -> tuple:
        """(status line, dot colour or None for hollow, action) for one engine's row."""
        p, spec = self.pal, promptify.ENGINES[key]
        if key in self.logins:
            return "Connecting · Finish signing in in the browser window", p["muted"], "cancel"
        state, err = connect.status(key)[0], self.eng_err.get(key)
        if state == "connected":
            # a failure while signed in (a usage limit, a timeout) is said, on the danger dot,
            # but the engine stays usable - Use/Active and the Model line: a fresh sign-in is
            # not the remedy. Connect is only for none/missing/expired.
            active = key == promptify.engine_spec(self.cfg)["key"]
            word = "Connected" if err is None else "Usage limit" if "limit" in err.lower() else "Error"
            line = f"{word} · {err} · " if err else f"{word} · "
            return line + spec["login"], p["danger"] if err else p["ring"], "active" if active else "use"
        if err:                                       # a sign-in that failed says why
            return f"Error · {err}", p["danger"], "connect"
        return f"{self.WORDS.get(state, 'Not connected')} · {spec['login']}", None, "connect"

    def _engine_rows(self) -> None:
        """The sheet's body, rebuilt from the engine table: intro, one block per engine (name;
        dot + state · meaning; the action at the right; the Model field under the active one),
        the disclosure. `e_rows[key]` holds each row's parts for the tests."""
        p, px, pad, inner = self.pal, self.px, self.px(SP[3]), self.e_inner
        self._cancel("breathe")                   # the old rows' connecting dot
        for w in inner.winfo_children():
            w.destroy()
        inner.unbind("<Configure>")               # the wrap bindings of the labels just destroyed
        self.e_rows, breathing = {}, []
        self._line(inner, "The engine writes the prompt. Each signs in on its own; murmur keeps "
                   "no keys.", "meta")
        rows = tk.Frame(inner, bg=p["layer"])
        rows.pack(fill="x", padx=pad, pady=(pad, 0))
        for i, key in enumerate(promptify.ORDER):
            spec = promptify.ENGINES[key]
            status, col, action = self._engine_row(key)
            if i:
                self.hairline(rows).pack(fill="x")
            blk = tk.Frame(rows, bg=p["layer"])
            blk.pack(fill="x", pady=px(SP[1]))
            top = tk.Frame(blk, bg=p["layer"])
            top.pack(fill="x")
            # the action column keeps its width and centres its control on the two text lines
            right = tk.Frame(top, bg=p["layer"], width=px(W_ACT))
            right.pack(side="right", fill="y")
            right.pack_propagate(False)
            left = tk.Frame(top, bg=p["layer"])
            left.pack(side="left", fill="x", expand=True, padx=(0, px(SP[2])))
            tk.Label(left, text=spec["label"], font=self.F["body"], fg=p["ink"], bg=p["layer"], bd=0,
                     padx=0, pady=0).pack(anchor="w")
            st = tk.Frame(left, bg=p["layer"])
            st.pack(fill="x", pady=(px(2), 0))
            # the state dot: `ring` connected, `danger` on an error, hollow when not connected;
            # a row that is signing in breathes (`_breathe`) - from its .7 frame, which is also
            # the still one under reduced motion
            dl = tk.Label(st, bg=p["layer"], bd=0, padx=0, pady=0,
                          image=self.dot(8, 6, mix(p["layer"], p["muted"], .7)) if key in self.logins else
                          self.dot(8, 6, col) if col else self.dot(8, 6, p["layer"], edge=p["stroke_field"]))
            dl.pack(side="left", anchor="n", pady=(px(SP[0]), 0))
            if key in self.logins:
                breathing.append(dl)
            sl = tk.Label(st, text=status, font=self.F["meta"], fg=p["muted"], bg=p["layer"],
                          anchor="w", justify="left", bd=0, padx=0, pady=0)
            sl.pack(side="left", fill="x", expand=True, padx=(px(SP[1]), 0))
            self._wrap(sl, px(SP[3]))
            col_ = tk.Frame(right, bg=p["layer"])
            col_.place(relx=1.0, rely=0.5, anchor="e")
            row = self.e_rows[key] = {"status": sl, "dot": dl, "action": action, "btn": None}
            if action == "active":
                tk.Label(col_, text="Active", font=self.F["meta"], fg=p["ink"], bg=p["layer"], bd=0,
                         padx=0, pady=0).pack(anchor="e")
            else:
                text, cmd, kind = {"cancel": ("Cancel", self._cancel_login, "text"),
                                   "use": ("Use", self._use, "secondary"),
                                   "connect": ("Connect", self._connect, "secondary")}[action]
                row["btn"] = _Btn(self, col_, text, lambda k=key, c=cmd: c(k), kind=kind)
                row["btn"].f.pack(anchor="e")
            if key == "openrouter" and action in ("active", "use"):   # its key is murmur's own file
                row["disconnect"] = _Btn(self, col_, "Disconnect", self._disconnect_openrouter, kind="text")
                row["disconnect"].f.pack(anchor="e", pady=(px(SP[0]), 0))
            w_act = max([px(W_ACT)] + [c.winfo_reqwidth() for c in col_.winfo_children()])
            right.configure(width=w_act)

            def fit(e, right=right, left=left, col_=col_, w_act=w_act):
                """A narrow pane (600 px: beside the column the status wrapped to ten-character
                lines, six of them) drops the action under the text, on the right edge; back
                beside it, centred on the lines, when the room returns. Change-only."""
                stack = e.width < px(W_ACT) + px(SP[2]) + px(200)
                if stack == getattr(right, "stacked", False):
                    return
                right.stacked = stack
                if stack:
                    col_.place_forget()
                    right.pack_propagate(True)
                    left.pack_configure(side="top", fill="x", expand=False, padx=0)
                    right.pack_configure(side="top", fill="x", after=left, pady=(px(SP[0]), 0))
                    col_.pack(anchor="e")
                else:
                    col_.pack_forget()
                    col_.place(relx=1.0, rely=0.5, anchor="e")
                    right.pack_propagate(False)
                    right.configure(width=w_act)
                    right.pack_configure(side="right", fill="y", before=left, pady=0)
                    left.pack_configure(side="left", fill="x", expand=True, padx=(0, px(SP[2])))
            top.bind("<Configure>", fit)
            login = self.logins.get(key)
            if login and login["url"] and key == "claude":
                row["code"] = self._paste_code(blk, login["login"])
            if action == "active":
                row["model"] = self._model_line(blk, key)
        if breathing and self.motion:
            self._breathe(breathing)
        self._vault_block(inner)
        self._split_block(inner)
        self._line(inner, "Connect opens the engine’s own sign-in in your browser. A dictation is "
                   "sent only to the engine you pick, only when you press Promptify. With a vault "
                   "turned on, short excerpts from matching notes travel with it.", "meta",
                   pady=(px(SP[2]), 0))

    def _breathe(self, dots, i=0) -> None:
        """The connecting rows' dots breathe: `muted` over the layer at alpha .4 -> .7 -> 1.0
        -> .7, one step per 225 ms (900 ms a cycle) - the one loop a sign-in's wait gets. The
        job (`jobs["breathe"]`) is cancelled when the rows are rebuilt and by `_rebuild`; a
        dot destroyed in between is skipped."""
        p = self.pal
        img = self.dot(8, 6, mix(p["layer"], p["muted"], (.4, .7, 1.0, .7)[i % 4]))
        for l in dots:
            if l.winfo_exists():
                l.configure(image=img)
        self.jobs["breathe"] = self.win.after(225, lambda: self._breathe(dots, i + 1))

    def _vault_block(self, inner) -> None:
        """The Obsidian bridge, under the engine rows: one row - name, state line, actions.
        Off by default; turning it on re-shows the disclosure once (vault_ack)."""
        p, px, pad = self.pal, self.px, self.px(SP[3])
        self.hairline(inner).pack(fill="x", padx=pad, pady=(px(SP[4]), 0))
        blk = tk.Frame(inner, bg=p["layer"])
        blk.pack(fill="x", padx=pad, pady=(px(SP[2]), 0))
        left = tk.Frame(blk, bg=p["layer"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Obsidian vault", font=self.F["body"], fg=p["ink"], bg=p["layer"],
                 bd=0, padx=0, pady=0).pack(anchor="w")
        vp = (self.cfg.get("vault_path") or "").strip()
        if not vp:
            status = "Off · lets Promptify look referents up in your own notes"
        else:
            idx = vault.load_index(self.history.path.parent)
            if idx and idx.get("vault") == vp:
                ago = max(0, time.time() - idx.get("built", 0))
                ago_s = f"{ago / 3600:.0f} h ago" if ago >= 3600 else f"{ago / 60:.0f} min ago"
                status = f"{Path(vp).name} · indexed {len(idx.get('notes') or [])} notes · {ago_s}"
            else:
                status = f"{Path(vp).name} · indexing…"
        l = tk.Label(left, text=status, font=self.F["meta"], fg=p["muted"], bg=p["layer"],
                     anchor="w", justify="left", bd=0, padx=0, pady=0)
        l.pack(anchor="w", pady=(px(SP[0]), 0))
        self._wrap(l, px(120))
        col = tk.Frame(blk, bg=p["layer"])
        col.pack(side="right")
        if not vp:
            found = vault.known_vaults()
            if found:
                _Btn(self, col, f"Use {Path(found[0]).name}",
                     lambda f=found[0]: self._pick_vault(f), kind="secondary").f.pack(anchor="e")
            _Btn(self, col, "Choose…", self._choose_vault, kind="text").f.pack(
                anchor="e", pady=(px(SP[0]) if found else 0, 0))
        else:
            _Btn(self, col, "Refresh", self._vault_refresh, kind="text").f.pack(anchor="e")
            _Btn(self, col, "Off", lambda: self._pick_vault(None), kind="text").f.pack(
                anchor="e", pady=(px(SP[0]), 0))

    def _split_block(self, inner) -> None:
        """One dictation stays one prompt unless the speaker turns splitting on."""
        p, px, pad = self.pal, self.px, self.px(SP[3])
        self.hairline(inner).pack(fill="x", padx=pad, pady=(px(SP[4]), 0))
        blk = tk.Frame(inner, bg=p["layer"])
        blk.pack(fill="x", padx=pad, pady=(px(SP[2]), 0))
        right = tk.Frame(blk, bg=p["layer"])
        right.pack(side="right")
        left = tk.Frame(blk, bg=p["layer"])
        left.pack(side="left", fill="x", expand=True, padx=(0, px(SP[2])))
        tk.Label(left, text="Split into prompts", font=self.F["body"], fg=p["ink"], bg=p["layer"],
                 bd=0, padx=0, pady=0).pack(anchor="w")
        l = tk.Label(left, text="Off, one take is one prompt; on, unrelated asks in a take "
                     "become separate prompts", font=self.F["meta"], fg=p["muted"], bg=p["layer"],
                     anchor="w", justify="left", bd=0, padx=0, pady=0)
        l.pack(anchor="w", pady=(px(SP[0]), 0))
        self._wrap(l, px(120))
        self._toggle(right, self.cfg.get("prompt_split", False), key="prompt_split")

    def _pick_vault(self, path) -> None:
        self.cfg["vault_path"] = str(path) if path else None
        if path:
            self.cfg["vault_ack"] = False        # the next Promptify says what now travels
            vault.refresh_if_stale(self.cfg, self.history.path.parent)
        self.save()
        self._engine_rows()

    def _choose_vault(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(parent=self.win, title="Choose an Obsidian vault")
        if d:
            self._pick_vault(d)

    def _vault_refresh(self) -> None:
        try:
            vault.index_path(self.history.path.parent).unlink()
        except OSError:
            pass
        vault.refresh_if_stale(self.cfg, self.history.path.parent)
        self._engine_rows()

    def _open_note(self, rel_path) -> None:
        vp = (self.cfg.get("vault_path") or "").strip()
        if not vp or not rel_path:
            return
        try:
            os.startfile(vault.obsidian_uri(vp, rel_path))
        except OSError:
            pass

    def _model_line(self, blk, key) -> tk.Text:
        """Model (active engine only): an underlined mono field, 240 wide or what is left,
        blank = the engine's default; writes cfg["prompt_models"][key]."""
        p, px = self.pal, self.px
        ml = tk.Frame(blk, bg=p["layer"])
        ml.pack(fill="x", pady=(px(SP[1]), 0))
        lab = tk.Label(ml, text="Model", font=self.F["meta"], fg=p["muted"], bg=p["layer"], bd=0, padx=0,
                       pady=0)
        lab.pack(side="left")
        models = self.cfg.get("prompt_models")
        cur = (models.get(key) if isinstance(models, dict) else "") or ""
        fld = self._uline(ml, cur, (1, 1), "default", self.mf["mono"],
                          commit=lambda: self._set_pmodel(key, fld))
        # the host keeps a fixed size (its width is set on the line's <Configure>): the Text's
        # own request is known at once, the packer's only after an idle pass
        fld.host.configure(height=fld.winfo_reqheight() + px(7) + px(6) + 2)
        fld.host.pack_propagate(False)
        fld.host.pack(side="left", padx=(px(SP[2]), 0))
        ml.bind("<Configure>", lambda e: fld.host.configure(width=max(
            px(120), min(px(240), e.width - lab.winfo_reqwidth() - px(SP[2])))))
        self.e_model = fld
        return fld

    def _set_pmodel(self, key, fld) -> None:
        models = self.cfg.get("prompt_models")
        if not isinstance(models, dict):
            models = self.cfg["prompt_models"] = {}
        v = fld.value()
        if (models.get(key) or "") != v:
            models[key] = v
            self.save()

    def _paste_code(self, blk, login) -> tk.Text:
        """Claude's fallback: when the browser could not hand the code back to the CLI, it
        shows one to paste. Return submits it."""
        p, px = self.pal, self.px
        pl = tk.Frame(blk, bg=p["layer"])
        pl.pack(fill="x", pady=(px(SP[1]), 0))
        tk.Label(pl, text="Paste code", font=self.F["meta"], fg=p["muted"], bg=p["layer"], bd=0, padx=0,
                 pady=0).pack(side="left")
        fld = self._uline(pl, "", (1, 1), "if the browser did not finish", self.mf["mono"],
                          commit=lambda: fld.value() and login.submit(fld.value()))
        fld.host.pack(side="left", fill="x", expand=True, padx=(px(SP[2]), 0))
        return fld

    def _connect(self, key) -> None:
        """Connect: the engine's own browser sign-in on a worker (`connect.Login`). Its events
        are queued and read by a ticker on the Tk thread, like a draft's result."""
        if key in self.logins:
            return
        ev = []
        self.eng_err.pop(key, None)
        login = connect.Login(key, lambda kind, text: ev.append((kind, text)))
        self.logins[key] = {"login": login, "ev": ev, "url": None}
        login.start()
        self._engine_rows()
        self._login_tick()

    def _login_tick(self) -> None:
        job = self.jobs.pop("login", None)
        if job:
            self.win.after_cancel(job)
        if not (self.win and self.win.winfo_exists()):
            return
        changed = False
        for key, L in list(self.logins.items()):
            while L["ev"]:
                kind, text = L["ev"].pop(0)
                changed = True
                if kind == "url":
                    L["url"] = text
                    continue
                self.logins.pop(key, None)
                if kind == "done" and self._engine_info()[2] != "connected":
                    self.cfg["prompt_engine"] = key      # the first engine in becomes the one
                    self.save()
                elif kind == "error":
                    self.eng_err[key] = text
        if changed:
            self._eng_cache.clear()
            self._engine_rows()
            self._refresh_engine()
        if self.logins:
            self.jobs["login"] = self.win.after(200, self._login_tick)

    def _cancel_login(self, key) -> None:
        L = self.logins.get(key)
        if L:
            L["login"].cancel()            # the worker answers "cancelled"; the ticker sees it
            self._login_tick()

    def _use(self, key) -> None:
        self.cfg["prompt_engine"] = key
        self._eng_cache.clear()
        self.save()
        self._engine_rows()
        self._refresh_engine()

    def _disconnect_openrouter(self) -> None:
        connect.forget_openrouter()
        self._eng_cache.clear()
        self._engine_rows()
        self._refresh_engine()

    def _refresh_engine(self) -> None:
        """The header engine control follows the engine table; a body that only said "no
        engine" (or nothing yet) is re-shown; one holding a draft keeps its edits."""
        if self.pstate in ("draft", "drafting", "ack"):
            self._head_engine()
            self._layout_head()          # the name's width changed: one row or two, decided now
        else:
            self._show(self._state_for(self.sel))

    # --- refresh -----------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        self.history.prune()
        items = self.history.items
        if not any(it is self.sel for it in items):
            shown = self.list.items()      # the newest row the filter shows, else the newest
            self.sel = (shown or items)[-1] if items else None
        n, days = len(items), self.cfg.get("retention_days", 0)
        kept = "history is off" if not days else f"kept {days:g} days"
        self.count.configure(text=f"{'no' if not n else n} dictation{'s' if n != 1 else ''} · {kept}")
        self.pcount.configure(text=str(n))
        self._clear_cancel()
        self.b_clear.f.pack(side="right") if n else self.b_clear.f.pack_forget()
        self.filter.master.grid() if n else self.filter.master.grid_remove()  # nothing to filter
        self.card.grid() if self.list.items() else self.card.grid_remove()   # the filter may empty it
        self.list.draw()
        self.plist.draw()
        self._select(self.sel)
