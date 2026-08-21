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
from collections import deque

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

COLORS = {"idle": (220, 220, 230), "recording": (235, 60, 60), "persistent": (245, 150, 40),
          "busy": (255, 170, 50), "loading": (255, 170, 50)}

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
    """A small glass stick at the bottom centre. Near-invisible at rest. While recording it
    turns red (amber in persistent mode) and a bundle of thin neon curves flows out of it,
    mirrored about the centre line: the voice level sets the envelope (newest at the right,
    scrolling left), several sine strands ride inside it, and everything pinches back to
    the stick at both ends. An orange-to-yellow pulse runs along it while transcribing.
    Call tick() from the UI thread's timer (~25 fps); post(state) from any thread."""

    W, H = 48, 7     # stick size in logical px
    PAD = 22         # headroom for the waveform, glow and shadow
    POINTS = 40      # level samples across the stick (the envelope)
    STRANDS = 6      # curves per side
    SS = 2           # supersampling

    def __init__(self, get_level, scale: float = 1.0):
        self.get_level = get_level
        self.scale = scale
        self.w = int((self.W + 2 * self.PAD) * scale)
        self.h = int((self.H + 2 * self.PAD) * scale)
        self.q: queue.Queue = queue.Queue()
        self.state = "idle"
        self.hist = deque([0.0] * self.POINTS, maxlen=self.POINTS)
        self.frame = 0
        self.anim = 0.0  # 0 = idle look, 1 = active look; eased per frame
        self.peak = 0.02  # running loudness reference, so the display fills for any mic gain
        self._cache: dict = {}
        self._make_window()

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

    def _waves(self, img: Image.Image, col, amp: float) -> None:
        """A bundle of thin glowing curves inside a voice-driven envelope, mirrored top/bottom."""
        n_w, n_h, x0, y0, x1, y1, r = self._geom()
        S = self.SS * self.scale
        cy = (y0 + y1) / 2
        # envelope: level history (oldest left .. newest right), auto-gained, smoothed, pinched at the ends
        lv = np.array(self.hist, dtype=np.float32)
        env = np.clip((lv - 0.002) / max(self.peak, 0.004), 0, 1) ** 0.7
        N = 160
        xs = np.linspace(x0 + r * 0.6, x1 - r * 0.6, N)
        env = np.interp(np.linspace(0, len(env) - 1, N), np.arange(len(env)), env)
        k = np.exp(-0.5 * (np.arange(-10, 11) / 4.0) ** 2); k /= k.sum()
        env = np.convolve(np.pad(env, 10, mode="edge"), k, mode="valid")
        env *= np.sin(np.linspace(0, np.pi, N)) ** 0.6           # contained: flat at both ends
        hmax = min(self.H * S * 4.0, (self.PAD - 3) * S) * amp   # never past the window edge
        t = self.frame / 25.0
        u = np.linspace(0, 1, N)
        layer = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        light = tuple(min(255, int(c * 0.45 + 255 * 0.55)) for c in col)
        for i in range(self.STRANDS):
            f = i % 3
            # each strand: a slow travelling ripple (different speed/frequency) inside the envelope
            ripple = 0.6 + 0.4 * np.sin(2 * np.pi * (u * (0.8 + 0.45 * f) - t * (0.3 + 0.12 * i)) + i * 1.1)
            y = hmax * env * ripple * (0.55 + 0.45 * (i + 1) / self.STRANDS)
            mix = i / max(1, self.STRANDS - 1)
            c = tuple(int(col[j] * (1 - mix) + light[j] * mix) for j in range(3))
            width = max(1, int((1.3 - 0.12 * i) * S))
            for sign in (1, -1):
                pts = [(float(x), float(cy + sign * yy)) for x, yy in zip(xs, y)]
                d.line(pts, fill=c + (170,), width=width, joint="curve")
        glow = layer.split()[3].filter(ImageFilter.GaussianBlur(2.5 * S)).point(lambda v: min(255, v * 2.0) * 0.5)
        g = Image.new("RGBA", (n_w, n_h), col + (255,))
        g.putalpha(glow)
        img.alpha_composite(g)
        img.alpha_composite(layer)
        # bright centre strand reads as the neon core
        core = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        dc = ImageDraw.Draw(core)
        y = hmax * env * (0.6 + 0.4 * np.sin(2 * np.pi * (u * 1.3 - t * 0.4))) * 0.95
        for sign in (1, -1):
            dc.line([(float(x), float(cy + sign * yy)) for x, yy in zip(xs, y)], fill=(255, 255, 255, 140),
                    width=max(1, int(0.6 * S)), joint="curve")
        img.alpha_composite(core)

    def _glass(self, mask: Image.Image, col, active: float, tint_idle: bool) -> Image.Image:
        """Shadow + glow + glass body for an arbitrary shape mask."""
        n_w, n_h = mask.size
        S = self.SS * self.scale
        img = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        # shadow below, glow around when active
        # (blur the single-channel mask, then colour it: 4x cheaper than blurring RGBA)
        shm = Image.new("L", (n_w, n_h), 0)
        shm.paste(mask, (0, int(2 * S)))
        shm = shm.filter(ImageFilter.GaussianBlur(3 * S)).point(lambda v: v * (0.28 + 0.2 * active))
        sh = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 255))
        sh.putalpha(shm)
        img.alpha_composite(sh)
        if active > 0.05 and not tint_idle:
            glm = mask.filter(ImageFilter.GaussianBlur(5 * S)).point(lambda v: min(255, v * 1.6) * 0.5 * active)
            gl = Image.new("RGBA", (n_w, n_h), col + (255,))
            gl.putalpha(glm)
            img.alpha_composite(gl)
        # body
        tint = (220, 220, 230) if tint_idle else tuple(int(c * active + 200 * (1 - active)) for c in col)
        body = Image.new("RGBA", (n_w, n_h), (0, 0, 0, 0))
        body.paste(tint + (30 if tint_idle else int(30 + 95 * active),), (0, 0), mask)
        # specular: a blurred bright band across the upper part of the shape
        hl = Image.new("L", (n_w, n_h), 0)
        arr = np.asarray(mask, dtype=np.float32) / 255
        ys = np.arange(n_h, dtype=np.float32)[:, None]
        top = np.argmax(arr > 0.5, axis=0).astype(np.float32)[None, :]           # first lit row per column
        height = (arr > 0.5).sum(axis=0).astype(np.float32)[None, :] + 1e-3
        band = np.clip(1 - (ys - top) / (0.45 * height), 0, 1) * arr
        hl = Image.fromarray((band * 120).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.0 * S))
        spec = Image.new("RGBA", (n_w, n_h), (255, 255, 255, 0))
        spec.putalpha(hl)
        body.alpha_composite(spec)
        # rim: the mask's edge, brighter on top
        edge = np.asarray(mask.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        rim = Image.new("RGBA", (n_w, n_h), (255, 255, 255, 0))
        rim.putalpha(Image.fromarray(np.clip(edge * (0.5 if tint_idle else 0.5 + 0.3 * active), 0, 150).astype(np.uint8), "L"))
        body.alpha_composite(rim)
        img.alpha_composite(body)
        return img

    def _base(self, state: str, active: float) -> Image.Image:
        """Static stick look, cached per (state, step). Recording draws per frame instead."""
        key = (state, round(active, 1))
        if key in self._cache:
            return self._cache[key]
        img = self._glass(self._stick_mask(), COLORS[state], active, state == "idle")
        if len(self._cache) > 40:
            self._cache.clear()
        self._cache[key] = img
        return img

    def _pulse(self, img, alpha_scale: float = 1.0) -> None:
        """Orange-to-yellow gradient sweeping along the stick, breathing in brightness."""
        n_w, n_h, x0, y0, x1, y1, r = self._geom()
        S = self.SS * self.scale
        t = self.frame / 25.0
        xs = np.linspace(0, 1, n_w, dtype=np.float32)
        wave = 0.5 + 0.5 * np.sin(2 * np.pi * (xs * 1.5 - t * 0.8))
        orange = np.array([255, 140, 30], np.float32)
        yellow = np.array([255, 225, 90], np.float32)
        row = orange[None, :] * (1 - wave[:, None]) + yellow[None, :] * wave[:, None]
        breathe = 0.65 + 0.35 * np.sin(2 * np.pi * t * 0.9)
        a = np.full((n_w, 1), int(170 * breathe * alpha_scale), np.float32)
        rgba = np.concatenate([row, a], axis=1).astype(np.uint8)
        grad = Image.fromarray(np.broadcast_to(rgba[None, :, :], (n_h, n_w, 4)).copy(), "RGBA")
        mask = Image.new("L", (n_w, n_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((x0 + 1.2 * S, y0 + 1.2 * S, x1 - 1.2 * S, y1 - 1.2 * S),
                                              radius=r, fill=255)
        grad.putalpha(Image.composite(grad.split()[3], Image.new("L", (n_w, n_h), 0), mask))
        img.alpha_composite(grad)

    def _render(self) -> Image.Image:
        active = self.anim
        if self.state in ("recording", "persistent"):
            img = self._base(self.state, active).copy()
            self._waves(img, COLORS[self.state], active)
        else:
            img = self._base(self.state, active).copy()
            if self.state in ("busy", "loading"):
                self._pulse(img, 1.0 if self.state == "busy" else 0.6)
        return img.reduce(self.SS)  # box filter: the 2x supersample already did the anti-aliasing

    # --- API ----------------------------------------------------------------
    def post(self, state: str) -> None:
        self.q.put(state)

    def tick(self) -> None:
        while not self.q.empty():
            self.state = self.q.get()
            self.hist.extend([0.0] * self.POINTS)
        target = 0.0 if self.state == "idle" else 1.0
        self.anim += (target - self.anim) * 0.25
        self.frame += 1
        if self.state in ("recording", "persistent"):
            lvl = self.get_level()
            self.hist.append(lvl)
            # auto-gain: jump up to a new peak, decay slowly (~4 s to halve) when it goes quiet
            self.peak = max(lvl, self.peak * 0.993, 0.004)
        elif self.anim < 0.99:
            self.hist.append(0.0)
        if self.state == "idle" and self.anim < 0.01 and self.frame % 10:
            return  # idle look is static: no need to redraw every frame
        self._blit(self._render())
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def close(self) -> None:
        user32.DestroyWindow(self.hwnd)
