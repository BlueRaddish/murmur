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

COLORS = {"idle": (150, 150, 160), "recording": (235, 70, 70), "persistent": (245, 160, 40),
          "busy": (80, 140, 240), "loading": (80, 140, 240)}

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
    """Call tick() from the UI thread's timer (~30 fps); post(state) from any thread."""

    BASE = 72        # disc diameter in logical px
    PAD = 28         # room for glow + shadow
    BARS = 7
    SS = 2           # supersampling

    def __init__(self, get_level, scale: float = 1.0):
        self.get_level = get_level
        self.scale = scale
        self.size = int((self.BASE + 2 * self.PAD) * scale)
        self.q: queue.Queue = queue.Queue()
        self.state = "idle"
        self.hist = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.frame = 0
        self.anim = 0.0  # 0 = idle look, 1 = active look; eased per frame
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
        self.x = (sw - self.size) // 2
        self.y = sh - self.size - int(36 * self.scale)
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        self.hwnd = user32.CreateWindowExW(ex, "murmur_overlay", "murmur", WS_POPUP, self.x, self.y,
                                           self.size, self.size, None, None, wc.hInstance, None)
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
    def _base(self, state: str, active: float) -> Image.Image:
        """Glow + shadow + glass disc. Blurs are slow, so this is cached per (state, active step)."""
        key = (state, round(active, 1))
        if key in self._cache:
            return self._cache[key]
        S = self.SS
        n = self.size * S
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        c = n / 2
        col = COLORS[state]
        disc_r = self._disc_r(active)

        glow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((c - disc_r * 1.15, c - disc_r * 1.15, c + disc_r * 1.15, c + disc_r * 1.15),
                                     fill=col + (int(40 + 110 * active),))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(self.PAD * self.scale * S * 0.45)))

        sh = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        off = 4 * self.scale * S
        ImageDraw.Draw(sh).ellipse((c - disc_r, c - disc_r + off, c + disc_r, c + disc_r + off), fill=(0, 0, 0, 120))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(6 * self.scale * S)))

        disc = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(disc)
        d.ellipse((c - disc_r, c - disc_r, c + disc_r, c + disc_r), fill=(24, 24, 30, int(170 + 60 * active)))
        hl = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        ImageDraw.Draw(hl).ellipse((c - disc_r * 0.8, c - disc_r * 0.95, c + disc_r * 0.8, c - disc_r * 0.1),
                                   fill=(255, 255, 255, 70))
        hl = hl.filter(ImageFilter.GaussianBlur(disc_r * 0.25))
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).ellipse((c - disc_r, c - disc_r, c + disc_r, c + disc_r), fill=255)
        hl.putalpha(Image.composite(hl.split()[3], Image.new("L", (n, n), 0), mask))
        disc.alpha_composite(hl)
        d.ellipse((c - disc_r, c - disc_r, c + disc_r, c + disc_r), outline=(255, 255, 255, 45),
                  width=int(1.5 * S * self.scale))
        img.alpha_composite(disc)
        if len(self._cache) > 40:
            self._cache.clear()
        self._cache[key] = img
        return img

    def _disc_r(self, active: float) -> float:
        return (self.BASE / 2) * self.scale * self.SS * (0.78 + 0.22 * active)

    def _render(self) -> Image.Image:
        active = self.anim
        img = self._base(self.state, active).copy()
        c = img.size[0] / 2
        col = COLORS[self.state]
        disc_r = self._disc_r(active)
        d = ImageDraw.Draw(img)
        if self.state in ("recording", "persistent"):
            bw = disc_r * 0.055          # half-width of a bar
            gap = disc_r * 0.19
            x0 = c - gap * (self.BARS - 1) / 2
            for i, lvl in enumerate(self.hist):
                # loudness -> height, curved so quiet speech still shows; 0.12 RMS ~ loud
                h = disc_r * (0.16 + 1.0 * min(1.0, lvl / 0.12) ** 0.6)
                x = x0 + gap * i
                d.rounded_rectangle((x - bw, c - h / 2, x + bw, c + h / 2), radius=bw, fill=col + (235,))
        elif self.state in ("busy", "loading"):
            ang = (self.frame * 9) % 360
            r = disc_r * 0.5
            d.arc((c - r, c - r, c + r, c + r), start=ang, end=ang + 100, fill=col + (230,),
                  width=int(disc_r * 0.1))
        else:  # idle: a small quiet dot
            r = disc_r * 0.1
            d.ellipse((c - r, c - r, c + r, c + r), fill=col + (140,))
        return img.reduce(self.SS)  # box filter: the 2x supersample already did the anti-aliasing

    # --- API ----------------------------------------------------------------
    def post(self, state: str) -> None:
        self.q.put(state)

    def tick(self) -> None:
        while not self.q.empty():
            self.state = self.q.get()
            self.hist.extend([0.0] * self.BARS)
        target = 0.0 if self.state == "idle" else 1.0
        self.anim += (target - self.anim) * 0.25
        self.frame += 1
        if self.state in ("recording", "persistent"):
            self.hist.append(self.get_level())
        if self.state == "idle" and abs(self.anim) < 0.01 and self.frame % 10:
            return  # idle look is static: no need to redraw every frame
        self._blit(self._render())
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def close(self) -> None:
        user32.DestroyWindow(self.hwnd)
