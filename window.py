"""The app window: transcript history plus settings. Hidden until opened from the tray.

One Toplevel, a sidebar and two views. The look is the brand green run through an OKLCH 12-step
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
PAD_TOP = 20                        # ... except the header's top pad: 24 sits too low under the
LH = 3                              # title bar, and the Text line lead that makes ~1.5 line height
R_CTL = 8                           # radius family: controls (button, field, segment, chip,
R_CARD = 12                         # nav pill, row highlight) and grouped cards. Inner radius
R_IN = 2                            # = outer - inset, clamped at 0: the segment's selected cell
H_CTL = 32                          # control height
H_ROW = 36                          # history row
H_CHIP = 20                         # "restart to apply" chip
H_MARK = 24                         # sidebar lockup: the mark's IMAGE box (its ink is 40/64 of
GAP_MARK = 16                       # that, ~1.9x the wordmark's x-height) and the air after it.
                                    # Both are up from the 20/10 first cut: at that size the mark
                                    # read as a letter and the sidebar said "m murmur"
W_CHIP = 88                         # trigger-key chip: fits "Previous Track" at 9 pt
W_SIDE = 168                        # sidebar
W_COL = 280                         # settings control column
W_COL_WIDE = 352                    # ... for the two colour rows (9 dots + swatch + hex entry)
W_TIME = 104                        # history time column
WIN = (780, 560)
WIN_MIN = (600, 400)

# --- domain ---------------------------------------------------------------------------------

MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"]
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
           ground: str = None, bar: tuple = None) -> bytes:
    """A rounded rectangle with an optional hairline - the shape a Tk widget cannot have, drawn
    at SS and reduced so the arc is anti-aliased. `ground` is the colour BEHIND the corners: pass
    it and the PNG is opaque (what a canvas item wants); leave it out and the corners are
    transparent for Tk to composite against a Label's own bg. `bar` = (x, w, h, radius, colour):
    an accent pill drawn inside, vertically centred (the selected history row keeps its bar)."""
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
    it - no motion, no weight or size shift. The focus ring is drawn on the button's own edge
    (2 px of `ring`) instead of around it, so focusing never moves anything."""

    def __init__(self, ui, parent, text, cmd, kind="ghost", font=None, h=None, pad=None):
        p, self.ui, self.cmd, self.kind = ui.pal, ui, cmd, kind
        self.ground = parent["bg"]
        self.fill, self.fg, self.hov = {
            "primary": (p["primary"], p["on_primary"], p["primary_hover"]),
            "danger": (p["danger"], p["on_danger"], p["danger_hover"]),
            "ghost": (self.ground, p["muted"], p["hover"]),
        }[kind]
        self.down = p["active"] if kind == "ghost" else self.hov
        self.edge = p["border_strong"] if kind == "ghost" else None
        self.h, self.pad, self.cur, self.ring = ui.px(h or H_CTL), 12 if pad is None else pad, \
            self.fill, False
        self.f = tk.Label(parent, text=text, font=font or ui.F["body"], fg=self.fg, bd=0,
                          bg=self.ground, compound="center", highlightthickness=0, takefocus=1,
                          cursor="hand2", padx=0, pady=0)   # Tk's default 1 px pad would show
        self._size()
        self._paint(self.fill)
        self.f.bind("<Enter>", lambda e: self._paint(self.hov))
        self.f.bind("<Leave>", lambda e: self._paint(self.fill))
        self.f.bind("<Button-1>", lambda e: self._paint(self.down))
        self.f.bind("<ButtonRelease-1>", self._release)
        self.f.bind("<FocusIn>", lambda e: self._ring(True))
        self.f.bind("<FocusOut>", lambda e: self._ring(False))
        for k in ("<Return>", "<space>"):
            self.f.bind(k, lambda e: self.cmd())

    def _size(self) -> None:
        self.f.configure(image="")                     # measure the TEXT, not the image behind it
        self.w = self.f.winfo_reqwidth() + 2 * self.ui.px(self.pad)

    def _paint(self, fill) -> None:
        self.cur = fill
        border, bw = (self.ui.pal["ring"], 2) if self.ring else (self.edge, 1)
        self.f.configure(image=self.ui.rr(self.w, self.h, self.ui.px(R_CTL), fill, self.ground,
                                          border, self.ui.px(bw) if self.ring else 1))

    def _ring(self, on) -> None:
        self.ring = on
        self._paint(self.cur)

    def _release(self, e) -> None:
        self._paint(self.hov)
        self.f.focus_set()
        if 0 <= e.x < self.f.winfo_width() and 0 <= e.y < self.f.winfo_height():
            self.cmd()

    def text(self, s) -> None:
        self.f.configure(text=s)
        self._size()
        self._paint(self.cur)


class _Row:
    """A settings row inside a group card: label (+ description, + restart chip) left, the
    control right-aligned in a fixed column. Rows are separated by an INSET hairline - from the
    label's left edge to the right padding, in a lighter step than the card's own outline, so
    the card reads as one object instead of a stack of full-width rules."""

    def __init__(self, ui, group, label, desc=None, col=W_COL):
        p, bg = ui.pal, group["bg"]
        self.rule = ui.hairline(group, color=p["s5"]) if group.winfo_children() else None
        if self.rule is not None:
            self.rule.pack(fill="x", padx=ui.cpad)
        self.ui, self.shown = ui, True
        self.frame = tk.Frame(group, bg=bg)
        self.frame.pack(fill="x")
        main = tk.Frame(self.frame, bg=bg)
        main.pack(fill="x", padx=ui.cpad)
        self.right = tk.Frame(main, bg=bg, width=ui.px(col), height=ui.px(H_CTL))
        self.right.pack(side="right", pady=ui.px(SP[2]))   # the control column keeps its width;
        self.right.pack_propagate(False)                   # the label column takes what is left
        left = tk.Frame(main, bg=bg)
        left.pack(side="left", fill="x", expand=True, pady=ui.px(SP[2]))
        self.head = tk.Frame(left, bg=bg)
        self.head.pack(anchor="w")
        tk.Label(self.head, text=label, font=ui.F["body"], fg=p["ink"], bg=bg).pack(side="left")
        self.chip = None
        self.desc = tk.Label(left, text=desc or "", font=ui.F["meta"], fg=p["muted"], bg=bg,
                             anchor="w", justify="left")
        left.bind("<Configure>",     # wrap rather than run under the control when narrow
                  lambda e: self.desc.configure(wraplength=max(e.width, ui.px(120))))
        if desc:
            self.desc.pack(anchor="w", pady=(ui.px(SP[0]), 0))
        self.err = tk.Label(self.frame, text="", font=ui.F["meta"], fg=p["danger_text"], bg=bg,
                            anchor="e")

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
        """Model / microphone / language changed: say so, and keep saying it."""
        if self.chip is None:
            ui, bg = self.ui, self.head["bg"]
            # accent text on a tint of the same hue - a grey label on a grey pill is the
            # generic-chip tell, and this one has no border to save it. A 20 px status chip is
            # the one thing in the radius family that wants to be a full pill, not R_CTL.
            h = ui.px(H_CHIP)
            self.chip = tk.Label(self.head, text="restart to apply", font=ui.F["meta"], bd=0,
                                 bg=bg, fg=ui.pal["primary"], compound="center",
                                 highlightthickness=0, padx=0, pady=0)
            w = self.chip.winfo_reqwidth() + 2 * ui.px(SP[1])
            self.chip.configure(image=ui.rr(w, h, h // 2, ui.up(bg), bg))
            self.chip.pack(side="left", padx=(ui.px(SP[1]), 0))

    def error(self, msg) -> None:
        self.err.configure(text=msg or "")
        (self.err.pack(fill="x", padx=self.ui.cpad, pady=(0, self.ui.px(SP[1])))
         if msg else self.err.pack_forget())


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
        self.hover = None
        self.imgs = {}            # Tk drops an image nobody references
        self.jobs = {}            # named after() ids, so a second flash cancels the first
        self.rows = []            # (item, y) per drawn row
        self.hl = {}              # the two row-highlight images, keyed by the width they fit
        self.hl_id = {}           # ... and their canvas items
        self.ctl = {}             # the settings controls, by cfg key (the tests drive these)
        self.panel_open = False   # the Promptify panel is showing in the list's place
        self.foot_links = []      # sidebar footer links, one per entry in `links`
        self.cards = []           # the settings group cards, top to bottom
        self.undo = None          # (index, item) while the undo offer stands

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
        self.F = {"title": ("Segoe UI Semibold", 12),
                  "body6": ("Segoe UI Semibold", 10), "body": ("Segoe UI", 10),
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
        # a card is inset SP[1] from the content edge and spends 1 px on its hairline, so its
        # padding is 16 minus that px - which puts every line of text back on the same 24 grid
        # as the view headers, cards or no cards
        self.cpad = self.px(SP[3]) - 1
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
        for name, build in (("history", self._build_history), ("settings", self._build_settings)):
            f = tk.Frame(content, bg=p["bg"])
            f.grid(row=0, column=0, sticky="nsew")
            self.views[name] = f
            build(f)
        w.bind("<Escape>", lambda e: self._escape())
        w.bind("<Up>", lambda e: self._nav_key(-1))
        w.bind("<Down>", lambda e: self._nav_key(1))
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
        st.layout("M.Vertical.TScrollbar",     # no arrows: trough + thumb, 8 px wide
                  [("Vertical.Scrollbar.trough",
                    {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                                   {"expand": "1", "sticky": "nswe"})]})])
        st.configure("M.Vertical.TScrollbar", troughcolor=p["bg"], background=p["border"],
                     bordercolor=p["bg"], darkcolor=p["border"], lightcolor=p["border"],
                     width=self.px(SP[1]))
        st.map("M.Vertical.TScrollbar", background=[("active", p["border_strong"])])

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

    def rr(self, w, h, r, fill, ground, border=None, bw=1, bar=None) -> tk.PhotoImage:
        """Cached rounded rect, sizes in device px. Every control's corner comes from here."""
        return self.img(("rr", w, h, r, fill, ground, border, bw, bar),
                        lambda: rr_png(w, h, r, fill, border, bw, ground, bar))

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
        # where the mark overshoots the word the block grows upward: give that height straight
        # back out of the top pad, so the wordmark's baseline stays where the view titles' is
        lock.pack(anchor="w", padx=self.px(SP[3]),
                  pady=(self.px(SP[4]) - lock.rise, self.px(SP[4])))
        self.nav = {}
        for name, label in (("history", "History"), ("settings", "Settings")):
            # the pill IS the row's image; the text sits on a Label placed inside it, inset by
            # the radius so its square background can never eat a rounded corner
            row = tk.Label(s, bd=0, highlightthickness=0, bg=p["surface"], cursor="hand2",
                           padx=0, pady=0)
            row.pack(padx=self.px(SP[1]))
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
        foot.pack(side="bottom", anchor="w", padx=self.px(SP[3]), pady=self.px(SP[4]))
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
        edge at x=0 puts it on the same left edge as the nav labels below."""
        p, f = self.pal, self.mf["title"]
        m = self.mark(self.px(H_MARK), p["muted"])
        desc = f.metrics("descent")
        h = max(f.metrics("linespace"), m.height() + desc)   # the taller of word and mark sets it
        base = h - desc
        x = m.width() + self.px(GAP_MARK)
        c = tk.Canvas(parent, bg=p["surface"], bd=0, highlightthickness=0,
                      width=x + f.measure("murmur"), height=h)
        c.create_image(0, base, anchor="sw", image=m)
        c.create_text(x, base + desc, anchor="sw", text="murmur", font=f, fill=p["muted"])
        c.rise = h - f.metrics("linespace")               # how far the mark overshoots the word
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
            self._nav_paint(n, p["active"] if sel else p["surface"])
            l.configure(fg=p["ink"] if sel else p["muted"])
        for n, v in self.views.items():        # not tkraise: an unmapped frame is skipped by
            v.grid() if n == name else v.grid_remove()   # tk_focusNext, a raised-over one is not

    # --- history view ------------------------------------------------------------------------
    def _build_history(self, f) -> None:
        p, pad = self.pal, self.px(SP[4])
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        head = tk.Frame(f, bg=p["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[4])))
        tk.Label(head, text="History", font=self.F["title"], fg=p["ink"], bg=p["bg"]).pack(side="left")
        self.count = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"])
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
        # the focus ring is NOT the canvas's own: a hard accent rectangle around a list of
        # rounded highlights is the one rigid line left in the view. It moves onto the selected
        # row instead (see `_draw_rows`), which is also the row the keyboard is about to move.
        self.list = tk.Canvas(box, bg=p["bg"], highlightthickness=self.px(2),
                              highlightbackground=p["bg"], highlightcolor=p["bg"],
                              takefocus=1, yscrollincrement=1)
        self.sb = ttk.Scrollbar(box, orient="vertical", style="M.Vertical.TScrollbar",
                                command=self.list.yview)
        self.list.configure(yscrollcommand=self._scrolled)
        self.list.pack(side="left", fill="both", expand=True)
        self.list.bind("<Configure>", lambda e: self._draw_rows())
        self.list.bind("<FocusIn>", lambda e: self._paint_rows())
        self.list.bind("<FocusOut>", lambda e: self._paint_rows())
        self.list.bind("<Motion>", self._row_hover)
        self.list.bind("<Leave>", lambda e: self._set_hover(None))
        self.list.bind("<Button-1>", self._row_click)
        self.list.bind("<Double-Button-1>", lambda e: self.copy_selected())
        self.list.bind("<Enter>", lambda e: self.list.bind_all("<MouseWheel>", self._wheel_list))
        self.list.bind("<Leave>", lambda e: self.list.unbind_all("<MouseWheel>"), add="+")
        for k, fn in (("<Up>", lambda e: self._move(-1)), ("<Down>", lambda e: self._move(1)),
                      ("<Return>", lambda e: self.copy_selected()),
                      ("<Control-c>", lambda e: self.copy_selected()),
                      ("<Control-d>", lambda e: self.promptify()),
                      ("<Delete>", lambda e: self.delete_selected())):
            self.list.bind(k, fn)

        self.dmax = 5                 # a short window gives the list the room, not the panel
        f.bind("<Configure>", self._fit_detail)
        # the selected transcript and its two actions are ONE grouped surface, inset to the same
        # 8 px as the row highlights above it, and its text lands on the same 24 px as theirs
        self.card = self._card(f)
        self.card.grid(row=2, column=0, sticky="ew", padx=self.px(SP[1]),
                       pady=(self.px(SP[1]), self.px(SP[4])))
        card = self.card.body
        self.detail = tk.Text(card, height=5, width=80, wrap="word", relief="flat", bd=0,
                              highlightthickness=0, bg=p["surface"], fg=p["ink"],
                              font=self.mf["body"],
                              spacing1=self.px(LH), spacing3=self.px(LH), state="disabled",
                              cursor="xterm", selectbackground=p["selected"],
                              selectforeground=p["ink"], inactiveselectbackground=p["selected"])
        # anchor "w", no fill: stretched, the width request is ignored and a maximised window
        # gives the transcript ~200-character lines - measure has to stay near 80
        self.detail.pack(anchor="w", padx=self.cpad, pady=(self.cpad, 0))

        act = self.act = tk.Frame(card, bg=p["surface"])
        act.pack(fill="x", padx=self.cpad, pady=(self.px(SP[2]), self.cpad))
        self.b_copy = _Btn(self, act, "Copy", self.copy_selected, kind="primary")
        self.b_copy.f.pack(side="left")
        self.b_prompt = _Btn(self, act, "Promptify", self.promptify)
        self.b_prompt.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.b_del = _Btn(self, act, "Delete", self.delete_selected)
        self.b_del.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.status = tk.Frame(act, bg=p["surface"])
        self.status.pack(side="right", pady=self.px(SP[1]))
        self.s_text = tk.Label(self.status, text="", font=self.F["body"], fg=p["muted"],
                               bg=p["surface"])
        self.s_text.pack(side="left")
        # underlined so the one clickable word in the status line does not read as more meta
        self.s_undo = self.link(self.status, "Undo", self._undo, font=self.F["body"] + ("underline",))
        self._build_panel(f)

    def _scrolled(self, lo, hi) -> None:
        self.sb.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.sb.pack_forget()
        else:
            self.sb.pack(side="right", fill="y")

    def _visible(self) -> bool:
        """The wheel handlers hang off the root's "all" tag: refuse to act once this window is
        gone or withdrawn, whatever else in the process owns the pointer."""
        return bool(self.win and self.win.winfo_exists() and self.win.winfo_viewable())

    def _wheel_list(self, e) -> None:
        if self._visible():
            self.list.yview_scroll(int(-e.delta / 120) * 3 * self.px(H_ROW), "units")

    def _fit_detail(self, e) -> None:
        """The panel is measured in characters, not pixels: cap it at 80, and never let it ask
        for more than the frame can show (600 px window -> about 54)."""
        ch = max(1, self.mf["body"].measure("n"))
        self.detail.configure(width=max(40, min(80, (e.width - 2 * self.px(SP[4])) // ch)))
        self.dmax = 5 if e.height >= self.px(480) else 3
        self._fit_height()

    def _fit_height(self) -> None:
        """Height follows the transcript. A two-line dictation used to hold open a five-line
        panel, which cost the list - the view's actual content - a third of its rows."""
        if getattr(self, "_fitting", False):
            return
        self._fitting = True             # configure -> relayout -> configure, one level deep
        try:
            self.detail.update_idletasks()
            n = self.detail.count("1.0", "end", "displaylines") or (1,)
            h = max(2, min(self.dmax, n[0]))
            if int(self.detail.cget("height")) != h:
                self.detail.configure(height=h)
        finally:
            self._fitting = False

    def _draw_rows(self) -> None:
        """Rows are canvas items, not widgets: 45 of them as Frames would be slow and would
        each need their own hover bindings.

        Hover and selection are not per-row rectangles any more but TWO rounded images that get
        moved to the row they belong to - a rounded highlight would otherwise cost three
        rectangles and four corner masks per row, and only two rows are ever lit.

        Ceiling: this redraws EVERY row on every <Configure>, ~7 font.measure calls each. Fine
        to about 2 000 items (a year at 5 dictations a day); past that the upgrade path is to
        draw only the slice between canvasy(0) and canvasy(0)+height, and redraw on scroll too.
        """
        c, p = self.list, self.pal
        c.delete("all")
        self.rows = []
        items = self.history.items
        w, h = c.winfo_width(), self.px(H_ROW)
        x0 = self.px(SP[4]) - self.px(2)          # the canvas border inset: keep one left edge
        if not items:
            self.hl_id = {}       # the highlights went with delete("all"): nothing to move
            self._empty(c, w)
            c.configure(scrollregion=(0, 0, 0, 0))
            return
        # the highlight is inset 8 px from the list's sides, like the card below it; the text
        # keeps its 24 px edge, which is 16 px of padding inside the highlight
        hx = self.px(SP[1]) - self.px(2)
        hw = max(self.px(H_ROW), w - 2 * hx)   # w is 1 until Tk has laid the canvas out
        if self.hl.get("w") != hw:
            bar = (self.px(SP[1]) - self.px(2), self.px(3), self.px(16), self.px(2), p["primary"])
            png = lambda **kw: tk.PhotoImage(data=base64.b64encode(rr_png(
                hw, h, self.px(R_CTL), ground=p["bg"], **kw)).decode())
            # not in the img() cache: these are as wide as the window and would pile up a copy
            # per pixel of a resize drag
            self.hl = {"w": hw, "hover": png(fill=p["hover"]),
                       "sel": png(fill=p["active"], bar=bar),
                       "focus": png(fill=p["active"], bar=bar, border=p["ring"],
                                    border_w=self.px(2))}
        self.hl_id = {k: c.create_image(hx, 0, anchor="nw", image=self.hl[k], state="hidden")
                      for k in ("hover", "sel")}     # created first: the row text draws over them
        for i, idx in enumerate(range(len(items) - 1, -1, -1)):
            it, y = items[idx], i * h
            c.create_text(x0, y + h / 2, anchor="w", text=when(it["t"]), fill=p["muted"],
                          font=self.mf["meta"])
            body = " ".join(it["text"].split())
            tx = x0 + self.px(W_TIME)
            c.create_text(tx, y + h / 2, anchor="w", font=self.mf["body"], fill=p["ink"],
                          text=ellipsize(self.mf["body"], body,
                                         hx + hw - self.px(SP[3]) - tx))
            self.rows.append((it, y))
        c.configure(scrollregion=(0, 0, w, len(items) * h))
        self._paint_rows()

    def _empty(self, c, w) -> None:
        """Hung under the hairline on the one left edge. Centred in the whole column it was an
        orphan half a screen below the header it belongs to, and the widest muted line in the
        app was the first thing the eye landed on."""
        p, days = self.pal, self.cfg.get("retention_days", 0)
        off = not days
        cx, cy = self.px(SP[4]) - self.px(2), self.px(SP[5])
        c.create_text(cx, cy, anchor="w", text="History is off" if off else "No dictations yet",
                      font=self.mf["body6"], fill=p["ink"])
        c.create_text(cx, cy + self.px(SP[4]), anchor="w", font=self.mf["body"], fill=p["muted"],
                      width=max(self.px(W_COL), w - cx - self.px(SP[4])), justify="left",
                      text="Set ‘Keep dictations for’ in Settings to keep them." if off else
                      "Hold Ctrl+Win and talk — what you say is typed, and kept here for "
                      f"{days:g} days.")

    def _paint_rows(self) -> None:
        for key, want in (("hover", self.hover), ("sel", self.sel)):
            item = self.hl_id.get(key)
            if item is None:
                continue
            y = next((y for it, y in self.rows if it is want), None)
            if y is None or (key == "hover" and want is self.sel):
                self.list.itemconfigure(item, state="hidden")
            else:
                self.list.coords(item, self.px(SP[1]) - self.px(2), y)
                self.list.itemconfigure(item, state="normal")
                if key == "sel":     # the keyboard's row wears the focus ring
                    self.list.itemconfigure(item, image=self.hl[
                        "focus" if self.win.focus_get() is self.list else "sel"])

    def _row_at(self, y):
        i = int(self.list.canvasy(y) // self.px(H_ROW))
        return self.rows[i][0] if 0 <= i < len(self.rows) else None

    def _set_hover(self, it) -> None:
        if it is not self.hover:
            self.hover = it
            self._paint_rows()

    def _row_hover(self, e) -> None:
        self._set_hover(self._row_at(e.y))

    def _row_click(self, e) -> None:
        self.list.focus_set()
        it = self._row_at(e.y)
        if it is not None:
            self._select(it)

    def _select(self, it) -> None:
        self.sel = it
        self._paint_rows()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if it is not None:
            self.detail.insert("1.0", it["text"])
        self.detail.configure(state="disabled")
        self._fit_height()

    def _move(self, step) -> None:
        order = [r[0] for r in self.rows]
        if not order:
            return
        i = next((j for j, it in enumerate(order) if it is self.sel), -1)
        self._select(order[max(0, min(len(order) - 1, i + step))])
        top = next(j for j, it in enumerate(order) if it is self.sel) * self.px(H_ROW)
        vis = self.list.winfo_height()
        if top < self.list.canvasy(0):
            self.list.yview_moveto(top / max(1, len(order) * self.px(H_ROW)))
        elif top + self.px(H_ROW) > self.list.canvasy(0) + vis:
            self.list.yview_moveto((top + self.px(H_ROW) - vis) / max(1, len(order) * self.px(H_ROW)))

    def _nav_key(self, step) -> None:
        """Up/Down reach the list before it has focus - but never while a field has it."""
        f = self.win.focus_get()
        if self.view != "history" or f is self.list or isinstance(f, (tk.Entry, tk.Text, ttk.Combobox)):
            return
        self.list.focus_set()
        self._move(step)

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
        p, pad = self.pal, self.px(SP[4])
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)
        head = tk.Frame(f, bg=p["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[4])))
        tk.Label(head, text="Settings", font=self.F["title"], fg=p["ink"], bg=p["bg"]).pack(side="left")
        self.saved = tk.Label(head, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"])
        self.saved.pack(side="right")

        box = tk.Frame(f, bg=p["bg"])
        box.grid(row=1, column=0, sticky="nsew")
        self.sc = tk.Canvas(box, bg=p["bg"], highlightthickness=0, yscrollincrement=1)
        self.ssb = ttk.Scrollbar(box, orient="vertical", style="M.Vertical.TScrollbar",
                                 command=self.sc.yview)
        self.sc.configure(yscrollcommand=self._settings_scrolled)
        self.sc.pack(side="left", fill="both", expand=True)
        # the rows live inside the scrolled canvas, so their right edge is one scrollbar gutter in
        # from the frame's; reserve the same gutter in the header or the two sit on different edges
        head.grid_configure(padx=(pad, pad + self.ssb.winfo_reqwidth()))
        inner = tk.Frame(self.sc, bg=p["bg"])
        wid = self.sc.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: self.sc.configure(scrollregion=self.sc.bbox("all")))
        self.sc.bind("<Configure>", lambda e: self.sc.itemconfigure(wid, width=e.width))
        self.sc.bind("<Enter>", lambda e: self.sc.bind_all("<MouseWheel>", self._wheel_settings))
        self.sc.bind("<Leave>", lambda e: self.sc.unbind_all("<MouseWheel>"))
        # the cards are inset 8 like the history highlights; their 16 px padding (less the
        # hairline) puts the row labels back on the header's 24 px edge
        body = tk.Frame(inner, bg=p["bg"])
        body.pack(fill="x", padx=self.px(SP[1]), pady=(0, self.px(SP[4])))
        self._indicator(body)
        self._listening(body)
        self._history_group(body)

    def _settings_scrolled(self, lo, hi) -> None:
        self.ssb.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.ssb.pack_forget()
        else:
            self.ssb.pack(side="right", fill="y")

    def _wheel_settings(self, e) -> None:
        # yscrollincrement=1 makes "units" mean pixels in both views, so one notch moves the same
        # distance here as in the list instead of a tenth of whatever the viewport happens to be
        if self._visible():
            self.sc.yview_scroll(int(-e.delta / 120) * 3 * self.px(H_CTL), "units")

    def _group(self, parent, title, first=False) -> tk.Frame:
        """A settings group is a card; its title sits above it on the ground, indented to the
        card's own padding so the title and the row labels share one left edge."""
        tk.Label(parent, text=title, font=self.F["body6"], fg=self.pal["ink"], bg=self.pal["bg"]).pack(
            anchor="w", padx=self.px(SP[3]), pady=(0 if first else self.px(SP[4]), self.px(SP[1])))
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

        self.r_color = _Row(self, g, "Colour", "", col=W_COL_WIDE)
        self._colors(self.r_color, "color", "#e63c3c")
        self._style_desc()
        r = _Row(self, g, "Transcribing colour", "The pulse while text is being typed", col=W_COL_WIDE)
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
                               image=self.rr(self.px(W_CHIP), self.px(24), self.px(R_CTL),
                                             self.up(ground), ground, p["border"]))
        self.l_trig.pack(fill="both", expand=True)
        self.b_rm = _Btn(self, grp, "Remove", self.clear_key)
        self.b_change = _Btn(self, grp, "Change…", self.capture_key)
        chip.pack(side="left")
        self.b_change.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.b_rm.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self._trig_buttons()

        self.r_mic = _Row(self, g, "Microphone", "Applies after restart")
        self.mics = self._list_mics()
        names = ["(system default)"] + [f"{i}: {n}" for i, n in self.mics]
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
        self._combo(self.r_model, self.v_model, MODELS, self._set_model)

        self.r_lang = _Row(self, g, "Language", "Blank = auto (en for *.en models) · applies after restart")
        self.e_lang = self._entry(self.r_lang.right, self.cfg.get("language") or "", 6,
                                  self.F["body"], self._set_lang)
        self.e_lang.master.pack(side="right")

    def _history_group(self, parent) -> None:
        g = self._group(parent, "History")
        self.r_days = _Row(self, g, "Keep dictations for", "0 keeps nothing")
        tk.Label(self.r_days.right, text="days", font=self.F["body"], fg=self.pal["muted"],
                 bg=self.r_days.right["bg"]).pack(side="right", padx=(self.px(SP[1]), 0))
        self.e_days = self._entry(self.r_days.right, f"{self.cfg.get('retention_days', 0):g}", 5,
                                  self.F["mono"], self._set_days)
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

    def _entry(self, parent, value, width, font, commit) -> tk.Entry:
        p = self.pal
        probe = tk.Entry(parent, width=width, font=font)     # what this many characters measure
        w = probe.winfo_reqwidth() + 2 * self.px(SP[1])
        probe.destroy()
        box = self._field(parent, w, self.px(H_CTL))
        e = tk.Entry(box, width=width, font=font, bg=box.fill, fg=p["ink"], relief="flat", bd=0,
                     insertbackground=p["ink"], highlightthickness=0, justify="left",
                     selectbackground=p["selected"], selectforeground=p["ink"])
        e.insert(0, value)
        box.slot(e)
        e.err = [False]
        e.paint = lambda error=None: (e.err.__setitem__(0, e.err[0] if error is None else error),
                                      box.paint("error" if e.err[0] else
                                                "focus" if e is self.win.focus_get() else "idle"))
        e.bind("<FocusIn>", lambda ev: box.paint("error" if e.err[0] else "focus"))
        e.bind("<FocusOut>", lambda ev: (box.paint("error" if e.err[0] else "idle"), commit()))
        e.bind("<Return>", lambda ev: commit())
        return e

    def _combo(self, row, var, values, commit, restart=True) -> None:
        """ttk cannot round a combobox, so it goes inside the same rounded field as an entry
        with its own border painted out (`bordercolor`/`lightcolor`/`darkcolor` = the fill) -
        the shape underneath is the only edge you see."""
        box = self._field(row.right, int(row.right["width"]), self.px(H_CTL))
        box.pack(fill="both", expand=True)
        c = ttk.Combobox(box, textvariable=var, values=values, state="readonly",
                         style="M.TCombobox", font=self.mf["body"])
        box.slot(c)
        c.bind("<FocusIn>", lambda e: box.paint("focus"))
        c.bind("<FocusOut>", lambda e: box.paint())
        c.bind("<<ComboboxSelected>>", lambda e: (commit(), restart and row.restart(), c.selection_clear()))

    def _segment(self, parent, key, options, value, on_pick) -> None:
        """One rounded, hairlined container with the picked option as an inner pill. Each option
        is a Label carrying its own SLICE of that one rendered container (see `segment_png`), so
        the group is still one control and one tab stop - the container takes focus (its hairline
        turns `ring`, as a field's does) and Left/Right move between the options. No separators:
        with one option always filled they would never be seen anyway."""
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
            fills = tuple(p["active"] if v == value[0] else
                          p["hover"] if i == hover[0] else None for i, (_, v) in enumerate(cells))
            border = p["ring"] if focus[0] else p["border_strong"]
            bw = self.px(2) if focus[0] else 1
            for i, (l, val) in enumerate(cells):
                l.configure(fg=p["ink"] if val == value[0] else p["muted"],
                            image=self.img(("seg", ws, i, fills, ground, border, bw),
                                           lambda i=i, fills=fills: segment_png(
                                               ws, h, r, i, fills, ground, ground, border, bw)))

        value = [value]

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
        e = self._entry(row.right, cur[0], 7, self.F["mono"], lambda: pick(e.get(), typed=True))
        e.master.pack(side="right")
        sw = tk.Label(row.right, bg=ground, bd=0)
        sw.pack(side="right", padx=(self.px(SP[2]), self.px(SP[1])))

        def swatch():
            sw.configure(image=self.img((cur[0], "sw"),
                                        lambda: dot_png(self.px(16), self.px(12), cur[0],
                                                        edge=p["border_strong"])))

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
            lab.configure(text=f"{val[0]:.2f}")

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
            knob = "#ffffff" if on[0] else self.up(ground)
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
        if mic.startswith("("):
            self.cfg["mic"] = None
        elif not mic.endswith("(as configured)"):
            # the name, not the index: indices shift when Windows re-enumerates audio devices
            self.cfg["mic"] = dict(self.mics)[int(mic.split(":")[0])]
        self.save()

    def _set_model(self) -> None:
        self.cfg["model"] = self.v_model.get().strip() or self.cfg.get("model")
        self.save()

    def _set_lang(self) -> None:
        v = self.e_lang.get().strip()
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


    # --- promptify: one dictation -> a prompt, in a panel over the list ----------------------
    def _build_panel(self, f) -> None:
        """The Promptify panel takes the list's slot while a draft is open: a prompt of 20-30
        lines plus its questions cannot live under the 5-line detail card. Built once here,
        filled per draft by `_fill_panel`. The dictation stays on top, collapsed to three lines -
        still the thing being worked on, not the work."""
        p, pad = self.pal, self.px(SP[4])
        self.panel = tk.Frame(f, bg=p["bg"])
        self.panel.grid(row=1, column=0, sticky="nsew")
        self.panel.grid_remove()
        self.p_head = tk.Frame(self.panel, bg=p["bg"])
        self.p_head.pack(fill="x", padx=pad, pady=(0, self.px(SP[2])))
        self.link(self.p_head, "‹ History", self._close_panel, font=self.F["body"]).pack(side="left")
        tk.Label(self.p_head, text="Promptify", font=self.F["body6"], fg=p["ink"], bg=p["bg"]).pack(
            side="left", padx=(self.px(SP[3]), 0))
        self.p_seg = None
        self.p_target = [self.cfg.get("prompt_target") or "code"]
        # no line spacing in the panel's Texts: Tk sizes a Text in font lines and ignores spacing,
        # so N spaced lines in a height-N widget clip the last one
        self.p_src = tk.Text(self.panel, height=3, wrap="word", relief="flat", bd=0, highlightthickness=0,
                             bg=p["bg"], fg=p["muted"], font=self.mf["body"], state="disabled",
                             cursor="arrow")
        self.p_src.pack(fill="x", padx=pad)
        st = tk.Frame(self.panel, bg=p["bg"])
        st.pack(fill="x", padx=pad, pady=(self.px(SP[2]), 0))
        self.p_status = tk.Label(st, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"],
                                 anchor="w", justify="left")
        self.p_status.pack(side="left", fill="x", expand=True)
        self._wrap(self.p_status, self.px(200))
        self.b_cancel = _Btn(self, st, "Cancel", self._cancel_draft)
        self.b_ack = _Btn(self, st, "Promptify", self._ack_go, kind="primary")
        self.b_ack_no = _Btn(self, st, "Not now", self._close_panel)
        # the draft scrolls as ONE frame in ONE canvas (the settings pattern): fields placed as
        # separate canvas items would drop out of the tab ring while scrolled out of sight
        box = tk.Frame(self.panel, bg=p["bg"])
        box.pack(fill="both", expand=True, pady=(self.px(SP[2]), 0))
        self.pc = tk.Canvas(box, bg=p["bg"], highlightthickness=0, yscrollincrement=1)
        self.psb = ttk.Scrollbar(box, orient="vertical", style="M.Vertical.TScrollbar",
                                 command=self.pc.yview)
        self.pc.configure(yscrollcommand=self._panel_scrolled)
        self.pc.pack(side="left", fill="both", expand=True)
        self.p_inner = tk.Frame(self.pc, bg=p["bg"])
        wid = self.pc.create_window((0, 0), window=self.p_inner, anchor="nw")
        self.p_inner.bind("<Configure>", lambda e: self.pc.configure(scrollregion=self.pc.bbox("all")))
        self.pc.bind("<Configure>", lambda e: self.pc.itemconfigure(wid, width=e.width))
        self.pc.bind("<Enter>", lambda e: self.pc.bind_all("<MouseWheel>", self._wheel_panel))
        self.pc.bind("<Leave>", lambda e: self.pc.unbind_all("<MouseWheel>"))
        self.draft_item = self.draft_thread = self.draft_cancel = None
        self.p_fields = {}

    def _panel_scrolled(self, lo, hi) -> None:
        self.psb.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.psb.pack_forget()
        else:
            self.psb.pack(side="right", fill="y")

    def _wheel_panel(self, e) -> None:
        if self._visible():
            self.pc.yview_scroll(int(-e.delta / 120) * 3 * self.px(H_CTL), "units")

    def _wrap(self, label, pad) -> None:
        """A label wraps to its parent's width less `pad`, whatever the window does."""
        label.master.bind("<Configure>",
                          lambda e: label.configure(wraplength=max(e.width - pad, self.px(120))), add="+")

    def promptify(self) -> None:
        """The button: show the entry's draft if it has one, else make one. The first time, say
        where the words go - this is the one thing murmur does that sends them off the machine."""
        it = self.sel
        if it is None:
            return
        if self.draft_thread is not None and self.draft_thread.is_alive():
            self.flash(self.s_text, "Still drafting the previous prompt", 3000)
            return
        ok, why = promptify.available(self.cfg)
        if not ok:
            self.flash(self.s_text, why, 8000)
            return
        self._open_panel(it)
        if it.get("draft"):
            self._fill_panel(it["draft"])
            self._status(self._by_line(it["draft"]))
        elif not self.cfg.get("prompt_ack"):
            spec = promptify.engine_spec(self.cfg)
            self._status(f"Sends this dictation to {spec['label']} ({spec['model'] or 'its default model'}) "
                         "through your own login or key. The transcript itself is not changed.")
            self.b_ack.f.pack(side="right")
            self.b_ack_no.f.pack(side="right", padx=(0, self.px(SP[1])))
        else:
            self._run_draft(it)

    def _ack_go(self) -> None:
        self.cfg["prompt_ack"] = True
        self.save()
        self.b_ack.f.pack_forget()
        self.b_ack_no.f.pack_forget()
        self._run_draft(self.draft_item)

    def _by_line(self, d) -> str:
        label = promptify.ENGINES.get(d.get("engine"), {}).get("label", d.get("engine", ""))
        return f"Drafted by {label} ({d.get('model', '')}) in {d.get('wall', 0):.0f} s"

    def _status(self, text, danger=False) -> None:
        self.p_status.configure(text=text, fg=self.pal["danger_text"] if danger else self.pal["muted"])

    def _open_panel(self, it) -> None:
        self.draft_item = it
        self.panel_open = True
        self.box.grid_remove()
        self.card.grid_remove()
        self.panel.grid()
        self._clear_body()
        for b in (self.b_cancel, self.b_ack, self.b_ack_no):
            b.f.pack_forget()
        self._status("")
        self.p_src.configure(state="normal")
        self.p_src.delete("1.0", "end")
        self.p_src.insert("1.0", it["text"])
        self.p_src.configure(state="disabled")
        # the target segment is rebuilt per open: its value lives in the control's own closure
        if self.p_seg is not None:
            self.p_seg.destroy()
        self.p_target[0] = (it.get("draft") or {}).get("target") or self.cfg.get("prompt_target") or "code"
        self.p_seg = self._segment(self.p_head, "target", list(promptify.TARGETS), self.p_target[0],
                                   self._set_target)

    def _close_panel(self) -> None:
        if not self.panel_open:
            return
        self._cancel_draft()
        self._save_edits()
        self.panel_open = False
        self.panel.grid_remove()
        self.box.grid()
        if self.history.items:
            self.card.grid()
        self.list.focus_set()

    def _escape(self) -> None:
        if self.panel_open:
            self._close_panel()
        else:
            self._clear_cancel()

    def _set_target(self, v) -> None:
        self.p_target[0] = v
        self.cfg["prompt_target"] = v
        self.save()

    def _run_draft(self, it, prompts=None, questions=None, answers=None) -> None:
        """Pass 1 (no prompts) or pass 2, on a worker thread. The worker only fills `res`; the
        ticker on the Tk thread shows the elapsed time and consumes the result when the thread
        ends - Tk calls from the worker (even after()) are not safe outside mainloop."""
        spec = promptify.engine_spec(self.cfg)
        self._clear_body()
        self.draft_cancel = threading.Event()
        cancel = self.draft_cancel
        self.b_cancel.f.pack(side="right")
        t0 = time.monotonic()
        self._status(f"Drafting with {spec['label']}…")
        res = {}

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
                if self.panel_open:
                    self._status(f"Drafting with {spec['label']} · {s} s" + (" · still working" if s >= 45 else ""))
                self.jobs["tick"] = self.win.after(200, tick)
            elif "ok" in res:
                self._draft_done(it, res["ok"])
            elif "err" in res:
                self._draft_failed(res["err"])
            else:
                self._cancel_draft()
        thread = self.draft_thread = threading.Thread(target=work, daemon=True)
        thread.start()
        tick()

    def _cancel_draft(self) -> None:
        if self.draft_cancel is not None:
            self.draft_cancel.set()
        self.b_cancel.f.pack_forget()
        job = self.jobs.pop("tick", None)
        if job:
            self.win.after_cancel(job)
        if self.panel_open:
            self._status("")

    def _draft_failed(self, msg) -> None:
        if not (self.win and self.win.winfo_exists()):
            return
        self._cancel_draft()
        self._status(msg, danger=True)

    def _draft_done(self, it, res) -> None:
        if not (self.win and self.win.winfo_exists()):
            return
        self._cancel_draft()
        it["draft"] = res
        self.history.save()
        if self.panel_open and self.draft_item is it:
            self._fill_panel(res)
            self._status(self._by_line(res))

    def _clear_body(self) -> None:
        for w in self.p_inner.winfo_children():
            w.destroy()
        self.p_fields = {"prompts": [], "questions": [], "answers": [], "cur": 0, "text": None}
        self.pc.yview_moveto(0)

    def _fill_panel(self, d) -> None:
        """The draft: the prompt in an editable field (a '1 of 2' switch when the dictation split
        into independent asks), the heard/wrote notes, one card per question with its chips and
        an answer field, and the actions. Copy is the primary action here; the transcript's own
        Copy is off screen while the panel is open, so there is still one primary on screen."""
        p, pad, cp = self.pal, self.px(SP[4]), self.cpad
        self._clear_body()
        F = self.p_fields
        F["prompts"] = list(d.get("prompts") or [""])
        F["questions"] = list(d.get("questions") or [])
        inner = self.p_inner
        top = tk.Frame(inner, bg=p["bg"])
        top.pack(fill="x", padx=pad)
        tk.Label(top, text="Prompt", font=self.F["body6"], fg=p["ink"], bg=p["bg"]).pack(side="left")
        if len(F["prompts"]) > 1:
            self._segment(top, "which", [(f"{i + 1} of {len(F['prompts'])}", i) for i in range(len(F["prompts"]))],
                          0, self._switch_prompt)
        card = self._card(inner)
        card.pack(fill="x", padx=self.px(SP[1]), pady=(self.px(SP[1]), 0))
        # the field shows the whole prompt: the panel is the one thing that scrolls (a Text that
        # scrolls inside a canvas that scrolls fights the wheel)
        F["text"] = self._textbox(card.body, F["prompts"][0], (4, 200))
        if d.get("notes"):
            n = tk.Label(inner, text=d["notes"], font=self.F["meta"], fg=p["muted"], bg=p["bg"],
                         anchor="w", justify="left")
            n.pack(fill="x", padx=pad, pady=(self.px(SP[1]), 0))
            self._wrap(n, 2 * pad)
        qs = F["questions"]
        tk.Label(inner, text="Questions" if qs else "No questions: the dictation was specific enough.",
                 font=self.F["body6"] if qs else self.F["body"], fg=p["ink"] if qs else p["muted"],
                 bg=p["bg"]).pack(anchor="w", padx=pad, pady=(self.px(SP[4]), self.px(SP[1])))
        for q in qs:
            c = self._card(inner)
            c.pack(fill="x", padx=self.px(SP[1]), pady=(0, self.px(SP[1])))
            b = c.body
            l = tk.Label(b, text=q["q"], font=self.F["body"], fg=p["ink"], bg=b["bg"], anchor="w", justify="left")
            l.pack(fill="x", padx=cp, pady=(cp, 0))
            self._wrap(l, 2 * cp)
            if q.get("why"):
                w = tk.Label(b, text=q["why"], font=self.F["meta"], fg=p["muted"], bg=b["bg"], anchor="w",
                             justify="left")
                w.pack(fill="x", padx=cp, pady=(self.px(SP[0]), 0))
                self._wrap(w, 2 * cp)
            a = self._textbox(b, "", (1, 3), boxed=True)
            F["answers"].append(a)
            if q.get("options"):
                row = tk.Frame(b, bg=b["bg"])
                row.pack(fill="x", padx=cp, pady=(self.px(SP[1]), 0), before=a.master.master)
                for o in q["options"]:
                    _Btn(self, row, o, lambda o=o, a=a: self._pick(a, o), font=self.F["meta"],
                         h=24, pad=8).f.pack(side="left", padx=(0, self.px(SP[1])))
        act = tk.Frame(inner, bg=p["bg"])
        act.pack(fill="x", padx=pad, pady=(self.px(SP[2]), self.px(SP[4])))
        _Btn(self, act, "Copy prompt", self._copy_prompt, kind="primary").f.pack(side="left")
        _Btn(self, act, "Update prompt" if qs else "Regenerate", self._update).f.pack(
            side="left", padx=(self.px(SP[1]), 0))
        _Btn(self, act, "Close", self._close_panel).f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.p_flash = tk.Label(act, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"])
        self.p_flash.pack(side="right")

    def _textbox(self, parent, text, lines, boxed=False) -> tk.Text:
        """An editable Text that grows with its content between `lines` (min, max). `boxed` puts
        it in its own rounded, hairlined field inside a card - the answer fields. Tab moves on
        instead of inserting a tab: these are form fields, not editors."""
        p = self.pal
        host = parent
        if boxed:
            c = self._card(parent, R_CTL)
            c.pack(fill="x", padx=self.cpad, pady=(self.px(SP[1]), self.cpad))
            host = c.body
        t = tk.Text(host, height=lines[0], wrap="word", relief="flat", bd=0, highlightthickness=0,
                    bg=host["bg"], fg=p["ink"], font=self.mf["body"], insertbackground=p["ink"],
                    selectbackground=p["selected"], selectforeground=p["ink"], undo=True)
        t.insert("1.0", text)
        t.pack(fill="x", padx=self.px(SP[1]) if boxed else self.cpad,
               pady=self.px(SP[0]) if boxed else self.cpad)

        def fit(e=None):
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

    def _pick(self, field, option) -> None:
        field.delete("1.0", "end")
        field.insert("1.0", option)
        field.fit()
        field.focus_set()

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
        if not F.get("text") or not F["text"].winfo_exists():
            return
        F["prompts"][F["cur"]] = F["text"].get("1.0", "end").strip()
        d = (self.draft_item or {}).get("draft")
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
        self.flash(self.p_flash, "Copied")

    def _update(self) -> None:
        """Pass 2 with the answers as they stand; with none given, pass 1 again."""
        if self.draft_thread is not None and self.draft_thread.is_alive():
            return
        self._save_edits()
        F = self.p_fields
        answers = [a.get("1.0", "end").strip() for a in F["answers"] if a.winfo_exists()]
        if any(answers):
            self._run_draft(self.draft_item, list(F["prompts"]), list(F["questions"]), answers)
        else:
            self._run_draft(self.draft_item)

    def receive(self, text: str) -> bool:
        """A take that arrived while this window was in front: into the focused answer or prompt
        field at the caret. Anything else on screen is not ours to type into."""
        if not (self.panel_open and self._visible()):
            return False
        f = self.win.focus_get()
        if not isinstance(f, tk.Text) or not str(f).startswith(str(self.p_inner)):
            return False
        before = f.get("1.0", "insert")
        f.insert("insert", ("" if not before or before.endswith((" ", "\n")) else " ") + text)
        getattr(f, "fit", lambda: None)()
        f.see("insert")
        return True

    # --- promptify settings ------------------------------------------------------------------
    def _promptify_group(self, parent) -> None:
        g = self._group(parent, "Promptify")
        labels = [promptify.ENGINES[k]["label"] for k in promptify.ORDER]
        cur = promptify.engine_spec(self.cfg)["key"]
        self.r_eng = _Row(self, g, "Engine", "Who writes the prompt, on its own login or your key")
        self.v_engine = tk.StringVar(value=promptify.ENGINES[cur]["label"])
        self._combo(self.r_eng, self.v_engine, labels, self._set_engine, restart=False)
        self.r_pmodel = _Row(self, g, "Model", "")
        self.e_pmodel = self._entry(self.r_pmodel.right, self.cfg.get("prompt_model") or "", 24,
                                    self.F["mono9"], self._set_pmodel)
        self.e_pmodel.master.pack(side="right")
        self.r_pkey = _Row(self, g, "API key", "Kept in config.json in your profile folder")
        self.e_pkey = self._entry(self.r_pkey.right, self.cfg.get("prompt_key") or "", 24,
                                  self.F["mono9"], self._set_pkey)
        self.e_pkey.configure(show="•")
        self.e_pkey.master.pack(side="right")
        self.r_purl = _Row(self, g, "Base URL", "An OpenAI-compatible endpoint, e.g. http://localhost:11434/v1")
        self.e_purl = self._entry(self.r_purl.right, self.cfg.get("prompt_url") or "", 24,
                                  self.F["mono9"], self._set_purl)
        self.e_purl.master.pack(side="right")
        self._engine_rows()

    def _engine_rows(self) -> None:
        """The model hint follows the engine; the key and URL rows exist only for the engines
        that need them."""
        spec = promptify.engine_spec(self.cfg)
        self.r_pmodel.desc.configure(text=spec["models"])
        self.r_pmodel.desc.pack(anchor="w", pady=(self.px(SP[0]), 0))
        self.r_pkey.show(spec["kind"] != "cli", after=self.r_pmodel)
        self.r_purl.show(spec["key"] == "custom", after=self.r_pkey if spec["kind"] != "cli" else self.r_pmodel)

    def _set_engine(self) -> None:
        name = self.v_engine.get()
        self.cfg["prompt_engine"] = next((k for k in promptify.ORDER if promptify.ENGINES[k]["label"] == name), "claude")
        self._engine_rows()
        self.save()

    def _set_pmodel(self) -> None:
        self.cfg["prompt_model"] = self.e_pmodel.get().strip()
        self.save()

    def _set_pkey(self) -> None:
        self.cfg["prompt_key"] = self.e_pkey.get().strip()
        self.save()

    def _set_purl(self) -> None:
        self.cfg["prompt_url"] = self.e_purl.get().strip()
        self.save()

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
        self.count.configure(text=f"{n} dictation{'s' if n != 1 else ''} · {kept}")
        self._clear_cancel()
        self.b_clear.f.pack(side="right") if n else self.b_clear.f.pack_forget()
        self.card.grid() if n and not self.panel_open else self.card.grid_remove()
        self._draw_rows()
        self._select(self.sel)
