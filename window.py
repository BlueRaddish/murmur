"""The app window: transcript history, Promptify (a dictation -> a prompt, and the engines that
write it) and settings. Hidden until opened from the tray.

One Toplevel, a sidebar and three views. The look is the brand green run through an OKLCH 12-step
ramp (Radix semantics) for the accent steps - solid fills and the focus ring - over achromatic
neutrals: a white ground in light, neutral near-black in dark. (Until 2026-08-28 the neutrals
carried the bar's accent hue at 1-2 % chroma; with a red bar colour the whole window read as
pink, and the user wants the window white and the brand green.) Depth is a surface ladder + hairlines
on ROUNDED surfaces: Tk cannot round a widget corner, so every corner in here is PIL-rendered
and handed to Tk as a PhotoImage - controls carry their shape as their own image (`rr_png`),
and surfaces that must stretch wear four corner masks (`corner_pngs`) pinned at their corners.
No motion: hover/active are instant colour swaps. Settings autosave - there is no Save button.
"""
import base64
import datetime
import io
import json
import math
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

import brand
import connect
import promptify

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


# L, C per step. Light descends from a pure white ground; dark is not a mirror - steps 1-2 are
# near-black. Chroma budget: 9/10 (the solid accent fills) and 8 (the focus ring, which has to be
# seen as the accent) carry real chroma; every other step - surfaces, borders, muted and ink text -
# is achromatic. Tinting them from the hue was the method's rule and the user's "pinkish" verdict
# overruled it: at 1-2 % chroma a red hue is a visible blush across every surface.
LIGHT = [(1.0, 0), (0.98, 0), (0.955, 0), (0.93, 0), (0.90, 0), (0.85, 0),
         (0.78, 0), (0.60, .13), (0.55, .17), (0.50, .17), (0.48, 0), (0.25, 0)]
DARK = [(0.14, 0), (0.17, 0), (0.21, 0), (0.24, 0), (0.27, 0), (0.31, 0),
        (0.36, 0), (0.55, .13), (0.60, .14), (0.65, .14), (0.70, 0), (0.92, 0)]
STEPS = ("bg", "surface", "hover", "active", "selected", "border", "border_strong", "ring",
         "primary", "primary_hover", "muted", "ink")
DANGER_HUE = 25.0


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
    """One hue -> the whole system. 12 Radix-semantic steps (also exposed as s1..s12), a danger
    sibling on the same L/C curve at hue 25, and the text colour that sits on a solid fill."""
    h = hex_hue(norm_hex(accent_hex, "#3c8cff"))
    ramp = DARK if dark else LIGHT
    steps = [oklch_hex(L, C, h) for L, C in ramp]
    p = {n: steps[i] for i, n in enumerate(STEPS)}
    p.update({"s%d" % (i + 1): s for i, s in enumerate(steps)})
    dgr = [oklch_hex(L, C, DANGER_HUE) for L, C in ramp]
    p["danger"], p["danger_hover"], p["danger_text"] = dgr[8], dgr[9], dgr[10]
    on = lambda fill: max(("#ffffff", steps[0], steps[11]), key=lambda c: contrast(c, fill))
    p["on_primary"], p["on_danger"] = on(steps[8]), on(dgr[8])
    # the chips' ground: a whisper of the accent over the page (12 % light / 20 % dark), a
    # little more under the pointer and behind the chosen chip - only a little in light, where
    # the focus ring has to stay 3:1 on the chosen chip (16 % holds 3.1; 18 % fell to 2.96)
    p["tint"] = mix(p["bg"], p["primary"], 0.20 if dark else 0.12)
    p["tint_hover"] = mix(p["bg"], p["primary"], 0.28 if dark else 0.16)
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
H_CTL = 32                          # control height
H_ROW = 36                          # history row (one line)
H_ROW2 = 52                         # Promptify's dictation row (two lines)
H_CHIP = 24                         # question chips, the trigger-key chip
H_MARK = 24                         # sidebar lockup: the mark's IMAGE box (its ink is 40/64 of
GAP_MARK = 16                       # that, ~1.9x the wordmark's x-height) and the air after it.
                                    # Both are up from the 20/10 first cut: at that size the mark
                                    # read as a letter and the sidebar said "m murmur"
W_CHIP = 88                         # trigger-key chip: fits "Previous Track" at 9 pt
W_SIDE = 168                        # sidebar
W_FIELD = 96                        # the hex and language entries (7 mono characters)
W_DAYS = 64                         # the retention entry
W_COMBO = 200                       # the microphone and model combos
W_TIME = 104                        # history time column
W_LIST = (176, 0.36, 240)           # Promptify's list pane: clamp(min, share of the main width, max)
W_ACT = 80                          # the engines sheet's action column
WIN = (780, 560)
WIN_MIN = (600, 400)
# the body measure: an 80-character line of body text is as wide as a prompt or a transcript
# gets. (`width=80` on a Text is 80 "0"s, which is ~100 average characters - too wide.)
SAMPLE80 = "The quick brown fox jumps over the lazy dog while the band plays on by the pier."

# --- domain ---------------------------------------------------------------------------------

MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"]
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


def when(t: float, now: float = None) -> str:
    """Relative-day stamp, 24-hour clock: 'Today 14:32' / 'Yesterday 09:10' / 'Mon 21:05' /
    'Aug 12' / 'Aug 12 2025'."""
    now = time.time() if now is None else now
    lt, ln = time.localtime(t), time.localtime(now)
    day = lambda s: datetime.date(s.tm_year, s.tm_mon, s.tm_mday)
    d = (day(ln) - day(lt)).days
    hm = time.strftime("%H:%M", lt)
    if d == 0:
        return "Today " + hm
    if d == 1:
        return "Yesterday " + hm
    if 2 <= d <= 6:
        return time.strftime("%a ", lt) + hm
    out = time.strftime("%b ", lt) + str(lt.tm_mday)
    return out if lt.tm_year == ln.tm_year else out + " " + str(lt.tm_year)


# --- PIL-rendered round things --------------------------------------------------------------

SS = 3          # supersampling: Tk's canvas has no anti-aliasing, PIL does


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.reduce(SS).save(buf, "PNG")
    return buf.getvalue()


def dot_png(box: int, d: int, fill: str, ring: str = None, ring_w: int = 2, gap: int = 2,
            edge: str = None) -> bytes:
    """A filled circle of diameter d centred in a transparent box; `edge` is its own hairline
    (white would otherwise disappear on the light ground), `ring` the selection halo."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (box * SS, box * SS), (0, 0, 0, 0))
    dr, c, r = ImageDraw.Draw(im), box * SS / 2, d * SS / 2
    dr.ellipse([c - r, c - r, c + r, c + r], fill=fill, outline=edge, width=SS if edge else 0)
    if ring:
        rr = r + (gap + ring_w / 2) * SS
        dr.ellipse([c - rr, c - rr, c + rr, c + rr], outline=ring, width=int(ring_w * SS))
    return _png(im)


def pill_png(w: int, h: int, track: str, knob: str, on: bool) -> bytes:
    """The toggle switch: a capsule with the knob at one end."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=h * SS / 2, fill=track)
    r, m = (h - 6) * SS / 2, 3 * SS
    cx = w * SS - m - r if on else m + r
    dr.ellipse([cx - r, h * SS / 2 - r, cx + r, h * SS / 2 + r], fill=knob)
    return _png(im)


def rr_png(w: int, h: int, r: int, fill: str, border: str = None, border_w: int = 1,
           ground: str = None, bar: tuple = None, inner: tuple = None) -> bytes:
    """A rounded rectangle with an optional hairline - the shape a Tk widget cannot have, drawn
    at SS and reduced so the arc is anti-aliased. `ground` is the colour BEHIND the corners: pass
    it and the PNG is opaque (what a canvas item wants); leave it out and the corners are
    transparent for Tk to composite against a Label's own bg. `bar` = (x, w, h, radius, colour):
    an accent pill drawn inside, vertically centred. `inner` = (inset, width, colour): a second
    outline INSIDE the edge, that far in, on the arc of radius r - inset - the focus ring of a
    solid button, whose fill the accent ring would vanish against."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w * SS, h * SS), ground if ground else (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=r * SS, fill=fill,
                         outline=border, width=int(border_w * SS) if border else 0)
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
    is it cut at the cell boundaries. Each slice goes on its own Label, which is how the cells
    stay real widgets (one tab stop on the group, a <Button-1> per option) with round corners."""
    from PIL import Image, ImageDraw
    w = sum(widths)
    im = Image.new("RGBA", (w * SS, h * SS), ground)
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=r * SS, fill=fill,
                         outline=border, width=int(border_w * SS))
    for j, cell in enumerate(fills):
        if cell:
            x = sum(widths[:j])
            dr.rounded_rectangle([(x + inset) * SS, inset * SS,
                                  (x + widths[j] - inset) * SS - 1, (h - inset) * SS - 1],
                                 radius=max(0, r - inset) * SS, fill=cell)
    x0 = sum(widths[:i])
    return _png(im.crop((x0 * SS, 0, (x0 + widths[i]) * SS, h * SS)))


# --- widgets --------------------------------------------------------------------------------


class _Btn:
    """A button is ONE Label carrying a rounded-rect image with its text drawn on top
    (`compound="center"`): the shape is the image, so hover, press and focus are re-renders of
    it - no motion, no weight or size shift. Kinds: primary (the one accent fill), danger,
    secondary (a `hover`-filled pill, ink text, no border), text (bare until hovered; muted, or
    `ink`) and chip (a full pill on the accent tint, see `_Chip`). The focus ring is drawn on the
    button's own edge (2 px of `ring`) instead of around it, so focusing never moves anything;
    a solid button, whose fill the ring would vanish against (1.2:1), wears it INSIDE its edge
    in its own text colour."""

    def __init__(self, ui, parent, text, cmd, kind="secondary", font=None, h=None, pad=None,
                 r=None, ink=False):
        p, self.ui, self.cmd, self.kind = ui.pal, ui, cmd, kind
        g = self.ground = parent["bg"]
        chip = kind == "chip"
        # rest fill, rest text, hover fill, hover text, pressed fill
        self.fill, self.fg, self.hov, self.hov_fg, self.down = {
            "primary": (p["primary"], p["on_primary"], p["primary_hover"], p["on_primary"], p["primary_hover"]),
            "danger": (p["danger"], p["on_danger"], p["danger_hover"], p["on_danger"], p["danger_hover"]),
            "secondary": (p["hover"], p["ink"], p["active"], p["ink"], p["selected"]),
            "text": (g, p["ink"] if ink else p["muted"], p["hover"], p["ink"], p["active"]),
            "chip": (p["tint"], p["ink"], p["tint_hover"], p["ink"], p["tint_hover"]),
        }[kind]
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
                    "down": (self.down, self.hov_fg)}[self.state]
        if not self.on:
            fill, fg = mix(fill, self.ground, .45), mix(fg, self.ground, .45)
        border, bw, inner = None, 1, None
        if self.ring and self.kind in ("primary", "danger"):
            inner = (self.ui.px(2), self.ui.px(2), fg)
        elif self.ring:
            border, bw = p["ring"], self.ui.px(2)
        self._draw(self.ui.rr(self.w, self.h, self.r, fill, self.ground, border, bw, inner=inner), fg)

    def _draw(self, img, fg) -> None:
        self.f.configure(image=img, fg=fg)

    def _set(self, state) -> None:
        if self.on:
            self.state = state
            self._paint()

    def _ring(self, on) -> None:
        self.ring = on
        self._paint()

    def _release(self, e) -> None:
        if not self.on:
            return
        self._set("hover")
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
    not tab stops; their strip is (`_chips`), and the ring goes on the one under its cursor."""

    def __init__(self, ui, parent, text, cmd):
        self.label, self.is_chosen = text, False
        super().__init__(ui, parent, text, cmd, kind="chip")

    def _widget(self, parent, text, font) -> tk.Widget:
        c = tk.Canvas(parent, bd=0, highlightthickness=0, bg=self.ground, cursor="hand2", takefocus=0)
        self.i_pill = c.create_image(0, 0, anchor="nw")
        self.i_dot = c.create_image(self.ui.px(7), self.h // 2, anchor="w", state="hidden",
                                    image=self.ui.dot(8, 6, self.ui.pal["primary"]))
        self.i_text = c.create_text(self.pad, self.h // 2, anchor="w", text=text, font=font)
        return c

    def _size(self) -> None:
        self.w = (self.ui.mf["body"].measure(self.label) + 2 * self.pad
                  + (self.ui.px(SP[2]) if self.is_chosen else 0))   # dot 6 + gap 6 before the text

    def _draw(self, img, fg) -> None:
        c = self.f
        c.configure(width=self.w, height=self.h)
        c.itemconfigure(self.i_pill, image=img)
        c.itemconfigure(self.i_text, fill=fg)
        c.coords(self.i_text, self.pad + (self.ui.px(SP[2]) if self.is_chosen else 0), self.h // 2)
        c.itemconfigure(self.i_dot, state="normal" if self.is_chosen else "hidden")

    def text(self, s, hold=True) -> None:
        self.label = s
        self.f.itemconfigure(self.i_text, text=s)
        self._size()
        self._paint()

    def chosen(self, on) -> None:
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
    stack of full-width rules. 8 px above and below the taller column: a label over its
    description makes the row 52. The Labels carry none of Tk's default border/padding
    (2 + 1 px a side), so their text lands on E and the row on the grid."""

    def __init__(self, ui, group, label, desc=None):
        p, bg = ui.pal, group["bg"]
        self.rule = ui.hairline(group, color=p["s5"]) if group.winfo_children() else None
        if self.rule is not None:
            self.rule.pack(fill="x", padx=ui.cpad)
        self.ui, self.shown = ui, True
        self.frame = tk.Frame(group, bg=bg)
        self.frame.pack(fill="x")
        main = tk.Frame(self.frame, bg=bg)
        main.pack(fill="x", padx=ui.cpad)
        self.right = tk.Frame(main, bg=bg)
        self.right.pack(side="right", pady=ui.px(SP[1]))   # the control's own width; the label
        left = tk.Frame(main, bg=bg)                        # column takes what is left
        left.pack(side="left", fill="x", expand=True, pady=ui.px(SP[1]), padx=(0, ui.px(SP[3])))
        self.head = tk.Frame(left, bg=bg)
        self.head.pack(anchor="w")
        tk.Label(self.head, text=label, font=ui.F["body"], fg=p["ink"], bg=bg, bd=0, padx=0,
                 pady=0).pack(side="left")
        self.chip = None
        self.desc = tk.Label(left, text=desc or "", font=ui.F["meta"], fg=p["muted"], bg=bg,
                             anchor="w", justify="left", bd=0, padx=0, pady=0)
        left.bind("<Configure>",     # wrap rather than run under the control when narrow
                  lambda e: self.desc.configure(wraplength=max(e.width - ui.px(SP[3]), ui.px(120))))
        if desc:
            self.desc.pack(anchor="w", pady=(ui.px(SP[0]), 0))
        self.err = tk.Label(self.frame, text="", font=ui.F["meta"], fg=p["danger_text"], bg=bg,
                            anchor="e", bd=0, padx=0, pady=0)

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
    would each need their own hover bindings. Two modes: one line (History: time, text, a draft
    dot at the inner right; `H_ROW`) and two lines (Promptify: text over time + dot; `H_ROW2`).

    Hover and selection are not per-row rectangles but TWO rounded images moved to the row they
    belong to - a rounded highlight would otherwise cost three rectangles and four corner masks
    per row, and only two rows are ever lit. The keyboard's row wears the focus ring on the
    selected highlight itself: a hard ring around the whole list would be the one rigid line in
    the view. The selection is the window's (`ui.sel`, shared by both lists); `on_select(item)`
    is told when a row is picked, `keys` are the pane's own bindings (Return, Delete...).

    Ceiling: `draw()` redraws EVERY row on every <Configure>, ~7 font.measure calls each. Fine to
    about 2 000 items (a year at 5 dictations a day); past that the upgrade path is to draw only
    the slice between canvasy(0) and canvasy(0)+height, and redraw on scroll too."""

    def __init__(self, ui, parent, two_line=False, on_select=None, keys=None):
        p = ui.pal
        super().__init__(parent, bg=p["bg"], highlightthickness=0, takefocus=1, yscrollincrement=1,
                         width=1, height=1, bd=0)
        self.ui, self.two, self.on_select = ui, two_line, on_select or (lambda it: None)
        self.H = ui.px(H_ROW2 if two_line else H_ROW)
        self.rows, self.hl, self.hl_id, self.hover = [], {}, {}, None
        self.sb = ttk.Scrollbar(parent, orient="vertical", style="M.Vertical.TScrollbar",
                                command=self.yview, takefocus=0)
        self.configure(yscrollcommand=self._scrolled)
        self.pack(side="left", fill="both", expand=True)
        self.bind("<Configure>", lambda e: self.draw())
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
        return self.ui.history.items

    def _scrolled(self, lo, hi) -> None:
        self.sb.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.sb.pack_forget()
        else:
            self.sb.pack(side="right", fill="y")

    def _wheel(self, e) -> None:
        if self.ui._visible():
            self.yview_scroll(int(-e.delta / 120) * 3 * self.H, "units")

    def draw(self) -> None:
        ui, p, c = self.ui, self.ui.pal, self
        px = ui.px
        c.delete("all")
        self.rows = []
        items = self.items()
        w, h = c.winfo_width(), self.H
        x0 = px(SP[3])                          # E, the pane's one text edge
        if not items:
            self.hl_id = {}       # the highlights went with delete("all"): nothing to move
            self.empty(w)
            c.configure(scrollregion=(0, 0, 0, 0))
            return
        # the highlight runs from E - 12 to the pane's right edge - 4, like every surface that
        # carries text; the text keeps its edge, 12 px inside it
        hx = px(SP[0])
        hw = max(h, w - 2 * hx)   # w is 1 until Tk has laid the canvas out
        if self.hl.get("w") != hw:
            png = lambda **kw: tk.PhotoImage(data=base64.b64encode(rr_png(
                hw, h, px(R_CTL), ground=p["bg"], **kw)).decode())
            # not in the img() cache: these are as wide as the window and would pile up a copy
            # per pixel of a resize drag
            self.hl = {"w": hw, "hover": png(fill=p["hover"]), "sel": png(fill=p["selected"]),
                       "focus": png(fill=p["selected"], border=p["ring"], border_w=px(2))}
        self.hl_id = {k: c.create_image(hx, 0, anchor="nw", image=self.hl[k], state="hidden")
                      for k in ("hover", "sel")}     # created first: the row text draws over them
        right = hx + hw - px(SP[2])                  # the highlight's inner right edge
        dot = ui.dot(8, 6, p["primary"])
        for i, idx in enumerate(range(len(items) - 1, -1, -1)):
            it, y = items[idx], i * h
            body = " ".join(it["text"].split())
            t = when(it["t"])
            if self.two:      # line 1 y+8..26: the words; line 2 y+28..44: the time, then the dot
                c.create_text(x0, y + px(17), anchor="w", font=ui.mf["body"], fill=p["ink"],
                              text=ellipsize(ui.mf["body"], body, right - x0))
                c.create_text(x0, y + px(36), anchor="w", text=t, fill=p["muted"], font=ui.mf["meta"])
                if it.get("draft"):
                    c.create_image(x0 + ui.mf["meta"].measure(t) + px(SP[1]), y + px(36),
                                   anchor="w", image=dot)
            else:
                c.create_text(x0, y + h / 2, anchor="w", text=t, fill=p["muted"], font=ui.mf["meta"])
                tx = x0 + px(W_TIME)
                c.create_text(tx, y + h / 2, anchor="w", font=ui.mf["body"], fill=p["ink"],
                              text=ellipsize(ui.mf["body"], body, right - px(14) - tx))
                if it.get("draft"):
                    c.create_image(right, y + h / 2, anchor="e", image=dot)
            self.rows.append((it, y))
        c.configure(scrollregion=(0, 0, w, len(items) * h))
        self.paint()

    def empty(self, w) -> None:
        """Hung on the one left edge, at the top: centred in the whole column it was an orphan
        half a screen below the header it belongs to, and the widest muted line in the app was
        the first thing the eye landed on."""
        ui, p = self.ui, self.ui.pal
        days = ui.cfg.get("retention_days", 0)
        off = not days
        x, f, mono, gap = ui.px(SP[3]), ui.mf["body"], ui.mf["mono"], ui.px(SP[0])
        lh = f.metrics("linespace")
        width = min(ui.measure, max(w - 2 * x, ui.px(120)))     # E to E-from-the-right
        self.create_text(x, 0, anchor="nw", text="History is off" if off else "No dictations yet",
                         font=f, fill=p["ink"])
        y = lh + gap
        if off:
            self.create_text(x, y, anchor="nw", font=f, fill=p["muted"], width=width, justify="left",
                             text="Set ‘Keep dictations for’ in Settings to keep dictations.")
            return
        # three items on ONE baseline: the key is in mono, which hangs a different descent
        base = y + lh - f.metrics("descent")
        for text, font in (("Hold ", f), ("Ctrl+Win", mono), (" and talk.", f)):
            self.create_text(x, base + font.metrics("descent"), anchor="sw", text=text, font=font,
                             fill=p["muted"])
            x += font.measure(text)
        if self.two:          # the retention sentence is History's; the pane beside it has a header
            return
        self.create_text(ui.px(SP[3]), y + lh + gap, anchor="nw", font=f, fill=p["muted"],
                         width=width, justify="left",
                         text="What you say is typed where your cursor is, and kept here for "
                              f"{days:g} days.")

    def paint(self) -> None:
        ui = self.ui
        for key, want in (("hover", self.hover), ("sel", ui.sel)):
            item = self.hl_id.get(key)
            if item is None:
                continue
            y = next((y for it, y in self.rows if it is want), None)
            if y is None or (key == "hover" and want is ui.sel):
                self.itemconfigure(item, state="hidden")
            else:
                self.coords(item, ui.px(SP[0]), y)
                self.itemconfigure(item, state="normal")
                if key == "sel":     # the keyboard's row wears the focus ring
                    self.itemconfigure(item, image=self.hl[
                        "focus" if self.focus_get() is self else "sel"])

    def row_at(self, y):
        i = int(self.canvasy(y) // self.H)
        return self.rows[i][0] if 0 <= i < len(self.rows) else None

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
        order = [r[0] for r in self.rows]
        if not order:
            return
        i = next((j for j, it in enumerate(order) if it is self.ui.sel), -1)
        self.on_select(order[max(0, min(len(order) - 1, i + step))])
        self.show_sel()

    def show_sel(self) -> None:
        """Scroll the selected row into view."""
        i = next((j for j, (it, _) in enumerate(self.rows) if it is self.ui.sel), None)
        if i is None:
            return
        top, total, vis = i * self.H, max(1, len(self.rows) * self.H), self.winfo_height()
        if top < self.canvasy(0):
            self.yview_moveto(top / total)
        elif top + self.H > self.canvasy(0) + vis:
            self.yview_moveto((top + self.H - vis) / total)


class AppWindow:
    """root: the (withdrawn) Tk root. cfg: the live config dict shared with the app.
    on_save(cfg): persist settings. links: {"vocab": fn, "folder": fn} for the sidebar footer.
    theme: "light"/"dark" to override the Windows setting (tests and screenshots).
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
        self.icon = icon
        self.win = None
        self.view = "history"     # remembered between show() and hide()
        self.sel = None           # the selected item itself: survives a delete + undo
        self.imgs = {}            # Tk drops an image nobody references
        self.jobs = {}            # named after() ids, so a second flash cancels the first
        self.ctl = {}             # the settings controls, by cfg key (the tests drive these)
        self.foot_links = []      # sidebar footer links, one per entry in `links`
        self.cards = []           # the settings group cards, top to bottom
        self.primaries = []       # every primary _Btn, so a check can count the ones on screen
        self.undo = None          # (index, item) while the undo offer stands
        self.drafting = None      # the item a draft is being written for, while the worker runs
        self.draft_item = None    # ... the last one (kept name)
        self.last_call = None     # (item, prompts, questions, answers) of the last draft: Try again
        self.p_item = None        # the item the Promptify detail pane shows
        self.pstate = "pick"      # ... and in which state (STATES)
        self.p_err = None         # (item, message) after a failed draft, until the next attempt
        self.p_fields = {}        # the draft body's live widgets: prompts, questions, answers...
        self.eng_err = {}         # engine key -> its last failure message (the sheet shows it)
        self.logins = {}          # engine key -> {"login", "ev", "url"} while its sign-in runs

    def px(self, n) -> int:
        return max(1, int(round(n * self.scale)))

    # --- lifecycle ---------------------------------------------------------------------------
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
        self.dark = (self.theme == "dark") if self.theme else system_dark()
        # the brand green, not the bar's colour: the bar colour is the user's per-take signal and
        # can be anything (their red made the whole window pink); the window is the product's
        p = self.pal = palette(brand.GREEN, self.dark)
        self.F = {"title": ("Segoe UI Semibold", 12), "body": ("Segoe UI", 10),
                  "meta": ("Segoe UI", 9), "mono": ("Cascadia Mono", 10),
                  "mono9": ("Cascadia Mono", 9)}
        w = self.win = tk.Toplevel(self.root)
        w.title("murmur")
        if self.icon and Path(self.icon).exists():
            try:
                w.iconbitmap(str(self.icon))
            except tk.TclError:
                pass
        w.configure(bg=p["bg"])
        w.geometry(f"{self.px(WIN[0])}x{self.px(WIN[1])}")
        w.minsize(self.px(WIN_MIN[0]), self.px(WIN_MIN[1]))
        w.protocol("WM_DELETE_WINDOW", self.hide)
        self.mf = {k: tkfont.Font(w, family=f[0], size=f[1]) for k, f in self.F.items()}
        self.measure = self.mf["body"].measure(SAMPLE80)    # the body column's width, device px
        # a card starts 12 px before the pane's text edge E (like a row highlight) and spends
        # 1 px on its hairline, so its padding is 12 minus that px - which puts every line of
        # text inside it back on E, cards or no cards
        self.cpad = self.px(SP[2]) - 1
        self._ttk()
        if self.dark:
            self._dark_titlebar()
        w.grid_columnconfigure(2, weight=1)
        w.grid_rowconfigure(0, weight=1)
        self._sidebar().grid(row=0, column=0, sticky="ns")
        self.hairline(w, vertical=True).grid(row=0, column=1, sticky="ns")
        content = tk.Frame(w, bg=p["bg"])
        content.grid(row=0, column=2, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        self.views = {}
        for name, build in (("history", self._build_history), ("promptify", self._build_promptify),
                            ("settings", self._build_settings)):
            f = tk.Frame(content, bg=p["bg"])
            f.grid(row=0, column=0, sticky="nsew")
            self.views[name] = f
            build(f)
        w.bind("<Escape>", lambda e: self._escape())
        w.bind("<Up>", lambda e: self._nav_key(-1))
        w.bind("<Down>", lambda e: self._nav_key(1))
        w.bind("<Control-Return>", lambda e: self.view == "promptify" and self._update())   # Update prompt
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
        fld = self.up(p["surface"])
        st.configure("M.TCombobox", fieldbackground=fld, background=fld, foreground=p["ink"],
                     bordercolor=fld, arrowcolor=p["muted"], lightcolor=fld, darkcolor=fld,
                     insertcolor=p["ink"], selectbackground=p["hover"], selectforeground=p["ink"],
                     padding=(0, 4), arrowsize=self.px(SP[1]))   # clam's arrow is a 2 px sliver
        st.map("M.TCombobox", fieldbackground=[("readonly", fld)],
               foreground=[("readonly", p["ink"])], bordercolor=[("focus", fld)],
               arrowcolor=[("active", p["ink"])])
        for k, v in (("*TCombobox*Listbox.background", p["surface"]),
                     ("*TCombobox*Listbox.foreground", p["ink"]),
                     ("*TCombobox*Listbox.selectBackground", p["active"]),
                     ("*TCombobox*Listbox.selectForeground", p["ink"])):
            self.root.option_add(k, v)
        self.root.option_add("*TCombobox*Listbox.font", self.mf["body"])
        # clam's scrollbar is as thick as its ARROWS, arrows or no arrows (`width` is ignored),
        # and spends 1 px of that on a `bordercolor` strip either side of the thumb: the strips
        # are painted in the ground's colour and the widget made 2 px wider, so the thumb that
        # shows is the 4 px the spec draws. The thumb's own light/dark edges are its colour.
        # Two styles, one per ground: "M" on the page, "C" inside the History card.
        for name, ground in (("M", p["bg"]), ("C", p["surface"])):
            style = f"{name}.Vertical.TScrollbar"
            st.layout(style,     # no arrows: trough + thumb
                      [("Vertical.Scrollbar.trough",
                        {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                                       {"expand": "1", "sticky": "nswe"})]})])
            st.configure(style, troughcolor=ground, background=p["border_strong"], bordercolor=ground,
                         darkcolor=p["border_strong"], lightcolor=p["border_strong"],
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
    def hairline(self, parent, vertical=False, color=None) -> tk.Frame:
        n = {"width" if vertical else "height": 1}
        return tk.Frame(parent, bg=color or self.pal["border"], **n)

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

    def rr(self, w, h, r, fill, ground, border=None, bw=1, bar=None, inner=None) -> tk.PhotoImage:
        """Cached rounded rect, sizes in device px. Every control's corner comes from here."""
        return self.img(("rr", w, h, r, fill, ground, border, bw, bar, inner),
                        lambda: rr_png(w, h, r, fill, border, bw, ground, bar, inner))

    def dot(self, box, d, fill, edge=None) -> tk.PhotoImage:
        """A `d` px dot in a `box` px image, sizes in logical px: the state dots. No `fill` but
        an `edge` is the hollow one (not connected)."""
        return self.img(("dot", box, d, fill, edge),
                        lambda: dot_png(self.px(box), self.px(d), fill, edge=edge))

    def up(self, ground, n=1) -> str:
        """The next step(s) UP the surface ladder from `ground`. A field is one step above what
        it sits on - `surface` on the window ground, a step lighter again inside a card - so a
        control never disappears into the surface that happens to be under it."""
        for i in range(1, 13):
            if self.pal["s%d" % i] == ground:
                return self.pal["s%d" % min(12, i + n)]
        return self.pal["surface"]

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
        """A grouped surface: `surface` fill, 1 px hairline, rounded. The hairline is the outer
        frame showing through a 1 px margin - a Tk highlight ring cannot be rounded, and place()
        measures from inside it. Rows go in `.body`."""
        p, ground = self.pal, parent["bg"]
        outer = tk.Frame(parent, bg=p["border"])
        inner = tk.Frame(outer, bg=p["surface"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self._corners(outer, self.px(radius or R_CARD), p["surface"], ground, p["border"])
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

    def flash(self, label, text, ms=2000) -> None:
        label.configure(text=text)
        job = self.jobs.pop(id(label), None)
        if job:
            self.win.after_cancel(job)
        self.jobs[id(label)] = self.win.after(ms, lambda: label.configure(text=""))

    # --- sidebar -----------------------------------------------------------------------------
    def _sidebar(self) -> tk.Frame:
        p = self.pal
        s = tk.Frame(self.win, bg=p["surface"], width=self.px(W_SIDE))
        s.pack_propagate(False)
        lock = self._lockup(s)
        # a 32 px row at y 16, like the view titles across the hairline; where the mark
        # overshoots that row the canvas grows upward and the top pad gives the same height
        # straight back, so the nav below starts at 64 whatever the mark's size
        lock.pack(anchor="w", padx=self.px(SP[3]),
                  pady=(self.px(PAD_TOP) - lock.rise, self.px(SP[3])))
        self.nav = {}
        for name, label in (("history", "History"), ("promptify", "Promptify"), ("settings", "Settings")):
            # the pill IS the row's image; the text sits on a Label placed inside it, inset by
            # the radius so its square background can never eat a rounded corner
            row = tk.Label(s, bd=0, highlightthickness=0, bg=p["surface"], cursor="hand2",
                           padx=0, pady=0)
            row.pack(padx=self.px(SP[1]), pady=(0, self.px(2)))
            l = tk.Label(row, text=label, font=self.F["body"], fg=p["muted"], bg=p["surface"],
                         anchor="w", padx=0, bd=0, highlightthickness=0)
            l.place(x=self.px(R_CTL), y=0, relwidth=1.0, width=-2 * self.px(R_CTL), relheight=1.0)
            self.nav[name] = (row, l)
            self._nav_paint(name, p["surface"])
            for wdg in (row, l):
                wdg.bind("<Button-1>", lambda e, n=name: self.go(n))
                wdg.bind("<Enter>", lambda e, n=name: self._nav_hover(n, True))
                wdg.bind("<Leave>", lambda e, n=name: self._nav_hover(n, False))
        foot = tk.Frame(s, bg=p["surface"])
        foot.pack(side="bottom", anchor="w", padx=self.px(SP[3]), pady=self.px(SP[3]))
        made = [("vocab", "vocab.txt"), ("folder", "config folder")]
        for i, (key, text) in enumerate([m for m in made if m[0] in self.links]):
            if i:
                tk.Label(foot, text="·", font=self.F["meta"], fg=p["border_strong"],
                         bg=p["surface"]).pack(side="left", padx=self.px(SP[0]))
            l = self.link(foot, text, self.links[key])
            l.pack(side="left")
            self.foot_links.append(l)
        return s

    def _lockup(self, parent) -> tk.Canvas:
        """mark + "murmur" on ONE baseline. Both in `muted`: up here the identity is chrome, the
        same weight as the word it sits next to, so "History" still reads louder and the accent
        stays reserved for the primary action.

        A Canvas because only a canvas can share a baseline between a PNG and a text: both are
        anchored "sw", the image on the baseline itself (mark() has cropped its margins away) and
        the text one descent below it, which is where a "sw" text hangs its box. The mark's left
        edge at x=0 puts it on the same left edge as the nav labels below. The word sits where
        a title Label sits in its 32 px row (line box centred), so the two share a baseline
        across the hairline; `rise` is how far a tall mark pokes above that row."""
        p, f = self.pal, self.mf["title"]
        m = self.mark(self.px(H_MARK), brand.GREEN)      # the mark in the brand green (user's ask
        # 2026-08-29: "the logo should be green, not just gray"); the word stays muted chrome
        lh, desc, row = f.metrics("linespace"), f.metrics("descent"), self.px(H_CTL)
        base = (row - lh) // 2 + lh - desc                # the baseline's y inside the row
        rise = max(0, m.height() - base)
        base += rise
        x = m.width() + self.px(GAP_MARK)
        c = tk.Canvas(parent, bg=p["surface"], bd=0, highlightthickness=0,
                      width=x + f.measure("murmur"), height=row + rise)
        c.create_image(0, base, anchor="sw", image=m)
        c.create_text(x, base + desc, anchor="sw", text="murmur", font=f, fill=p["muted"])
        c.rise = rise
        return c

    def _nav_paint(self, name, fill) -> None:
        row, l = self.nav[name]
        row.configure(image=self.rr(self.px(W_SIDE - 2 * SP[1]), self.px(H_CTL), self.px(R_CTL),
                                    fill, self.pal["surface"]))
        l.configure(bg=fill)

    def _nav_hover(self, name, on) -> None:
        if name == self.view:
            return
        p = self.pal
        self._nav_paint(name, p["hover"] if on else p["surface"])

    def go(self, name) -> None:
        self.view = name
        p = self.pal
        for n, (row, l) in self.nav.items():
            sel = n == name
            self._nav_paint(n, p["selected"] if sel else p["surface"])
            l.configure(fg=p["ink"] if sel else p["muted"])
        for n, v in self.views.items():        # not tkraise: an unmapped frame is skipped by
            v.grid() if n == name else v.grid_remove()   # tk_focusNext, a raised-over one is not
        lst = self._list()
        if lst is not None:                    # both lists share the selection: the one shown
            lst.paint()                        # catches up and brings the row into view
            self.win.after_idle(lst.show_sel)
        if name == "promptify":
            self._show(self._state_for(self.sel))

    # --- history view ------------------------------------------------------------------------
    def _build_history(self, f) -> None:
        p, pad = self.pal, self.px(SP[3])
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        head = tk.Frame(f, bg=p["bg"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), pad))
        head.pack_propagate(False)                 # the title row is 32 high with or without a button
        tk.Label(head, text="History", font=self.F["title"], fg=p["ink"], bg=p["bg"], bd=0, padx=0,
                 pady=0).pack(side="left")
        self.count = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"], bd=0,
                              padx=0, pady=0)
        self.count.pack(side="left", padx=(self.px(SP[2]), 0))
        self.hdr_r = tk.Frame(head, bg=p["bg"])
        self.hdr_r.pack(side="right")
        self.b_clear = _Btn(self, self.hdr_r, "Clear all", self._clear_ask)
        self.b_clear.f.pack(side="right")
        self.confirm = tk.Frame(head, bg=p["bg"])
        self.c_label = tk.Label(self.confirm, text="", font=self.F["body"], fg=p["ink"], bg=p["bg"])
        self.c_label.pack(side="left", padx=(0, self.px(SP[2])))
        _Btn(self, self.confirm, "Clear", self._clear_do, kind="danger").f.pack(side="left")
        _Btn(self, self.confirm, "Keep", self._clear_cancel).f.pack(side="left", padx=(self.px(SP[1]), 0))

        # no rule under the header: a full-bleed hairline over a list of rounded highlights is
        # the most rigid line on the screen, and the air below the title separates them anyway
        box = self.box = tk.Frame(f, bg=p["bg"])
        box.grid(row=1, column=0, sticky="nsew")
        self.list = RowList(self, box, on_select=self._select, keys={
            "<Return>": self.copy_selected, "<Control-c>": self.copy_selected,
            "<Double-Button-1>": self.copy_selected, "<Delete>": self.delete_selected,
            "<Control-d>": self._to_promptify})

        f.bind("<Configure>", self._fit_detail)
        # the selected transcript and its two actions are ONE grouped surface, from E - 12 to
        # the pane's right - 4 like the row highlights above it; its text lands on E like theirs
        self.card = self._card(f)
        self.card.grid(row=2, column=0, sticky="ew", padx=self.px(SP[0]), pady=self.px(SP[3]))
        card = self.card.body
        trow = tk.Frame(card, bg=p["surface"])
        trow.pack(fill="x", padx=self.cpad, pady=(self.cpad, 0))
        self.detail = tk.Text(trow, height=1, width=1, wrap="word", relief="flat", bd=0,
                              highlightthickness=0, bg=p["surface"], fg=p["ink"],
                              font=self.mf["body"], padx=0, pady=0,
                              spacing1=self.px(LH), spacing3=self.px(LH), state="disabled",
                              cursor="xterm", selectbackground=p["selected"],
                              selectforeground=p["ink"], inactiveselectbackground=p["selected"])
        # no fill: stretched, the width request is ignored and a maximised window gives the
        # transcript ~200-character lines - `_fit_detail` holds it to the body measure
        self.detail.pack(side="left")
        # the transcript's own thumb, at the card's inner right, only while it has more lines
        # than the card shows (`_fit_height`)
        self.dsb = ttk.Scrollbar(trow, orient="vertical", style="C.Vertical.TScrollbar",
                                 command=self.detail.yview, takefocus=0)
        self.detail.configure(yscrollcommand=self.dsb.set)

        act = self.act = tk.Frame(card, bg=p["surface"])
        act.pack(fill="x", padx=self.cpad, pady=(self.px(SP[2]), self.cpad))
        self.b_copy = _Btn(self, act, "Copy", self.copy_selected, kind="primary")
        self.b_copy.f.pack(side="left")
        # the way into the Promptify view from here: the same row, drafting at once (Ctrl+D)
        self.b_prompt = _Btn(self, act, "Promptify", self._to_promptify)
        self.b_prompt.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.b_del = _Btn(self, act, "Delete", self.delete_selected)
        self.b_del.f.pack(side="left", padx=(self.px(SP[1]), 0))
        # the status ("Copied", "Deleted · Undo") is meta on the SAME row, after the buttons -
        # never in the body flow, never at the far edge where the eye has to travel for it
        self.status = tk.Frame(act, bg=p["surface"])
        self.status.pack(side="left", padx=(self.px(SP[2]), 0))
        self.s_text = tk.Label(self.status, text="", font=self.F["meta"], fg=p["muted"],
                               bg=p["surface"])
        self.s_text.pack(side="left")
        # underlined so the one clickable word in the status line does not read as more meta
        self.s_undo = self.link(self.status, "Undo", self._undo, font=self.F["meta"] + ("underline",))

    @property
    def rows(self) -> list:
        return self.list.rows

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
            room = (H - px(PAD_TOP) - px(H_CTL) - px(SP[3]) - len(self.history.items) * self.list.H
                    - px(SP[3]) - chrome - px(SP[3]))
            fit = room // lh
            cap = fit if fit >= 3 else (6 if H >= px(480) else 3)
            h = max(1, min(lines, cap))
            if int(self.detail.cget("height")) != h:
                self.detail.configure(height=h)
            if lines > h:
                self.dsb.pack(side="right", fill="y")
            else:
                self.dsb.pack_forget()
                self.detail.yview_moveto(0)
        finally:
            self._fitting = False

    def _select(self, it) -> None:
        """The one selection, shared by both lists; the History card and the Promptify detail
        pane follow it."""
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

    def _move(self, step) -> None:
        lst = self._list()
        if lst is not None:
            lst.move(step)

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
        self.flash(self.s_text, "Copied")

    def delete_selected(self) -> None:
        i = self._idx()
        if i is None:
            return
        item = self.history.items[i]
        self.history.delete(i)
        self.sel = None
        self.undo = (i, item)
        self.refresh()
        self.s_text.configure(text="Deleted ·")
        self.s_undo.pack(side="left", padx=(self.px(SP[0]), 0))
        job = self.jobs.pop(id(self.s_text), None)
        if job:
            self.win.after_cancel(job)
        self.jobs[id(self.s_text)] = self.win.after(8000, self._undo_expire)   # the offer lapses

    def _undo_expire(self) -> None:
        self.undo = None
        self.s_undo.pack_forget()
        self.s_text.configure(text="")

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

    clear_all = _clear_do          # kept: the old public name

    # --- settings view -----------------------------------------------------------------------
    def _build_settings(self, f) -> None:
        """Title row, the scroll-only rule at 56, the groups in a body canvas from 64 (the
        Promptify pane's `_scroller`)."""
        p, pad = self.pal, self.px(SP[3])
        f.grid_rowconfigure(2, weight=1)
        f.grid_columnconfigure(0, weight=1)
        head = tk.Frame(f, bg=p["bg"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[1])))
        head.pack_propagate(False)
        tk.Label(head, text="Settings", font=self.F["title"], fg=p["ink"], bg=p["bg"], bd=0, padx=0,
                 pady=0).pack(side="left")
        self.saved = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"], bd=0,
                              padx=0, pady=0)
        self.saved.pack(side="right")
        rule = self.hairline(f, color=p["bg"])
        rule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(f, bg=p["bg"])
        box.grid(row=2, column=0, sticky="nsew")
        self.sc, self.ssb, inner = self._scroller(box, rule)
        # the cards start 12 px before E like the row highlights and end 4 px before the pane's
        # right edge, where the thumb's column is; their padding (`cpad`) puts the row labels
        # back on E and the controls on the header's right edge
        body = tk.Frame(inner, bg=p["bg"])
        body.pack(fill="x", padx=self.px(SP[0]))
        self._indicator(body)
        self._listening(body)
        self._history_group(body)

    def _group(self, parent, title, first=False) -> tk.Frame:
        """A settings group is a card; its title (meta, muted: a section label, not a second
        heading) sits above it on the ground, on the same left edge as the row labels."""
        tk.Label(parent, text=title, font=self.F["meta"], fg=self.pal["muted"], bg=self.pal["bg"],
                 bd=0, padx=0, pady=0).pack(
            anchor="w", padx=self.px(SP[2]), pady=(0 if first else self.px(SP[4]), self.px(SP[1])))
        card = self._card(parent)
        card.pack(fill="x")
        self.cards.append(card)
        body = tk.Frame(card.body, bg=card.body["bg"])
        body.pack(fill="x", pady=self.px(SP[0]))   # 12 (row) + 4 = the card's 16 px top padding
        return body

    def _indicator(self, parent) -> None:
        g = self._group(parent, "Indicator", first=True)
        r = _Row(self, g, "Style", "What the bar does while listening")
        self._segment(r.right, "style", [("Waveform", "waves"), ("Light", "light")],
                      self.cfg.get("indicator", "waves") or "waves", self._set_style)

        self.r_color = _Row(self, g, "Colour", "")
        self._colors(self.r_color, "color", "#e63c3c")
        self._style_desc()
        r = _Row(self, g, "Transcribing colour", "The pulse while text is being typed")
        self._colors(r, "color_busy", "#ffaa32")

        r = _Row(self, g, "Opacity", "Of the bar and its glow")
        self._slider(r.right, float(self.cfg.get("opacity", 0.9)))
        r = _Row(self, g, "Haze", "A wide soft glow around the bar")
        self._toggle(r.right, bool(self.cfg.get("haze", False)))

    def _style_desc(self) -> None:
        self.r_color.desc.configure(
            text="Recording and persistent mode" if self.cfg.get("indicator", "waves") != "light"
            else "The light while listening")
        self.r_color.desc.pack(anchor="w", pady=(self.px(SP[0]), 0))

    def _set_style(self, v) -> None:
        self.cfg["indicator"] = v
        self._style_desc()
        self.save()

    def _listening(self, parent) -> None:
        p = self.pal
        g = self._group(parent, "Listening")
        r = _Row(self, g, "Trigger key", "A headset button, media key or F13 toggles recording")
        # three widgets in one right-aligned group: packed straight into r.right they hugged the
        # LEFT edge of the control column and broke the one right edge every other row shares
        ground = r.right["bg"]
        grp = tk.Frame(r.right, bg=ground)
        grp.pack(side="right")
        # the chip keeps its fixed width in a frame that does not propagate: a Label sized by
        # its own image would grow when the text turns into "Press a key…" and shove the row
        chip = tk.Frame(grp, bg=ground, width=self.px(W_CHIP), height=self.px(24))
        chip.pack_propagate(False)
        self.l_trig = tk.Label(chip, text=vk_name(self.cfg.get("trigger_vk")), font=self.F["mono9"],
                               bg=ground, bd=0, highlightthickness=0, compound="center",
                               padx=0, pady=0,
                               fg=p["ink"] if self.cfg.get("trigger_vk") else p["muted"],
                               image=self.rr(self.px(W_CHIP), self.px(H_CHIP), self.px(6),
                                             p["hover"], ground, p["border"]))
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
    def _field(self, parent, w, h, r=None):
        """The rounded field a text control lives in: a Label whose image IS the field, with the
        control placed inside it. Inset by the RADIUS horizontally and by the hairline
        vertically (`.slot`), which is the whole trick - a square widget placed that far in can
        never cover a corner, and its inset is exactly the padding the text wanted anyway.

        A Canvas + create_window would look identical and be wrong: Tk unmaps a canvas's window
        items while the canvas is scrolled out of sight, so a field below the fold would drop
        out of the tab ring until someone scrolled to it."""
        ground, r = parent["bg"], r or self.px(R_CTL)
        fill = self.up(ground)
        box = tk.Label(parent, bd=0, highlightthickness=0, bg=ground, padx=0, pady=0)

        def paint(state="idle"):
            border = {"idle": self.pal["border_strong"], "focus": self.pal["ring"],
                      "error": self.pal["danger"]}[state]
            box.configure(image=self.rr(w, h, r, fill, ground, border,
                                        1 if state == "idle" else self.px(2)))
        box.paint, box.fill = paint, fill
        box.slot = lambda child: child.place(x=r, y=1, relwidth=1.0, width=-2 * r,
                                             relheight=1.0, height=-2)
        paint()
        return box

    def _entry(self, parent, value, width, font, commit, w=None, placeholder=None) -> tk.Entry:
        """A one-line field `width` characters wide - or `w` logical px. Empty and unfocused it
        shows `placeholder` in muted; `.value()` is "" then (`.get()` would hand it back)."""
        p = self.pal
        if w:
            w = self.px(w)
        else:
            probe = tk.Entry(parent, width=width, font=font)     # what this many characters measure
            w = probe.winfo_reqwidth() + 2 * self.px(SP[1])
            probe.destroy()
        box = self._field(parent, w, self.px(H_CTL))
        e = tk.Entry(box, width=width, font=font, bg=box.fill, fg=p["ink"], relief="flat", bd=0,
                     insertbackground=p["ink"], highlightthickness=0, justify="left",
                     selectbackground=p["selected"], selectforeground=p["ink"])
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

    def _segment(self, parent, key, options, value, on_pick) -> None:
        """One rounded container - a `hover` track, no hairline - with the picked option as an
        inner `selected` pill. Each option is a Label carrying its own SLICE of that one
        rendered container (see `segment_png`), so the group is still one control and one tab
        stop - the container takes focus (a 2 px `ring` on the track's edge, as a field's) and
        Left/Right move between the options. No separators: with one option always filled they
        would never be seen anyway."""
        p, ground = self.pal, parent["bg"]
        f = tk.Frame(parent, bg=ground, height=self.px(H_CTL), highlightthickness=0, takefocus=1)
        f.pack(side="right")
        f.pack_propagate(False)
        h, r = self.px(H_CTL), self.px(R_CTL)
        ws = tuple(self.mf["body"].measure(t) + 2 * self.px(SP[2]) for t, _ in options)
        cells, focus, hover = [], [False], [None]
        for i, (text, val) in enumerate(options):
            l = tk.Label(f, text=text, font=self.F["body"], cursor="hand2", bd=0, bg=ground,
                         compound="center", highlightthickness=0, fg=p["muted"], padx=0, pady=0)
            l.pack(side="left")
            cells.append((l, val))
        f.configure(width=sum(ws))

        def paint():
            fills = tuple(p["selected"] if v == value[0] else
                          p["active"] if i == hover[0] else None for i, (_, v) in enumerate(cells))
            border = p["ring"] if focus[0] else p["hover"]        # idle: the track's own colour
            bw = self.px(2) if focus[0] else 1
            for i, (l, val) in enumerate(cells):
                l.configure(fg=p["ink"] if val == value[0] else p["muted"],
                            image=self.img(("seg", ws, i, fills, ground, border, bw),
                                           lambda i=i, fills=fills: segment_png(
                                               ws, h, r, i, fills, ground, p["hover"], border, bw)))

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

        for i, (l, val) in enumerate(cells):
            l.bind("<Enter>", lambda e, i=i: hov(i))
            l.bind("<Leave>", lambda e: hov(None))
            l.bind("<Button-1>", lambda e, v=val: pick(v))
        step = lambda d: pick(cells[(next(i for i, c in enumerate(cells) if c[1] == value[0])
                                    + d) % len(cells)][1])
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
        box, d, ring = self.px(24), self.px(16), self.px(2)
        # one tab stop for the whole strip (nine would bury the hex field): Left/Right move a
        # cursor, Space/Return picks - so arrowing past a dot never writes cfg
        kb = [0, False]                                    # cursor index, strip has focus
        dots = tk.Frame(row.right, bg=ground, takefocus=1, highlightthickness=ring,
                        highlightbackground=ground, highlightcolor=p["ring"])
        dots.pack(side="left")

        def paint():
            for i, (l, hx) in enumerate(cells):
                on, at = hx == cur[0], kb[1] and i == kb[0]
                halo = p["ink"] if on else p["border_strong"] if at else None
                l.configure(image=self.img((hx, "sel" if on else "cur" if at else "off"),
                                           lambda hx=hx, halo=halo:
                                           dot_png(box, d, hx, halo, ring,
                                                   edge=p["border_strong"])))

        cells = []
        for hx in PRESETS:
            l = tk.Label(dots, bg=ground, cursor="hand2", bd=0)
            l.pack(side="left")
            cells.append((l, hx))
            l.bind("<Enter>", lambda e, l=l, hx=hx: hx != cur[0] and l.configure(
                image=self.img((hx, "hov"), lambda hx=hx: dot_png(box, d, hx, p["border_strong"], ring,
                                                                 edge=p["border_strong"]))))
            l.bind("<Leave>", lambda e: paint())
            l.bind("<Button-1>", lambda e, hx=hx: (dots.focus_set(), pick(hx)))

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
                                       p["border_strong"]))

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
        p, ring, ground = self.pal, self.px(2), parent["bg"]
        W, H = self.px(160) - 2 * ring, self.px(H_CTL) - 2 * ring   # the ring is inside the 160x32
        val = [round(value, 2)]
        lab = tk.Label(parent, text="", font=self.F["mono"], fg=p["muted"], bg=ground)
        lab.pack(side="right")
        c = tk.Canvas(parent, width=W, height=H, bg=ground, cursor="hand2", takefocus=1,
                      highlightthickness=ring, highlightbackground=ground, highlightcolor=p["ring"])
        c.pack(side="right", padx=(0, self.px(SP[2])))
        r = self.px(14) / 2
        track = c.create_line(r, H / 2, W - r, H / 2, fill=p["border_strong"], width=self.px(2))
        fill = c.create_line(r, H / 2, r, H / 2, fill=p["primary"], width=self.px(2))
        face = self.up(ground)          # the knob is a step above whatever it slides on
        knob = c.create_image(r, H / 2, image=self.img(
            ("knob", face), lambda: dot_png(self.px(18), self.px(14), face, p["border_strong"], 1, 0)))

        def paint():
            x = r + (val[0] - 0.2) / 0.8 * (W - 2 * r)
            c.coords(fill, r, H / 2, x, H / 2)
            c.coords(knob, x, H / 2)
            lab.configure(text=f"{val[0] * 100:.0f} %")

        def at(ev, commit):
            x = min(W - r, max(r, c.canvasx(ev.x)))   # canvasx: the focus ring offsets ev.x
            set_(round(0.2 + (x - r) / (W - 2 * r) * 0.8, 2), commit)

        def set_(v, commit):
            val[0] = min(1.0, max(0.2, round(v, 2)))
            paint()
            if commit:
                self.cfg["opacity"] = val[0]
                self.save()

        self.ctl["opacity"] = c
        c.bind("<Button-1>", lambda e: (c.focus_set(), at(e, False)))
        c.bind("<B1-Motion>", lambda e: at(e, False))
        c.bind("<ButtonRelease-1>", lambda e: at(e, True))
        c.bind("<Left>", lambda e: set_(val[0] - 0.01, True))     # keyboard: one step, committed
        c.bind("<Right>", lambda e: set_(val[0] + 0.01, True))
        paint()

    def _toggle(self, parent, value) -> None:
        p, w, h = self.pal, self.px(36), self.px(20)
        ground = parent["bg"]
        on = [bool(value)]
        l = tk.Label(parent, bg=ground, bd=0, cursor="hand2", takefocus=1,
                     highlightthickness=self.px(2), highlightbackground=ground,
                     highlightcolor=p["ring"])
        l.pack(side="right")

        def paint(hover=False):
            track = (p["primary_hover"] if hover else p["primary"]) if on[0] else \
                    (p["ring"] if hover else p["border_strong"])
            knob = p["on_primary"] if on[0] else self.up(ground)
            l.configure(image=self.img((track, knob, on[0]),
                                       lambda: pill_png(w, h, track, knob, on[0])))

        def toggle(*_):
            on[0] = not on[0]
            self.cfg["haze"] = on[0]
            paint(True)
            self.save()

        self.ctl["haze"] = l
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
        self.l_trig.configure(text="Press a key…", fg=self.pal["primary"])
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
            self.flash(self.saved, "Saved", 1500)

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
        lp = tk.Frame(f, bg=p["bg"], width=self.px(W_LIST[0]))
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
        lp.grid_rowconfigure(1, weight=1)
        lp.grid_columnconfigure(0, weight=1)
        head = tk.Frame(lp, bg=p["bg"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), pad))
        head.pack_propagate(False)
        # no Tk default border/padding on any Label in this view (2 + 1 px a side): the text
        # sits on E like the rows' and the buttons' glyphs
        tk.Label(head, text="Dictations", font=self.F["title"], fg=p["ink"], bg=p["bg"], bd=0, padx=0,
                 pady=0).pack(side="left")
        self.pcount = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"], bd=0,
                               padx=0, pady=0)
        self.pcount.pack(side="left", padx=(self.px(SP[2]), 0))
        box = tk.Frame(lp, bg=p["bg"])
        box.grid(row=1, column=0, sticky="nsew")
        self.plist = RowList(self, box, two_line=True, on_select=self._select, keys={
            "<Return>": self._plist_go, "<Double-Button-1>": self._plist_go,
            "<Control-c>": self.copy_selected})

        d = self.dpane = tk.Frame(f, bg=p["bg"])
        d.grid(row=0, column=2, sticky="nsew")
        d.grid_rowconfigure(0, weight=1)
        d.grid_columnconfigure(0, weight=1)
        d.bind("<Configure>", lambda e: self._layout_head())
        self._build_draft(d)
        self._build_sheet(d)

    def _build_draft(self, parent) -> None:
        """The draft frame: header row 1 (engine dot + name, [target segment], the primary),
        row 2 (the segment, when row 1 cannot hold it), the scroll-only rule, the body canvas."""
        p, pad = self.pal, self.px(SP[3])
        df = self.draft_f = tk.Frame(parent, bg=p["bg"])
        df.grid(row=0, column=0, sticky="nsew")
        df.grid_rowconfigure(2, weight=1)
        df.grid_columnconfigure(0, weight=1)
        head = self.head = tk.Frame(df, bg=p["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[1])))
        head.grid_columnconfigure(2, weight=1)
        eng = tk.Frame(head, bg=p["bg"])
        eng.grid(row=0, column=0, sticky="w")
        self.eng_dot = tk.Label(eng, bg=p["bg"], bd=0, padx=0, pady=0)
        self.eng_dot.pack(side="left")
        # the name's own 8 px pad is the gap after the dot
        self.b_eng = _Btn(self, eng, "Claude Code", self._open_sheet, kind="text", ink=True)
        self.b_eng.f.pack(side="left")
        self.seg_host = tk.Frame(head, bg=p["bg"])
        self.p_target = [self.cfg.get("prompt_target") or "code"]
        self.seg = self._segment(self.seg_host, "target", list(promptify.TARGETS), self.p_target[0],
                                 self._set_target)
        self.b_main = _Btn(self, head, "Promptify", self.promptify, kind="primary")
        self.b_main.f.grid(row=0, column=3, sticky="e")
        self.head_rows, self.main_shown = 0, True
        self.rule = self.hairline(df, color=p["bg"])
        self.rule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(df, bg=p["bg"])
        box.grid(row=2, column=0, sticky="nsew")
        self.pc, self.psb, self.p_inner = self._scroller(box, self.rule, cap=True)

    def _scroller(self, box, rule, cap=False) -> tuple:
        """The settings pattern: one inner Frame in one Canvas, so fields keep their place in the
        tab ring wherever they are scrolled to. `rule` is the hairline under the pane header: it
        shows only while the body is scrolled, so scrolled text clips right under it. `cap`
        holds the content to the body measure (an 80-character line) with air at the right.
        Returns (canvas, scrollbar, the frame to fill) - the frame has the body's 7/16 padding."""
        p, g = self.pal, self.px(SP[0])
        c = tk.Canvas(box, bg=p["bg"], highlightthickness=0, yscrollincrement=1, bd=0, width=1, height=1)
        c.pack(side="left", fill="both", expand=True)
        # the thumb's column: the outer 4 px of the pane's 16 px right padding, OVER the canvas
        # (a sibling placed on top of it), so the canvas never changes width when the thumb
        # comes and goes and the body's right edge stays the header's. The scrollbar is 2 px
        # wider than the column and 1 px left of it: the column clips clam's `bordercolor`
        # strips (`_ttk`) and the 4 px that show are all thumb.
        gut = tk.Frame(box, bg=p["bg"], width=g)
        gut.place(relx=1.0, y=0, anchor="ne", relheight=1.0)
        gut.lift()
        sb = ttk.Scrollbar(gut, orient="vertical", style="M.Vertical.TScrollbar", command=c.yview,
                           takefocus=0)
        sb.show = lambda on: (sb.place(x=-1, y=0, width=g + 2, relheight=1.0) if on else sb.place_forget())
        c.configure(yscrollcommand=lambda lo, hi: self._body_scrolled(sb, rule, lo, hi))
        gut.bind("<Enter>", lambda e: c.bind_all("<MouseWheel>", lambda ev: self._wheel(c, ev)))
        gut.bind("<Leave>", lambda e: c.unbind_all("<MouseWheel>"))
        win = tk.Frame(c, bg=p["bg"])
        wid = c.create_window((0, 0), window=win, anchor="nw")
        win.bind("<Configure>", lambda e: c.configure(scrollregion=c.bbox("all")))
        c.bind("<Configure>", lambda e: c.itemconfigure(
            wid, width=min(e.width, self.measure + 2 * self.px(SP[3])) if cap else e.width))
        c.bind("<Enter>", lambda e: c.bind_all("<MouseWheel>", lambda ev: self._wheel(c, ev)))
        c.bind("<Leave>", lambda e: c.unbind_all("<MouseWheel>"))
        inner = tk.Frame(win, bg=p["bg"])
        inner.pack(fill="both", expand=True, pady=(self.px(7), self.px(SP[3])))
        return c, sb, inner

    def _body_scrolled(self, sb, rule, lo, hi) -> None:
        sb.set(lo, hi)
        sb.show(not (float(lo) <= 0.0 and float(hi) >= 1.0))
        rule.configure(bg=self.pal["border"] if float(lo) > 0.0 else self.pal["bg"])

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
        if w <= 1 or self.pstate == "noengine":
            return
        # the parts, not their frames: a Label's request is known at once, a frame's aggregate
        # only after Tk's next idle pass - and no <Configure> follows a re-labelled engine name
        # to make good a decision taken on the stale one
        need = (self.eng_dot.winfo_reqwidth() + self.b_eng.w + 2 * self.px(SP[2])
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
        no process and no network."""
        spec = promptify.engine_spec(self.cfg)
        return spec["key"], spec, connect.status(spec["key"])[0]

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
        col = p["danger"] if self.pstate == "error" else p["primary"] if est == "connected" else None
        self.eng_dot.configure(image=self.dot(8, 6, col) if col else
                               self.dot(8, 6, p["bg"], edge=p["border_strong"]))
        self.b_eng.text(spec["label"] if named else "No engine", hold=False)
        self.b_eng.mute(not named)

    def _clear_body(self) -> None:
        for w in self.p_inner.winfo_children():
            w.destroy()
        self.p_inner.unbind("<Configure>")       # the wrap bindings of the labels just destroyed
        self.p_fields = {"prompts": [], "questions": [], "answers": [], "chips": [], "cur": 0,
                         "text": None}

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
        self._line(self.p_inner, "Pick another engine above, or try again.", "meta",
                   pady=(self.px(SP[0]), 0))

    def _body_drafting(self, it) -> None:
        """ONE line: the engine and the seconds (the ticker in `_run_draft` keeps them going),
        and Cancel. Header and list do not move."""
        p, label = self.pal, promptify.engine_spec(self.cfg)["label"]
        row = tk.Frame(self.p_inner, bg=p["bg"])
        row.pack(fill="x", padx=self.px(SP[3]))
        self.l_draft = tk.Label(row, text=f"Drafting with {label} · 0 s", font=self.F["body"],
                                fg=p["muted"], bg=p["bg"], bd=0, padx=0, pady=0)
        self.l_draft.pack(side="left")
        self.b_cancel = _Btn(self, row, "Cancel", self._cancel_draft, kind="text")
        self.b_cancel.f.pack(side="left", padx=(self.px(SP[2]) - self.px(SP[1]), 0))

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
        orow = tk.Frame(inner, bg=p["bg"])
        orow.pack(fill="x", padx=(pad - px(SP[1]), pad))     # the glyph lands on E
        self.orig_open = False
        self.b_orig = _Btn(self, orow, "Original ›", self._toggle_orig, kind="text",
                           font=self.F["meta"], h=H_CHIP, r=6)
        self.b_orig.f.pack(side="left")
        if len(F["prompts"]) > 1:          # the dictation split into independent asks
            self._segment(orow, "which", [(f"{i + 1} of {len(F['prompts'])}", i)
                                          for i in range(len(F["prompts"]))], 0, self._switch_prompt)
        self.l_orig = tk.Label(inner, text=it["text"], font=self.F["body"], fg=p["muted"],
                               bg=p["bg"], anchor="w", justify="left", bd=0, padx=0, pady=0)
        self._wrap(self.l_orig, 2 * pad)
        F["text"] = t = self._textbox(inner, F["prompts"][0], (4, 200))
        t.host.pack(fill="x", padx=(pad - px(2), pad), pady=(px(SP[1]), 0))   # the ring bar before E
        if d.get("notes"):
            self._line(inner, "Notes · " + d["notes"], "meta", pady=(px(SP[2]), 0))
        qs = F["questions"]
        self.hairline(inner).pack(fill="x", padx=pad, pady=(px(SP[4]), 0))
        self._line(inner, f"Questions · {len(qs)}" if qs else
                   "No questions · the dictation was specific enough.", "meta", pady=(pad, 0))
        for i, q in enumerate(qs):
            blk = tk.Frame(inner, bg=p["bg"])
            blk.pack(fill="x", padx=pad, pady=(px(SP[2]) if i == 0 else pad, 0))
            l = tk.Label(blk, text=q["q"], font=self.F["body"], fg=p["ink"], bg=p["bg"],
                         anchor="w", justify="left", bd=0, padx=0, pady=0)
            l.pack(fill="x")
            self._wrap(l, 0)
            if q.get("why"):
                w = tk.Label(blk, text=q["why"], font=self.F["meta"], fg=p["muted"], bg=p["bg"],
                             anchor="w", justify="left", bd=0, padx=0, pady=0)
                w.pack(fill="x")
                self._wrap(w, 0)
            # the strip is made before the answer so Tab reaches the chips first
            strip = tk.Frame(blk, bg=p["bg"], takefocus=1, highlightthickness=0,
                             height=px(H_CHIP)) if q.get("options") else None
            a = self._uline(blk, "", (1, 3), "Type an answer, or leave it blank to skip")
            if strip is not None:
                self._chips(strip, q["options"], a)
                strip.pack(fill="x", pady=(px(SP[1]), 0))
            a.host.pack(fill="x", pady=(px(SP[1]), 0))
            F["answers"].append(a)
            F["chips"].append(strip)
        meta = tk.Frame(inner, bg=p["bg"])
        meta.pack(anchor="w", padx=pad, pady=(px(SP[4]), 0))
        label = promptify.ENGINES.get(d.get("engine"), {}).get("label", d.get("engine", ""))
        for text, font in ((f"Drafted by {label} · ", "meta"), (d.get("model") or "", "mono9"),
                           (f" · {d.get('wall', 0):.0f} s", "meta")):
            tk.Label(meta, text=text, font=self.F[font], fg=p["muted"], bg=p["bg"], bd=0, padx=0,
                     pady=0).pack(side="left")
        self.b_update = _Btn(self, inner, "Update prompt", self._update, kind="text")
        self.b_update.f.pack(anchor="w", padx=(pad - px(SP[1]), 0), pady=(px(SP[1]), 0))

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
        `border` over the ground; focused, both `ring` - the band's height never changes, so
        focusing shifts nothing. Empty and unfocused it shows `placeholder` in muted under the
        tag "ph", and `.value()` is "" then. Grows with its content between `lines`; a
        one-line field commits on Return. Tab moves on; Ctrl+Return is Update prompt. `.host`
        is the frame to pack; `.set(s)` writes it; `.changed` is told after every edit."""
        p, ground = self.pal, parent["bg"]
        host = tk.Frame(parent, bg=ground)
        t = tk.Text(host, height=lines[0], wrap="word", relief="flat", bd=0, highlightthickness=0,
                    bg=ground, fg=p["ink"], font=font or self.mf["body"], insertbackground=p["ink"],
                    selectbackground=p["selected"], selectforeground=p["ink"], undo=True,
                    padx=0, pady=0)
        t.pack(fill="x", pady=(self.px(7), self.px(6)))
        band = [tk.Frame(host, bg=p["border"], height=1), tk.Frame(host, bg=ground, height=1)]
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
                b.configure(bg=p["ring"] if on else (p["border"] if b is band[0] else ground))
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
                    selectbackground=p["selected"], selectforeground=p["ink"], undo=True,
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
        ok, why = promptify.available(self.cfg)
        if not ok:
            self._show("noengine")
            return
        if not self.cfg.get("prompt_ack"):
            self._show("ack")
        else:
            self._run_draft(it)

    def _ack_go(self) -> None:
        self.cfg["prompt_ack"] = True
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
                res["ok"] = promptify.draft(self.cfg, it["text"], self.p_target[0], prompts, questions,
                                            answers, cancel=cancel, workdir=self.history.path.parent)
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
        if self.view == "promptify" and self.sel is it:
            self._show("error")

    def _draft_done(self, it, res) -> None:
        if not (self.win and self.win.winfo_exists()):
            return
        self._stop_draft()
        it["draft"] = res                   # the draft lives on the History entry, on disk
        self.history.save()
        self.eng_err.pop(res.get("engine"), None)
        self.list.draw()                    # the dot
        self.plist.draw()
        if self.view == "promptify" and self.sel is it:
            self._show("draft")
            self.b_main.f.focus_set()       # ... which now says Copy prompt

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
        ok, why = promptify.available(self.cfg)
        if not ok:
            self._show("noengine")
            return
        self._save_edits()
        F = self.p_fields
        answers = [a.value() for a in F["answers"] if a.winfo_exists()]
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
        """Esc: the sheet closes; a draft in progress is cancelled; a field - or anything in the
        Promptify detail pane, chip strips and header buttons included - hands focus back to
        the view's list; the Clear confirm is kept."""
        if self.view == "promptify":
            if self.sheet_open:
                self._close_sheet()
                return
            if self.drafting is not None and self.drafting is self.sel:
                self._cancel_draft()
                return
        f, lst = self.win.focus_get(), self._list()
        if lst is not None and f is not None and (isinstance(f, (tk.Entry, tk.Text, ttk.Combobox))
                                                  or str(f).startswith(str(self.dpane) + ".")):
            lst.focus_set()
            return
        self._clear_cancel()

    # --- the engines sheet -------------------------------------------------------------------
    def _build_sheet(self, parent) -> None:
        """Swapped in over the draft frame (grid / grid_remove; the list pane stays). Four rows,
        one per engine, rebuilt from the engine table on every change of state
        (`_engine_rows`). No key fields anywhere: each engine signs in on its own, in the
        browser, and murmur only ever reads whether it did (`connect.status`)."""
        p, pad = self.pal, self.px(SP[3])
        s = self.sheet = tk.Frame(parent, bg=p["bg"])
        s.grid(row=0, column=0, sticky="nsew")
        s.grid_remove()
        self.sheet_open, self.e_rows = False, {}
        s.grid_rowconfigure(2, weight=1)
        s.grid_columnconfigure(0, weight=1)
        head = tk.Frame(s, bg=p["bg"], height=self.px(H_CTL))
        head.grid(row=0, column=0, sticky="ew", padx=(pad - self.px(SP[1]), pad),
                  pady=(self.px(PAD_TOP), self.px(SP[1])))
        head.pack_propagate(False)
        self.b_back = _Btn(self, head, "‹ Draft", self._close_sheet, kind="text")
        self.b_back.f.pack(side="left")
        tk.Label(head, text="Engines", font=self.F["title"], fg=p["ink"], bg=p["bg"], bd=0, padx=0,
                 pady=0).pack(side="left", padx=(self.px(SP[0]), 0))   # the button's 8 + 4 = 12
        self.srule = self.hairline(s, color=p["bg"])
        self.srule.grid(row=1, column=0, sticky="ew")
        box = tk.Frame(s, bg=p["bg"])
        box.grid(row=2, column=0, sticky="nsew")
        self.ec, self.esb, self.e_inner = self._scroller(box, self.srule)

    def _open_sheet(self) -> None:
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
            return line + spec["login"], p["danger"] if err else p["primary"], "active" if active else "use"
        if err:                                       # a sign-in that failed says why
            return f"Error · {err}", p["danger"], "connect"
        return f"{self.WORDS.get(state, 'Not connected')} · {spec['login']}", None, "connect"

    def _engine_rows(self) -> None:
        """The sheet's body, rebuilt from the engine table: intro, one block per engine (name;
        dot + state · meaning; the action at the right; the Model field under the active one),
        the disclosure. `e_rows[key]` holds each row's parts for the tests."""
        p, px, pad, inner = self.pal, self.px, self.px(SP[3]), self.e_inner
        for w in inner.winfo_children():
            w.destroy()
        inner.unbind("<Configure>")               # the wrap bindings of the labels just destroyed
        self.e_rows = {}
        self._line(inner, "The engine writes the prompt. Each signs in on its own; murmur keeps "
                   "no keys.", "meta")
        rows = tk.Frame(inner, bg=p["bg"])
        rows.pack(fill="x", padx=pad, pady=(pad, 0))
        for i, key in enumerate(promptify.ORDER):
            spec = promptify.ENGINES[key]
            status, col, action = self._engine_row(key)
            if i:
                self.hairline(rows).pack(fill="x")
            blk = tk.Frame(rows, bg=p["bg"])
            blk.pack(fill="x", pady=px(SP[1]))
            top = tk.Frame(blk, bg=p["bg"])
            top.pack(fill="x")
            # the action column keeps its width and centres its control on the two text lines
            right = tk.Frame(top, bg=p["bg"], width=px(W_ACT))
            right.pack(side="right", fill="y")
            right.pack_propagate(False)
            left = tk.Frame(top, bg=p["bg"])
            left.pack(side="left", fill="x", expand=True, padx=(0, px(SP[2])))
            tk.Label(left, text=spec["label"], font=self.F["body"], fg=p["ink"], bg=p["bg"], bd=0,
                     padx=0, pady=0).pack(anchor="w")
            st = tk.Frame(left, bg=p["bg"])
            st.pack(fill="x", pady=(px(2), 0))
            tk.Label(st, bg=p["bg"], bd=0, padx=0, pady=0,
                     image=self.dot(8, 6, col) if col else self.dot(8, 6, p["bg"], edge=p["border_strong"])
                     ).pack(side="left", anchor="n", pady=(px(SP[0]), 0))
            sl = tk.Label(st, text=status, font=self.F["meta"], fg=p["muted"], bg=p["bg"],
                          anchor="w", justify="left", bd=0, padx=0, pady=0)
            sl.pack(side="left", fill="x", expand=True, padx=(px(SP[1]), 0))
            self._wrap(sl, px(SP[3]))
            col_ = tk.Frame(right, bg=p["bg"])
            col_.place(relx=1.0, rely=0.5, anchor="e")
            row = self.e_rows[key] = {"status": sl, "action": action, "btn": None}
            if action == "active":
                tk.Label(col_, text="Active", font=self.F["meta"], fg=p["ink"], bg=p["bg"], bd=0,
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
            right.configure(width=max([px(W_ACT)] + [c.winfo_reqwidth() for c in col_.winfo_children()]))
            login = self.logins.get(key)
            if login and login["url"] and key == "claude":
                row["code"] = self._paste_code(blk, login["login"])
            if action == "active":
                row["model"] = self._model_line(blk, key)
        self._line(inner, "Connect opens the engine’s own sign-in in your browser. A dictation is "
                   "sent only to the engine you pick, only when you press Promptify.", "meta",
                   pady=(px(SP[4]), 0))

    def _model_line(self, blk, key) -> tk.Text:
        """Model (active engine only): an underlined mono field, 240 wide or what is left,
        blank = the engine's default; writes cfg["prompt_models"][key]."""
        p, px = self.pal, self.px
        ml = tk.Frame(blk, bg=p["bg"])
        ml.pack(fill="x", pady=(px(SP[1]), 0))
        lab = tk.Label(ml, text="Model", font=self.F["meta"], fg=p["muted"], bg=p["bg"], bd=0, padx=0,
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
        pl = tk.Frame(blk, bg=p["bg"])
        pl.pack(fill="x", pady=(px(SP[1]), 0))
        tk.Label(pl, text="Paste code", font=self.F["meta"], fg=p["muted"], bg=p["bg"], bd=0, padx=0,
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
        self.save()
        self._engine_rows()
        self._refresh_engine()

    def _disconnect_openrouter(self) -> None:
        connect.forget_openrouter()
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
            self.sel = items[-1] if items else None
        n, days = len(items), self.cfg.get("retention_days", 0)
        kept = "history is off" if not days else f"kept {days:g} days"
        self.count.configure(text=f"{'no' if not n else n} dictation{'s' if n != 1 else ''} · {kept}")
        self.pcount.configure(text=str(n))
        self._clear_cancel()
        self.b_clear.f.pack(side="right") if n else self.b_clear.f.pack_forget()
        self.card.grid() if n else self.card.grid_remove()
        self.list.draw()
        self.plist.draw()
        self._select(self.sel)
