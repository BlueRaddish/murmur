"""On-screen indicator: a glassy disc with a soft glow and vertical level bars.

Rendered with PIL at physical DPI (supersampled, anti-aliased) into a Win32 layered
window with per-pixel alpha, so it is crisp on HiDPI and has real shadows and glow
rather than colour-keyed corners. Click-through and never takes focus.

Why not tkinter's canvas: it is not DPI-aware (Windows bitmap-scales it) and draws
without anti-aliasing, which is what looked pixelated.
"""
import ctypes
import ctypes.wintypes as wt
import queue
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOPMOST = 0x8
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x80
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x2, 0x1, 0x10

COLORS = {"idle": (220, 220, 230), "recording": (230, 60, 60), "persistent": (230, 60, 60),
          "busy": (255, 170, 50), "loading": (255, 170, 50)}


def hex_rgb(h: str, fallback):
    """'#rrggbb' -> (r, g, b); anything else -> fallback."""
    try:
        h = h.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return fallback
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return fallback

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# 64-bit handles: without argtypes ctypes truncates them to C int
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_void_p, ctypes.c_void_p, wt.HDC,
                                       ctypes.c_void_p, wt.COLORREF, ctypes.c_void_p, wt.DWORD]
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wt.HBITMAP
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


def set_dpi_aware() -> float:
    """Per-monitor DPI awareness for the process; returns the primary monitor's scale."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class Overlay:
    """A small glass stick at the bottom centre. Near-invisible at rest. While recording the
    stick *is* the visualizer: a live frequency spectrum (FFT -> log bands -> neighbour
    spreading -> gravity fall-off, the cava/easyeffects recipe) becomes one solid glowing
    silhouette, mirrored about the centre line, lows in the middle and highs pinching back
    into the stick's tips. Red; amber in persistent mode. An orange-to-yellow pulse runs
    along it while transcribing. Call tick() from the UI thread's timer (~25 fps);
    post(state) from any thread. With indicator=light the stick keeps its resting look and a
    single breathing light sits behind it instead of the spectrum."""

    W, H = 48, 7     # stick size in logical px
    PAD = 22         # headroom for the waveform, glow and shadow
    BANDS = 20       # spectrum bands per half (mirrored -> 40 across)
    NBARS = 14       # fixed-size bars across the container
    SS = 2           # supersampling
    ENTER, EXIT = 0.18, 0.65   # seconds: light up at once, let go over ~0.65 s and be *gone*
    FALL = 0.9       # per-frame decay of the bands (one rate now: the exit relaxes the shape too)

    def __init__(self, get_samples, scale: float = 1.0, window: bool = True):
        self.get_samples = get_samples   # () -> last ~2048 samples at 16 kHz, or None
        self.bands = np.zeros(self.BANDS, dtype=np.float32)
        self.colors = dict(COLORS)
        self.opacity = 0.9               # 0..1, from config
        self.haze = False
        self.style = "waves"             # "waves" = spectrum, "light" = one breathing light
        self.busymix = 0.0               # 0 = accent colour, 1 = transcribing colour; eased
        self.pulse_gain = 1.0            # frozen with the colour when the exit starts
        self.rng = np.random.default_rng(7)
        self.noise_phase = self.rng.uniform(0, 2 * np.pi, size=(2, 4))   # random field per side
        # asymmetry axes: each side has a few lobe centres that wander; the spectrum is laid out
        # around each centre (lows at the centre, highs outward) and the lobes are soft-OR'd
        self.axes = [[self.rng.uniform(0.2, 0.8, 3), self.rng.uniform(0, 2 * np.pi, 3),
                      self.rng.uniform(0.02, 0.06, 3)] for _ in range(2)]
        self.scale = scale
        self.w = int((self.W + 2 * self.PAD) * scale)
        self.h = int((self.H + 2 * self.PAD) * scale)
        self.q: queue.Queue = queue.Queue()
        self.state = "idle"
        self.frame = 0
        # transitions are time-based, not per-frame: this CPU drops frames and a frame-count
        # exponential then stutters *and* leaves a long dim tail. One curve, one dissolve.
        self.clock = time.monotonic       # attribute so tests can inject a clock
        self.t_last = self.clock()
        self.level = 0.0                  # 0 = idle look, 1 = active look
        self.level_from = self.level_to = 0.0
        self.level_t0, self.level_dur = self.t_last, self.EXIT
        self.ctime = 0.0                  # seconds of *active* time: colour/pulse phase
        self.phase = 0.0                  # breath phase of the light (accumulated, so the
        #                                   busy period change does not jump)
        self.lvl = self.lpeak = 0.0       # mic loudness for the light style, auto-gained
        self.last: Image.Image | None = None   # last rendered frame (also when there is no window)
        self.peak = 1.0   # running spectrum reference, so the display fills for any mic gain
        self._cache: dict = {}
        self.hwnd = None
        if window:
            self._make_window()

    def configure(self, cfg: dict) -> None:
        """Look from config: 'color' for recording/persistent, 'color_busy' for the
        transcribing pulse, opacity, haze and the indicator style. Next frame."""
        rec = hex_rgb(cfg.get("color", ""), COLORS["recording"])
        busy = hex_rgb(cfg.get("color_busy", ""), COLORS["busy"])
        self.colors.update(recording=rec, persistent=rec, busy=busy, loading=busy)
        try:
            self.opacity = min(1.0, max(0.2, float(cfg.get("opacity", 0.9))))
        except (TypeError, ValueError):
            self.opacity = 0.9
        self.haze = bool(cfg.get("haze", False))
        self.style = "light" if cfg.get("indicator") == "light" else "waves"
        if self.style == "light":
            self.bands[:] = 0   # light style never calls _analyse: a leftover shape would never
            #                     decay. Only there: Save mid-take must not flatten a live waveform.
        self._cache.clear()

    # --- window -------------------------------------------------------------
    def _make_window(self) -> None:
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        ctypes.windll.kernel32.GetModuleHandleW.restype = wt.HMODULE
        wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "murmur_overlay"
        user32.RegisterClassW(ctypes.byref(wc))
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self.x = (sw - self.w) // 2
        self.y = sh - self.h - int(40 * self.scale)
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        self.hwnd = user32.CreateWindowExW(ex, "murmur_overlay", "murmur", WS_POPUP, self.x, self.y,
                                           self.w, self.h, None, None, wc.hInstance, None)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def _blit(self, img: Image.Image) -> None:
        if not self.hwnd:
            return
        w, h = img.size
        arr = np.asarray(img).astype(np.uint16)           # RGBA straight alpha
        a = arr[..., 3:4]
        pre = (arr[..., :3] * a // 255).astype(np.uint8)  # UpdateLayeredWindow wants premultiplied
        bgra = np.concatenate([pre[..., 2:3], pre[..., 1:2], pre[..., 0:1], a.astype(np.uint8)], axis=2)
        px = bytearray(np.ascontiguousarray(bgra).tobytes())
        buf = (ctypes.c_char * len(px)).from_buffer(px)
        hbmp = gdi32.CreateBitmap(w, h, 1, 32, buf)
        hdc_screen = user32.GetDC(None)
        hdc = gdi32.CreateCompatibleDC(hdc_screen)
        old = gdi32.SelectObject(hdc, hbmp)
        pos = wt.POINT(self.x, self.y)
        size = wt.SIZE(w, h)
        src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, hdc_screen, ctypes.byref(pos), ctypes.byref(size), hdc,
                                   ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, hdc_screen)

    # --- drawing ------------------------------------------------------------
    def _geom(self):
        S = self.SS * self.scale
        n_w, n_h = self.w * self.SS, self.h * self.SS
        x0, y0 = self.PAD * S, self.PAD * S
        x1, y1 = x0 + self.W * S, y0 + self.H * S
        return n_w, n_h, x0, y0, x1, y1, (y1 - y0) / 2

    def _stick_mask(self) -> Image.Image:
        n_w, n_h, x0, y0, x1, y1, r = self._geom()
        m = Image.new("L", (n_w, n_h), 0)
        ImageDraw.Draw(m).rounded_rectangle((x0, y0, x1, y1), radius=r, fill=255)
        return m

    # --- spectrum ----------------------------------------------------------------
    def _analyse(self, x) -> None:
        """Update self.bands from raw samples: FFT, log-spaced bands, auto-gain, neighbour
        spreading (each band lifts its neighbours by 1/1.6^distance so the outline is one
        flowing shape, not fence posts), then fast attack / slow fall."""
        if x is None or len(x) < 512:
            target = np.zeros(self.BANDS, dtype=np.float32)
        else:
            x = np.asarray(x, dtype=np.float32)
            x = x - x.mean()
            spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            df = 16000 / len(x)
            edges = np.geomspace(90, 5500, self.BANDS + 1) / df
            target = np.zeros(self.BANDS, dtype=np.float32)
            for i in range(self.BANDS):
                lo, hi = int(edges[i]), max(int(edges[i]) + 1, int(edges[i + 1]))
                target[i] = spec[lo:hi].mean()
            target = np.log1p(target * 4.0) * (1 + 0.6 * np.linspace(0, 1, self.BANDS))  # lift the highs a little
            self.peak = max(float(target.max()), self.peak * 0.994, 0.5)
            target = np.clip(target / self.peak, 0, 1) ** 1.3
            spread = target.copy()
            for d in range(1, 5):
                f = 1.6 ** d
                spread[d:] = np.maximum(spread[d:], target[:-d] / f)
                spread[:-d] = np.maximum(spread[:-d], target[d:] / f)
            target = spread
        rising = target > self.bands
        self.bands = np.where(rising, self.bands * 0.35 + target * 0.65, self.bands * self.FALL)

    def _spectrum_mask(self, amp: float) -> Image.Image:
        """One silhouette: the mirrored, smoothed band heights become the half-height of the
        stick at each x; lows in the middle, highs fading into the tips."""
        n_w, n_h, x0, y0, x1, y1, r = self._geom()
        S = self.SS * self.scale
        cy = (y0 + y1) / 2
        N = 200
        k = np.exp(-0.5 * (np.arange(-8, 9) / 3.0) ** 2); k /= k.sum()
        taper = np.sin(np.linspace(0, np.pi, N)) ** 0.35     # still meets the tips

        def profile(b):
            pr = np.interp(np.linspace(0, len(b) - 1, N), np.arange(len(b)), b)
            pr = np.convolve(np.pad(pr, 8, mode="edge"), k, mode="valid")
            return pr * taper

        # organic asymmetry: the mirrored spectrum (lows centre) is modulated by a smooth random
        # field - a few sines with random phases, drifting slowly - different for top and bottom,
        # so the shape is never the same twice and never favours one side
        t = self.frame / 25.0
        u = np.linspace(0, 1, N)

        def field(side):
            ph = self.noise_phase[side]
            f = (np.sin(2 * np.pi * (u * 1.3 + 0.11 * t) + ph[0]) + 0.7 * np.sin(2 * np.pi * (u * 2.7 - 0.07 * t) + ph[1])
                 + 0.5 * np.sin(2 * np.pi * (u * 4.1 + 0.05 * t) + ph[2]) + 0.4 * np.sin(2 * np.pi * (u * 0.6 - 0.13 * t) + ph[3]))
            return 0.72 + 0.28 * f / 2.6

        def lobes(side):
            """Profile over x from several wandering axes. Each axis k sits at c_k(t) and spans
            w_k; the band curve is read by distance from the axis. Combined as 1-prod(1-v)."""
            centres, phases, speeds = self.axes[side]
            acc = np.ones(N, dtype=np.float32)
            widths = (0.55, 0.38, 0.3)
            gains = (1.0, 0.8, 0.65)
            for k in range(3):
                c = centres[k] + 0.22 * np.sin(2 * np.pi * speeds[k] * t + phases[k]) \
                    + 0.08 * np.sin(2 * np.pi * speeds[k] * 2.7 * t + phases[k] * 1.7)
                d = np.clip(np.abs(u - c) / widths[k], 0, 1)
                v = np.interp(d * (len(self.bands) - 1), np.arange(len(self.bands)), self.bands) * gains[k]
                v *= (1 - d) ** 0.35                              # fade each lobe at its rim
                acc *= 1 - np.clip(v, 0, 1)
            pr = 1 - acc
            pr = np.convolve(np.pad(pr, 8, mode="edge"), k_, mode="valid")
            return pr * taper

        k_ = k
        shape = lobes(0) * field(0)       # one set of axes for both halves: they move together
        top_p = shape
        bot_p = shape * 0.85
        hmax = min(self.H * S * 4.0, (self.PAD - 3) * S) * amp
        # fixed-size bars: NBARS at a fixed pitch across the container, each a rounded bar whose
        # top and bottom follow the profile at its centre; the container (stick) stays underneath
        m = Image.new("L", (n_w, n_h), 0)
        d = ImageDraw.Draw(m)
        d.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=255)
        inner_w = (x1 - x0) - 2 * r * 0.9
        pitch = inner_w / self.NBARS
        bw = pitch * 0.52                                        # half-width just over the pitch: bars fuse, no gaps
        idx = np.linspace(0, N - 1, self.NBARS * 2 + 1)[1::2]    # profile sample at each bar centre
        for i in range(self.NBARS):
            xc = x0 + r * 0.9 + pitch * (i + 0.5)
            ht = r + hmax * float(np.interp(idx[i], np.arange(N), top_p))
            hb = r + hmax * float(np.interp(idx[i], np.arange(N), bot_p))
            d.rounded_rectangle((xc - bw, cy - ht, xc + bw, cy + hb), radius=bw * 0.6, fill=255)
        return m.filter(ImageFilter.GaussianBlur(0.6 * S))

    @staticmethod
    def _lerp(a, b, m):
        return tuple(int(a[i] + (b[i] - a[i]) * m) for i in range(3))

    def _live_color(self, state: str):
        """Accent colour, breathing slowly while recording, crossfading to the transcribing
        colour as busymix rises."""
        acc = self.colors["recording"]
        light = tuple(min(255, int(c * 0.6 + 255 * 0.4)) for c in acc)
        breath = 0.5 + 0.5 * np.sin(2 * np.pi * self.ctime * 0.45)
        acc = self._lerp(acc, light, 0.18 * breath)
        return self._lerp(acc, self.colors["busy"], self.busymix)

    def _fill(self, img: Image.Image, mask: Image.Image, col) -> None:
        """Glowing one-piece fill: bright, lighter colour along the centre line fading to the
        state colour at the edge, a soft outer glow, and a thin bright rim. No layer is scaled
        by the transition any more - _dissolve fades glow, body and rim together."""
        n_w, n_h = mask.size
        S = self.SS * self.scale
        _, _, x0, y0, x1, y1, r = self._geom()
        cy = (y0 + y1) / 2
        m = np.asarray(mask, dtype=np.float32) / 255
        # distance from the centre line, normalised per column by the shape's half-height there
        lit = m > 0.5
        ys = np.arange(n_h, dtype=np.float32)[:, None]
        above = np.maximum((lit & (ys < cy)).sum(axis=0), 1.0)[None, :]
        below = np.maximum((lit & (ys >= cy)).sum(axis=0), 1.0)[None, :]
        dist = np.where(ys < cy, (cy - ys) / above, (ys - cy) / below)
        dist = np.clip(dist, 0, 1)
        light = np.array([min(255, c * 0.35 + 255 * 0.65) for c in col], np.float32)
        base = np.array(col, np.float32)
        rgb = light[None, None, :] * (1 - dist[..., None]) ** 1.6 + base[None, None, :] * (1 - (1 - dist[..., None]) ** 1.6)
        alpha = (255 * self.opacity * (1 - 0.3 * dist ** 2) * m)[..., None]
        body = Image.fromarray(np.concatenate([rgb, alpha], axis=2).astype(np.uint8), "RGBA")
        glow = mask.filter(ImageFilter.GaussianBlur(4 * S)).point(lambda v: min(255, v * 1.8) * 0.6 * self.opacity)
        g = Image.new("RGBA", (n_w, n_h), col + (255,))
        g.putalpha(glow)
        img.alpha_composite(g)
        if self.haze:
            hz = mask.filter(ImageFilter.GaussianBlur(12 * S)).point(lambda v: min(255, v * 2.4) * 0.45 * self.opacity)
            h = Image.new("RGBA", (n_w, n_h), col + (255,))
            h.putalpha(hz)
            img.alpha_composite(h)
        img.alpha_composite(body)
        edge = np.asarray(mask.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        rim = Image.new("RGBA", (n_w, n_h), (255, 255, 255, 0))
        rim.putalpha(Image.fromarray(np.clip(edge * 0.7 * self.opacity, 0, 160).astype(np.uint8), "L"))
        img.alpha_composite(rim)

    def _glass(self, mask: Image.Image) -> Image.Image:
        """Shadow + frosted glass body for a shape mask. Only the resting look comes through
        here now: every active state is drawn by _fill (waves) or _light, so the old tinted /
        glowing half of this (and _base's state argument) is gone."""
        n_w, n_h = mask.size
        S = self.SS * self.scale
        img = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        # shadow below
        # (blur the single-channel mask, then colour it: 4x cheaper than blurring RGBA)
        shm = Image.new("L", (n_w, n_h), 0)
        shm.paste(mask, (0, int(2 * S)))
        shm = shm.filter(ImageFilter.GaussianBlur(3 * S)).point(lambda v: v * 0.28)
        sh = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 255))
        sh.putalpha(shm)
        img.alpha_composite(sh)
        # body: a frosted, whitish glass with a fine grain
        body = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        grain = self.rng.normal(0, 1, (n_h, n_w))
        grain = Image.fromarray(np.clip(128 + grain * 22, 0, 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(0.5))
        frost = Image.new("RGBA", (n_w, n_h), (240, 240, 245, 0))
        frost.putalpha(Image.fromarray((np.asarray(grain, dtype=np.float32) * 0.9 * self.opacity).astype(np.uint8), "L"))
        body.paste(frost, (0, 0), mask)
        if self.haze:
            hz = mask.filter(ImageFilter.GaussianBlur(11 * S)).point(lambda v: min(255, v * 2.2) * 0.22 * self.opacity)
            h = Image.new("RGBA", (n_w, n_h), (245, 245, 250, 255))
            h.putalpha(hz)
            img.alpha_composite(h)
        # specular: a blurred bright band across the upper part of the shape
        arr = np.asarray(mask, dtype=np.float32) / 255
        ys = np.arange(n_h, dtype=np.float32)[:, None]
        top = np.argmax(arr > 0.5, axis=0).astype(np.float32)[None, :]           # first lit row per column
        height = (arr > 0.5).sum(axis=0).astype(np.float32)[None, :] + 1e-3
        band = np.clip(1 - (ys - top) / (0.45 * height), 0, 1) * arr
        hl = Image.fromarray((band * 70 * self.opacity).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.0 * S))
        spec = Image.new("RGBA", (n_w, n_h), (255, 255, 255, 0))
        spec.putalpha(hl)
        body.alpha_composite(spec)
        # rim: the mask's edge, brighter on top
        edge = np.asarray(mask.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        rim = Image.new("RGBA", (n_w, n_h), (255, 255, 255, 0))
        rim.putalpha(Image.fromarray(np.clip(edge * 0.35 * self.opacity, 0, 150).astype(np.uint8), "L"))
        body.alpha_composite(rim)
        img.alpha_composite(body)
        return img

    def _base(self) -> Image.Image:
        """The resting stick. Static, so it is built once per configure() and cached."""
        if "idle" not in self._cache:
            self._cache["idle"] = self._glass(self._stick_mask())
        return self._cache["idle"]

    def _pulse(self, img, alpha_scale: float = 1.0) -> None:
        """Orange-to-yellow gradient sweeping along the stick, breathing in brightness."""
        n_w, n_h, x0, y0, x1, y1, r = self._geom()
        S = self.SS * self.scale
        t = self.ctime
        xs = np.linspace(0, 1, n_w, dtype=np.float32)
        wave = 0.5 + 0.5 * np.sin(2 * np.pi * (xs * 1.5 - t * 0.8))
        base = self.colors["busy"]
        orange = np.array(base, np.float32)
        yellow = np.array([min(255, c * 0.55 + 255 * 0.45) for c in base], np.float32)
        row = orange[None, :] * (1 - wave[:, None]) + yellow[None, :] * wave[:, None]
        breathe = 0.65 + 0.35 * np.sin(2 * np.pi * t * 0.9)
        a = np.full((n_w, 1), int(190 * breathe * alpha_scale * self.opacity), np.float32)
        rgba = np.concatenate([row, a], axis=1).astype(np.uint8)
        grad = Image.fromarray(np.broadcast_to(rgba[None, :, :], (n_h, n_w, 4)).copy(), "RGBA")
        mask = Image.new("L", (n_w, n_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((x0 + 1.2 * S, y0 + 1.2 * S, x1 - 1.2 * S, y1 - 1.2 * S),
                                              radius=r, fill=255)
        grad.putalpha(Image.composite(grad.split()[3], Image.new("L", (n_w, n_h), 0), mask))
        img.alpha_composite(grad)

    def _light(self, col) -> Image.Image:
        """style=light: the resting stick with an LED behind the glass - a small disc at the
        centre, a halo and a wider bloom that spills past the bar. Breathes slowly (2.2 s, 0.9 s
        while transcribing) and brightens with the mic level, so speaking lights it up."""
        n_w, n_h, x0, y0, x1, y1, r = self._geom()
        S = self.SS * self.scale
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        breath = 0.5 + 0.5 * float(np.sin(self.phase))
        b = min(1.0, 0.55 + 0.45 * breath + 0.35 * self.lvl) * self.opacity
        # only the scalar b changes between frames: the two blurs and the mgrid distance field
        # cost ~10 ms a frame, so build the geometry once (colour-independent) and per frame
        # only tint and scale it. configure() clears _cache, which is where opacity/scale change.
        if "led" not in self._cache:
            def disc(d, blur):
                m = Image.new("L", (n_w, n_h), 0)
                ImageDraw.Draw(m).ellipse((cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2), fill=255)
                return m.filter(ImageFilter.GaussianBlur(blur))

            rad = 0.4 * self.H * S
            ys, xs = np.mgrid[0:n_h, 0:n_w].astype(np.float32)
            d = np.clip(np.hypot(xs - cx, ys - cy) / rad, 0, 1)[..., None]
            self._cache["led"] = (disc(1.8 * self.H * S, 7 * S),   # bloom, well past the bar
                                  disc(1.8 * self.H * S, 3 * S),   # halo
                                  (1 - d) ** 1.6,
                                  np.clip((1 - d) * rad, 0, 1) * 255)   # 1 px feather at the rim
        bloom, halo, f, a0 = self._cache["led"]

        def lit(m, alpha):
            lay = Image.new("RGBA", (n_w, n_h), col + (255,))
            lay.putalpha(m.point(lambda v: v * alpha * b))
            return lay

        img = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        img.alpha_composite(lit(bloom, 0.35))
        img.alpha_composite(lit(halo, 0.7))
        img.alpha_composite(self._base())                          # the glass sits over both
        # the LED itself: lighter core fading to the accent at its rim (the _fill formula)
        light = np.array([min(255, c * 0.35 + 255 * 0.65) for c in col], np.float32)
        rgb = light * f + np.array(col, np.float32) * (1 - f)
        img.alpha_composite(Image.fromarray(np.concatenate([rgb, a0 * b], 2).astype(np.uint8), "RGBA"))
        return img

    @staticmethod
    def _dissolve(idle: Image.Image, active: Image.Image, level: float) -> Image.Image:
        """Cross-fade idle -> active in PREMULTIPLIED alpha. Straight-alpha blending (what
        Image.blend does) drags the colour of transparent pixels into the mix and steps the
        alpha; here every layer of the active image - glow, haze, body, rim, pulse - fades
        together under this one factor."""
        if level <= 0.0:
            return idle
        if level >= 1.0:
            return active
        a = np.asarray(idle, dtype=np.float32)
        b = np.asarray(active, dtype=np.float32)
        prem = (a[..., :3] * a[..., 3:4] * (1 - level) + b[..., :3] * b[..., 3:4] * level) / 255.0
        al = a[..., 3:4] * (1 - level) + b[..., 3:4] * level
        rgb = np.divide(prem * 255.0, al, out=np.zeros_like(prem), where=al > 0)   # un-premultiply
        return Image.fromarray(np.concatenate([np.clip(rgb, 0, 255), al], 2).astype(np.uint8), "RGBA")

    def _render(self) -> Image.Image:
        """One renderer per look: idle is the resting glass, *every* active state is the lit
        stick (waves) or the light, and `level` dissolves between the two. No renderer swap
        mid-transition, so nothing can jump."""
        idle = self._base()
        img = idle
        if self.level > 0.0:
            col = self._live_color(self.state)
            if self.style == "light":
                act = self._light(col)
            else:
                # with the bands relaxed lobes() is 0 everywhere, so the mask no longer depends
                # on `frame`; once level and the colour have settled the fill is bit-identical
                # frame to frame. That is the whole model load and everything past the first
                # ~2 s of a take, and _fill's per-pixel float32 work is ~17 ms a frame on a CPU
                # already busy with whisper - so keep one slot and re-sweep only the pulse.
                key = (col, self.haze)
                slot = self._cache.get("fill")
                static = self.level >= 1.0 and self.bands.max() == 0.0
                if static and slot is not None and slot[0] == key:
                    act = slot[1].copy()
                else:
                    act = Image.new("RGBA", (self.w * self.SS, self.h * self.SS), (0, 0, 0, 0))
                    self._fill(act, self._spectrum_mask(self.level), col)   # bands at 0 -> plain stick
                    if static:
                        self._cache["fill"] = (key, act.copy())
                if self.busymix > 0.02:
                    self._pulse(act, self.busymix * self.pulse_gain)
            img = self._dissolve(idle, act, self.level)
        return img.reduce(self.SS)  # box filter: the 2x supersample already did the anti-aliasing

    # --- API ----------------------------------------------------------------
    def post(self, state: str) -> None:
        self.q.put(state)

    def _mic_level(self, x) -> None:
        """Mic loudness in 0..1 for the light style: RMS auto-gained against a decaying peak,
        fast attack / slow release."""
        rms = 0.0
        if x is not None and len(x) >= 512:
            rms = float(np.sqrt(np.mean(np.square(np.asarray(x, dtype=np.float32)))))
            if not np.isfinite(rms):
                rms = 0.0
        self.lpeak = max(rms, self.lpeak * 0.995, 0.01)
        raw = min(1.0, max(0.0, rms / self.lpeak))
        self.lvl = self.lvl * 0.5 + raw * 0.5 if raw > self.lvl else self.lvl * 0.9

    def tick(self) -> None:
        while not self.q.empty():
            self.state = self.q.get()
        now = self.clock()
        dt = min(0.2, max(0.0, now - self.t_last))     # a dropped frame must not jump the eases
        self.t_last = now
        to = 0.0 if self.state == "idle" else 1.0
        if to != self.level_to:                        # new segment, from wherever we are now
            self.level_from, self.level_to = self.level, to
            self.level_t0, self.level_dur = now, self.ENTER if to else self.EXIT
        x = min(1.0, max(0.0, (now - self.level_t0) / self.level_dur))
        e = 1 - (1 - x) ** 3 if self.level_to else x * x * (3 - 2 * x)   # ease-out in, smoothstep out
        self.level = self.level_from + (self.level_to - self.level_from) * e
        self.frame += 1
        if self.state != "idle":
            # colour, pulse and breath advance only while active: during the exit they are frozen
            # and only `level` moves, so the fade cannot drift back towards the accent colour
            self.ctime += dt
            self.phase += 2 * np.pi * dt / (2.2 - 1.3 * self.busymix)
            self.pulse_gain = 1.0 if self.state == "busy" else 0.6
            bm = 1.0 if self.state in ("busy", "loading") else 0.0
            tau = (0.25 if bm > self.busymix else 0.30) / 3      # ~95% there in 250 / 300 ms
            self.busymix += (bm - self.busymix) * (1 - np.exp(-dt / tau))
        elif self.level <= 0.0:
            self.busymix = 0.0                         # the take is over: forget its colour
            self.bands[:] = 0                          # and its shape
        if self.style == "light":
            if self.state in ("recording", "persistent"):
                self._mic_level(self.get_samples())    # no spectrum analysed in light style
            elif self.state != "idle":
                self.lvl *= 0.9                        # settle back to the plain breath
        elif self.state in ("recording", "persistent"):
            self._analyse(self.get_samples())
        elif self.bands.max() > 0.01:
            self._analyse(None)                        # keeps relaxing at fall 0.9 while it fades
            if self.bands.max() <= 0.01:
                self.bands[:] = 0                      # under a third of a pixel of bar: settle
                #   exactly rather than leave a residue that stops decaying (tick would no longer
                #   call _analyse), so the relaxed silhouette stops depending on `frame` at all
        if self.state == "idle" and self.level <= 0.0 and self.bands.max() <= 0.01 and self.busymix < 0.02 and self.frame % 10:
            return  # idle look is static: no need to redraw every frame
        self.last = self._render()
        if self.hwnd:
            self._blit(self.last)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def close(self) -> None:
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
