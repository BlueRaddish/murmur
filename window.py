"""The app window: transcript history plus settings. Hidden until opened from the tray.

One Toplevel, a sidebar and two views. The look is built from ONE hue - the user's accent
colour - run through an OKLCH 12-step ramp (Radix semantics): neutrals are that same hue at
2-6 % chroma, so nothing here is a flat grey. Depth language is hairlines + a surface ladder
at radius 0; Tk cannot round a widget corner, so the only round things are PIL-rendered (the
colour dots, the toggle pill, the slider knob). No motion: hover/active are instant colour
swaps. Settings autosave - there is no Save button.
"""
import base64
import datetime
import io
import json
import math
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

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


# L, C per step. Light descends; dark is not a mirror - steps 1-2 are tinted near-black.
# Chroma budget: 9/10 (the solid accent fills) and 8 (the focus ring, which has to be seen as
# the accent) carry real chroma; everything else - step 11 included, because it is secondary
# TEXT here - stays a tinted neutral at 1-5 %, or the whole app reads as one loud green.
LIGHT = [(0.99, .012), (0.98, .012), (0.955, .012), (0.93, .014), (0.90, .016), (0.85, .018),
         (0.78, .02), (0.60, .13), (0.55, .17), (0.50, .17), (0.48, .035), (0.25, .05)]
DARK = [(0.14, .008), (0.17, .008), (0.21, .01), (0.24, .012), (0.27, .014), (0.31, .016),
        (0.36, .02), (0.55, .13), (0.60, .14), (0.65, .14), (0.70, .03), (0.92, .01)]
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
H_CTL = 32                          # control height
H_ROW = 36                          # history row
H_CHIP = 20                         # "restart to apply" chip
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


# --- widgets --------------------------------------------------------------------------------


class _Btn:
    """A button is a Label with bindings: hover/active swap the background only - no motion,
    no weight or size shift. Focus ring via highlightcolor."""

    def __init__(self, ui, parent, text, cmd, kind="ghost", font=None, h=None, pad=None):
        p, self.ui, self.cmd, self.kind = ui.pal, ui, cmd, kind
        ground = parent["bg"]
        self.fill, self.fg, self.hov = {
            "primary": (p["primary"], p["on_primary"], p["primary_hover"]),
            "danger": (p["danger"], p["on_danger"], p["danger_hover"]),
            "ghost": (ground, p["muted"], p["hover"]),
        }[kind]
        ghost = kind == "ghost"
        # every kind gets the same 2 px focus ring; the ghost's own hairline lives on an INNER
        # frame so the ring never has to overwrite the border to be seen
        self.f = tk.Frame(parent, bg=ground, height=ui.px(h or H_CTL), takefocus=1,
                          cursor="hand2", highlightthickness=ui.px(2),
                          highlightbackground=ground, highlightcolor=p["ring"])
        self.b = tk.Frame(self.f, bg=self.fill, highlightthickness=1 if ghost else 0,
                          highlightbackground=p["border_strong"])
        self.b.pack(fill="both", expand=True)
        self.l = tk.Label(self.b, text=text, font=font or ui.F["body"], bg=self.fill, fg=self.fg)
        self.l.pack(fill="both", expand=True)
        self.f.pack_propagate(False)
        self.f.configure(width=self.l.winfo_reqwidth() + 2 * ui.px(12 if pad is None else pad))
        for w in (self.f, self.b, self.l):
            w.bind("<Enter>", lambda e: self._paint(self.hov))
            w.bind("<Leave>", lambda e: self._paint(self.fill))
            w.bind("<Button-1>", lambda e: self._paint(p["active"] if ghost else self.hov))
            w.bind("<ButtonRelease-1>", self._release)
        for k in ("<Return>", "<space>"):
            self.f.bind(k, lambda e: self.cmd())

    def _paint(self, bg) -> None:
        self.b.configure(bg=bg)
        self.l.configure(bg=bg)

    def _release(self, e) -> None:
        self._paint(self.hov)
        self.f.focus_set()
        if 0 <= e.x < self.f.winfo_width() and 0 <= e.y < self.f.winfo_height():
            self.cmd()

    def text(self, s) -> None:
        self.l.configure(text=s)
        self.f.configure(width=self.l.winfo_reqwidth() + 2 * self.ui.px(12))


class _Row:
    """A settings row: label (+ description, + restart chip) left, the control right-aligned in
    a fixed column. Hairline above every row but the first."""

    def __init__(self, ui, group, label, desc=None, col=W_COL):
        p, bg = ui.pal, ui.pal["bg"]
        if group.winfo_children():
            ui.hairline(group).pack(fill="x")
        self.ui = ui
        self.frame = tk.Frame(group, bg=bg)
        self.frame.pack(fill="x")
        main = tk.Frame(self.frame, bg=bg)
        main.pack(fill="x")
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

    def restart(self) -> None:
        """Model / microphone / language changed: say so, and keep saying it."""
        if self.chip is None:
            box = tk.Frame(self.head, bg=self.ui.pal["s3"], height=self.ui.px(H_CHIP))
            # accent text on a tint of the same hue - a grey label on a grey pill is the
            # generic-chip tell, and this one has no border to save it
            self.chip = tk.Label(box, text="restart to apply", font=self.ui.F["meta"],
                                 bg=self.ui.pal["s3"], fg=self.ui.pal["primary"],
                                 padx=self.ui.px(SP[1]))
            self.chip.pack(fill="both", expand=True)
            box.pack_propagate(False)
            box.configure(width=self.chip.winfo_reqwidth())
            box.pack(side="left", padx=(self.ui.px(SP[1]), 0))

    def error(self, msg) -> None:
        self.err.configure(text=msg or "")
        (self.err.pack(fill="x", pady=(0, self.ui.px(SP[1]))) if msg else self.err.pack_forget())


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
        self.rows = []            # (item, bg id, bar id) per drawn row
        self.ctl = {}             # the settings controls, by cfg key (the tests drive these)
        self.foot_links = []      # sidebar footer links, one per entry in `links`
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
        p = self.pal = palette(self.cfg.get("color"), self.dark)
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
        w.bind("<Escape>", lambda e: self._clear_cancel())
        w.bind("<Up>", lambda e: self._nav_key(-1))
        w.bind("<Down>", lambda e: self._nav_key(1))
        self.go(self.view)

    def _ttk(self) -> None:
        p, st = self.pal, ttk.Style(self.win)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("M.TCombobox", fieldbackground=p["surface"], background=p["surface"],
                     foreground=p["ink"], bordercolor=p["border_strong"], arrowcolor=p["muted"],
                     lightcolor=p["surface"], darkcolor=p["surface"], insertcolor=p["ink"],
                     selectbackground=p["hover"], selectforeground=p["ink"], padding=(8, 4),
                     arrowsize=self.px(SP[1]))   # clam's default arrow is a 2 px sliver
        st.map("M.TCombobox", fieldbackground=[("readonly", p["surface"])],
               foreground=[("readonly", p["ink"])], bordercolor=[("focus", p["ring"])],
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
        # chrome, not content: same size as a view title but muted, so "History" reads louder
        tk.Label(s, text="murmur", font=self.F["title"], fg=p["muted"], bg=p["surface"]).pack(
            anchor="w", padx=self.px(SP[3]), pady=(self.px(SP[4]), self.px(SP[4])))
        self.nav = {}
        for name, label in (("history", "History"), ("settings", "Settings")):
            row = tk.Frame(s, bg=p["surface"], height=self.px(H_CTL), cursor="hand2")
            row.pack(fill="x", padx=self.px(SP[1]))
            row.pack_propagate(False)
            l = tk.Label(row, text=label, font=self.F["body"], fg=p["muted"], bg=p["surface"],
                         anchor="w", padx=self.px(SP[1]))
            l.pack(fill="both", expand=True)
            self.nav[name] = (row, l)
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

    def _nav_hover(self, name, on) -> None:
        if name == self.view:
            return
        p = self.pal
        row, l = self.nav[name]
        row.configure(bg=p["hover"] if on else p["surface"])
        l.configure(bg=p["hover"] if on else p["surface"])

    def go(self, name) -> None:
        self.view = name
        p = self.pal
        for n, (row, l) in self.nav.items():
            sel = n == name
            row.configure(bg=p["active"] if sel else p["surface"])
            l.configure(bg=p["active"] if sel else p["surface"], fg=p["ink"] if sel else p["muted"])
        for n, v in self.views.items():        # not tkraise: an unmapped frame is skipped by
            v.grid() if n == name else v.grid_remove()   # tk_focusNext, a raised-over one is not

    # --- history view ------------------------------------------------------------------------
    def _build_history(self, f) -> None:
        p, pad = self.pal, self.px(SP[4])
        f.grid_rowconfigure(2, weight=1)
        f.grid_columnconfigure(0, weight=1)

        head = tk.Frame(f, bg=p["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[2])))
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

        self.hairline(f).grid(row=1, column=0, sticky="ew")
        box = tk.Frame(f, bg=p["bg"])
        box.grid(row=2, column=0, sticky="nsew")
        self.list = tk.Canvas(box, bg=p["bg"], highlightthickness=self.px(2),
                              highlightbackground=p["bg"], highlightcolor=p["ring"],
                              takefocus=1, yscrollincrement=1)
        self.sb = ttk.Scrollbar(box, orient="vertical", style="M.Vertical.TScrollbar",
                                command=self.list.yview)
        self.list.configure(yscrollcommand=self._scrolled)
        self.list.pack(side="left", fill="both", expand=True)
        self.list.bind("<Configure>", lambda e: self._draw_rows())
        self.list.bind("<Motion>", self._row_hover)
        self.list.bind("<Leave>", lambda e: self._set_hover(None))
        self.list.bind("<Button-1>", self._row_click)
        self.list.bind("<Double-Button-1>", lambda e: self.copy_selected())
        self.list.bind("<Enter>", lambda e: self.list.bind_all("<MouseWheel>", self._wheel_list))
        self.list.bind("<Leave>", lambda e: self.list.unbind_all("<MouseWheel>"), add="+")
        for k, fn in (("<Up>", lambda e: self._move(-1)), ("<Down>", lambda e: self._move(1)),
                      ("<Return>", lambda e: self.copy_selected()),
                      ("<Control-c>", lambda e: self.copy_selected()),
                      ("<Delete>", lambda e: self.delete_selected())):
            self.list.bind(k, fn)

        self.dmax = 5                 # a short window gives the list the room, not the panel
        f.bind("<Configure>", self._fit_detail)
        self.h_detail = self.hairline(f)
        self.h_detail.grid(row=3, column=0, sticky="ew")
        self.detail = tk.Text(f, height=5, width=80, wrap="word", relief="flat", bd=0,
                              highlightthickness=0, bg=p["bg"], fg=p["ink"], font=self.mf["body"],
                              spacing1=self.px(LH), spacing3=self.px(LH), state="disabled",
                              cursor="xterm", selectbackground=p["selected"],
                              selectforeground=p["ink"], inactiveselectbackground=p["selected"])
        # sticky "w", not "ew": stretched, the width request is ignored and a maximised window
        # gives the transcript ~200-character lines - measure has to stay near 80
        self.detail.grid(row=4, column=0, sticky="w", padx=pad, pady=(self.px(SP[2]), 0))

        act = self.act = tk.Frame(f, bg=p["bg"])
        act.grid(row=5, column=0, sticky="ew", padx=pad, pady=(self.px(SP[2]), self.px(SP[4])))
        self.b_copy = _Btn(self, act, "Copy", self.copy_selected, kind="primary")
        self.b_copy.f.pack(side="left")
        self.b_del = _Btn(self, act, "Delete", self.delete_selected)
        self.b_del.f.pack(side="left", padx=(self.px(SP[1]), 0))
        self.status = tk.Frame(act, bg=p["bg"])
        self.status.pack(side="right", pady=self.px(SP[1]))
        self.s_text = tk.Label(self.status, text="", font=self.F["body"], fg=p["muted"], bg=p["bg"])
        self.s_text.pack(side="left")
        # underlined so the one clickable word in the status line does not read as more meta
        self.s_undo = self.link(self.status, "Undo", self._undo, font=self.F["body"] + ("underline",))

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
            self._empty(c, w)
            c.configure(scrollregion=(0, 0, 0, 0))
            return
        for i, idx in enumerate(range(len(items) - 1, -1, -1)):
            it, y = items[idx], i * h
            bg = c.create_rectangle(0, y, w, y + h, fill=p["bg"], outline="")
            bar = c.create_rectangle(0, y, self.px(2), y + h, fill=p["primary"], outline="",
                                     state="hidden")
            c.create_text(x0, y + h / 2, anchor="w", text=when(it["t"]), fill=p["muted"],
                          font=self.mf["meta"])
            body = " ".join(it["text"].split())
            tx = x0 + self.px(W_TIME)
            c.create_text(tx, y + h / 2, anchor="w", font=self.mf["body"], fill=p["ink"],
                          text=ellipsize(self.mf["body"], body, w - tx - self.px(SP[4])))
            self.rows.append((it, bg, bar))
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
        p = self.pal
        for it, bg, bar in self.rows:
            sel = it is self.sel
            self.list.itemconfigure(bg, fill=p["active"] if sel else
                                    p["hover"] if it is self.hover else p["bg"])
            self.list.itemconfigure(bar, state="normal" if sel else "hidden")

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
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(self.px(PAD_TOP), self.px(SP[2])))
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
        body = tk.Frame(inner, bg=p["bg"])
        body.pack(fill="x", padx=pad, pady=(0, self.px(SP[4])))
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
        tk.Label(parent, text=title, font=self.F["body6"], fg=self.pal["ink"], bg=self.pal["bg"]).pack(
            anchor="w", pady=(0 if first else self.px(SP[4]), self.px(SP[1])))
        g = tk.Frame(parent, bg=self.pal["bg"])
        g.pack(fill="x")
        return g

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
        grp = tk.Frame(r.right, bg=p["bg"])
        grp.pack(side="right")
        chip = tk.Frame(grp, bg=p["hover"], height=self.px(24), highlightthickness=1,
                        highlightbackground=p["border"])
        self.l_trig = tk.Label(chip, text=vk_name(self.cfg.get("trigger_vk")), font=self.F["mono9"],
                               bg=p["hover"], fg=p["ink"] if self.cfg.get("trigger_vk") else p["muted"],
                               padx=self.px(SP[1]))
        self.l_trig.pack(fill="both", expand=True)
        chip.pack_propagate(False)
        chip.configure(width=self.px(W_CHIP))
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
                 bg=self.pal["bg"]).pack(side="right", padx=(self.px(SP[1]), 0))
        self.e_days = self._entry(self.r_days.right, f"{self.cfg.get('retention_days', 0):g}", 5,
                                  self.F["mono"], self._set_days)
        self.e_days.master.pack(side="right")

    # --- controls ----------------------------------------------------------------------------
    def _entry(self, parent, value, width, font, commit) -> tk.Entry:
        p = self.pal
        box = tk.Frame(parent, bg=p["bg"], height=self.px(H_CTL))
        box.pack_propagate(False)
        e = tk.Entry(box, width=width, font=font, bg=p["surface"], fg=p["ink"], relief="flat",
                     insertbackground=p["ink"], highlightthickness=1, justify="left",
                     highlightbackground=p["border_strong"], highlightcolor=p["ring"],
                     selectbackground=p["selected"], selectforeground=p["ink"])
        e.insert(0, value)
        e.pack(fill="both", expand=True, ipadx=self.px(SP[1]))
        box.configure(width=e.winfo_reqwidth() + 2 * self.px(SP[1]))
        e.bind("<FocusOut>", lambda ev: commit())
        e.bind("<Return>", lambda ev: commit())
        return e

    def _combo(self, row, var, values, commit) -> None:
        c = ttk.Combobox(row.right, textvariable=var, values=values, state="readonly",
                         style="M.TCombobox", font=self.mf["body"])
        c.pack(fill="both", expand=True)
        c.bind("<<ComboboxSelected>>", lambda e: (commit(), row.restart(), c.selection_clear()))

    def _segment(self, parent, key, options, value, on_pick) -> None:
        """Two labels in one hairline frame - a radio group that reads as one control, and so
        one tab stop: the frame takes focus (its hairline turns `ring`, as an entry's does) and
        Left/Right move between the options."""
        p = self.pal
        f = tk.Frame(parent, bg=p["border_strong"], height=self.px(H_CTL), highlightthickness=1,
                     takefocus=1, highlightbackground=p["border_strong"], highlightcolor=p["ring"])
        f.pack(side="right")
        f.pack_propagate(False)
        cells, total = [], 0
        for i, (text, val) in enumerate(options):
            if i:
                sep = tk.Frame(f, width=1, bg=p["border_strong"])
                sep.pack(side="left", fill="y")
                total += 1
            l = tk.Label(f, text=text, font=self.F["body"], padx=self.px(SP[2]), cursor="hand2",
                         bg=p["bg"], fg=p["muted"])
            l.pack(side="left", fill="y")
            total += l.winfo_reqwidth()
            cells.append((l, val))

        def paint():
            for l, val in cells:
                on = val == value[0]
                l.configure(bg=p["active"] if on else p["bg"], fg=p["ink"] if on else p["muted"])

        value = [value]

        def pick(v):
            value[0] = v
            paint()
            f.focus_set()
            on_pick(v)

        for l, val in cells:
            l.bind("<Enter>", lambda e, l=l, v=val: l.configure(
                bg=p["hover"]) if v != value[0] else None)
            l.bind("<Leave>", lambda e: paint())
            l.bind("<Button-1>", lambda e, v=val: pick(v))
        step = lambda d: pick(cells[(next(i for i, c in enumerate(cells) if c[1] == value[0])
                                    + d) % len(cells)][1])
        f.bind("<Left>", lambda e: step(-1))
        f.bind("<Right>", lambda e: step(1))
        f.configure(width=total)
        self.ctl[key] = cells
        paint()

    def _colors(self, row, key, default) -> None:
        """Nine presets plus a hex field - the field is the escape hatch, the dots are the taste."""
        p, cur = self.pal, [norm_hex(self.cfg.get(key), default)]
        box, d, ring = self.px(24), self.px(16), self.px(2)
        # one tab stop for the whole strip (nine would bury the hex field): Left/Right move a
        # cursor, Space/Return picks - so arrowing past a dot never writes cfg
        kb = [0, False]                                    # cursor index, strip has focus
        dots = tk.Frame(row.right, bg=p["bg"], takefocus=1, highlightthickness=ring,
                        highlightbackground=p["bg"], highlightcolor=p["ring"])
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
            l = tk.Label(dots, bg=p["bg"], cursor="hand2", bd=0)
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
        sw = tk.Label(row.right, bg=p["bg"], bd=0)
        sw.pack(side="right", padx=(self.px(SP[2]), self.px(SP[1])))

        def swatch():
            sw.configure(image=self.img((cur[0], "sw"),
                                        lambda: dot_png(self.px(16), self.px(12), cur[0],
                                                        edge=p["border_strong"])))

        def pick(v, typed=False):
            hx = norm_hex(v)
            if hx is None:
                row.error("Use a hex colour like #e63c3c")
                e.configure(highlightbackground=p["danger"], highlightcolor=p["danger"])
                return
            row.error("")
            e.configure(highlightbackground=p["border_strong"], highlightcolor=p["ring"])
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
        p, ring = self.pal, self.px(2)
        W, H = self.px(160) - 2 * ring, self.px(H_CTL) - 2 * ring   # the ring is inside the 160x32
        val = [round(value, 2)]
        lab = tk.Label(parent, text="", font=self.F["mono"], fg=p["muted"], bg=p["bg"])
        lab.pack(side="right")
        c = tk.Canvas(parent, width=W, height=H, bg=p["bg"], cursor="hand2", takefocus=1,
                      highlightthickness=ring, highlightbackground=p["bg"], highlightcolor=p["ring"])
        c.pack(side="right", padx=(0, self.px(SP[2])))
        r = self.px(14) / 2
        track = c.create_line(r, H / 2, W - r, H / 2, fill=p["border_strong"], width=self.px(2))
        fill = c.create_line(r, H / 2, r, H / 2, fill=p["primary"], width=self.px(2))
        knob = c.create_image(r, H / 2, image=self.img(
            ("knob",), lambda: dot_png(self.px(18), self.px(14), p["surface"], p["border_strong"], 1, 0)))

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
        on = [bool(value)]
        l = tk.Label(parent, bg=p["bg"], bd=0, cursor="hand2", takefocus=1,
                     highlightthickness=self.px(2), highlightbackground=p["bg"],
                     highlightcolor=p["ring"])
        l.pack(side="right")

        def paint(hover=False):
            track = (p["primary_hover"] if hover else p["primary"]) if on[0] else \
                    (p["ring"] if hover else p["border_strong"])
            knob = "#ffffff" if on[0] else p["surface"]
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
            self.cfg["mic"] = int(mic.split(":")[0])
        self.save()

    def _set_model(self) -> None:
        self.cfg["model"] = self.v_model.get().strip() or self.cfg.get("model")
        self.save()

    def _set_lang(self) -> None:
        v = self.e_lang.get().strip()
        if v and not (v.isalpha() and len(v) <= 5):
            self.r_lang.error("Use a language code like en, or leave it blank")
            self.e_lang.configure(highlightbackground=self.pal["danger"])
            return
        self.r_lang.error("")
        self.e_lang.configure(highlightbackground=self.pal["border_strong"])
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
            self.e_days.configure(highlightbackground=self.pal["danger"])
            return
        self.r_days.error("")
        self.e_days.configure(highlightbackground=self.pal["border_strong"])
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
        for w in (self.h_detail, self.detail, self.act):
            w.grid() if n else w.grid_remove()
        self._draw_rows()
        self._select(self.sel)
