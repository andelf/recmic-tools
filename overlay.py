"""
Always-on-top translucent recording indicator.

A frosted "pill" that sits near the bottom-centre of the monitor hosting the
foreground window.  While the user holds the record key its bars dance with the
live microphone level; while the audio is being transcribed they switch to a
travelling wave; when the transcript lands the pill flashes green and fades out.

Implementation notes
--------------------
* Plain tkinter for the message pump — no extra dependency, starts in ~50 ms.
  The window itself carries no widgets: its pixels are supplied by
  ``UpdateLayeredWindow`` from a 32-bit premultiplied ARGB DIB.
* **Per-pixel alpha, not a window region.**  ``SetWindowRgn`` clips to a binary
  region, and Windows never anti-aliases a region — that is why the earlier
  build had visible stair-steps on the rounded ends.  A layered window lets us
  hand Windows an alpha channel, so the pill's rim is a real 8-bit gradient.
* The shape is rasterised from a rounded-box SDF with a smoothstep ramp, so the
  edge is not merely 1 px anti-aliased but genuinely soft (``EDGE_FEATHER``).
* ``WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`` makes it click-through and
  focus-stealing-free, so it never disturbs whatever the user is typing into.
* All Win32 calls are made through ctypes with explicit prototypes, because
  HWNDs/HDCs are pointer-sized and the default ``c_int`` conversion truncates
  them on 64-bit Windows.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import time
import tkinter as tk
from typing import List, Tuple

import numpy as np

# ── geometry / look ──────────────────────────────────────────────────────────
WIDTH, HEIGHT = 248, 66            # the pill itself
PAD = 4                            # window margin so the soft rim is not clipped
WIN_W, WIN_H = WIDTH + 2 * PAD, HEIGHT + 2 * PAD
BARS = 13
BAR_W = 4
BAR_GAP = 11
MAX_BAR = 44          # tallest a bar can get, px
MIN_BAR = 4           # resting dot, px
SCREEN_Y = 0.90       # vertical centre, as a fraction of the work area

# A fully transparent background was tried and rejected: with no plate behind
# them the white bars vanish on a light desktop.  Keeping a dark plate at 50 %
# opacity still leaves bars and plate ~127 luminance apart, so the shape stays
# readable on light and dark wallpapers alike.
C_LISTEN = "#FFFFFF"
C_THINK = "#5B9DFF"
C_DONE = "#22C55E"
C_EMPTY = "#6B7280"

PLATE_ALPHA = 0.50    # plate opacity — the "50 % 半透明" the user settled on
EDGE_FEATHER = 4.0    # px of soft falloff on the pill rim; 1.0 = crisp AA only
BAR_FEATHER = 1.0     # px of AA on the bars themselves


def _envelope() -> List[float]:
    """Centre-weighted bar heights, so the shape reads as a waveform."""
    return [0.40 + 0.60 * math.sin(math.pi * (i + 0.5) / BARS) for i in range(BARS)]


ENVELOPE = _envelope()

# ── Win32 prototypes ─────────────────────────────────────────────────────────
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

MONITOR_DEFAULTTONEAREST = 2

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0

_PTR_MAX = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def _bind_prototypes() -> None:
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.GetCursorPos.restype = wt.BOOL

    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wt.HWND

    _user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int

    _user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    _user32.MonitorFromWindow.restype = wt.HANDLE

    _user32.MonitorFromPoint.argtypes = [_POINT, wt.DWORD]
    _user32.MonitorFromPoint.restype = wt.HANDLE

    _user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wt.BOOL

    _user32.GetParent.argtypes = [wt.HWND]
    _user32.GetParent.restype = wt.HWND

    _user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long

    _user32.SetWindowPos.argtypes = [
        wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT
    ]
    _user32.SetWindowPos.restype = wt.BOOL

    _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wt.BOOL

    _user32.GetDC.argtypes = [wt.HWND]
    _user32.GetDC.restype = wt.HDC
    _user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int

    _user32.UpdateLayeredWindow.argtypes = [
        wt.HWND,
        wt.HDC,
        ctypes.POINTER(_POINT),
        ctypes.POINTER(_SIZE),
        wt.HDC,
        ctypes.POINTER(_POINT),
        wt.DWORD,
        ctypes.POINTER(_BLENDFUNCTION),
        wt.DWORD,
    ]
    _user32.UpdateLayeredWindow.restype = wt.BOOL

    _gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    _gdi32.CreateCompatibleDC.restype = wt.HDC

    _gdi32.DeleteDC.argtypes = [wt.HDC]
    _gdi32.DeleteDC.restype = wt.BOOL

    _gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
    _gdi32.SelectObject.restype = wt.HANDLE

    _gdi32.DeleteObject.argtypes = [wt.HANDLE]
    _gdi32.DeleteObject.restype = wt.BOOL

    _gdi32.CreateDIBSection.argtypes = [
        wt.HDC,
        ctypes.POINTER(_BITMAPINFOHEADER),
        wt.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wt.HANDLE,
        wt.DWORD,
    ]
    _gdi32.CreateDIBSection.restype = wt.HANDLE


def _enable_dpi_awareness() -> None:
    """Without this the pill is bitmap-stretched and blurred on scaled displays."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:  # noqa: BLE001 — pre-8.1 or already set
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


# Do it at import time: once anything has created a window, Windows refuses to
# change the process DPI awareness and the overlay comes out blurry.
_enable_dpi_awareness()


_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})


def _foreground_window() -> int:
    """The window the user is actually working in, or 0 if only the shell is up.

    Clicking bare desktop makes Progman/WorkerW "foreground"; treating those as
    a real app would pin the pill to the primary monitor, so they count as none.
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0
    buf = ctypes.create_unicode_buffer(64)
    if _user32.GetClassNameW(hwnd, buf, 64) and buf.value in _SHELL_CLASSES:
        return 0
    return hwnd


def work_area() -> Tuple[int, int, int, int]:
    """Work area (taskbar excluded) of the monitor hosting the foreground window.

    Falls back to the monitor under the cursor when there is no foreground
    window (bare desktop, UAC prompt, or right after a display change).
    """
    hmon = 0
    hwnd = _foreground_window()
    if hwnd:
        hmon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        pt = _POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            hmon = _user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)

    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if hmon and _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        r = info.rcWork
        return (r.left, r.top, r.right, r.bottom)
    return (0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))


def _anchor() -> Tuple[int, int]:
    """Top-left corner for the pill on the monitor hosting the foreground app."""
    left, top, right, bottom = work_area()
    mon_w = max(1, right - left)
    mon_h = max(1, bottom - top)
    return (
        left + (mon_w - WIN_W) // 2,
        top + int(mon_h * SCREEN_Y) - WIN_H // 2,
    )


# ── rasteriser ───────────────────────────────────────────────────────────────
# Pixel-centre coordinate grids, computed once.  Everything below is a signed
# distance field, which is what makes the edges smooth: a value of 0 sits on the
# outline, negative is inside, positive outside.
_YS, _XS = np.mgrid[0.0:WIN_H, 0.0:WIN_W]
_XS = (_XS + 0.5).astype(np.float32)
_YS = (_YS + 0.5).astype(np.float32)


def _round_box_cov(xs, ys, cx, cy, hw, hh, r, feather):
    """Coverage 0..1 for a rounded box, with a smoothstep ramp ``feather`` px wide.

    ``feather`` is the whole width of the transition band: 1.0 gives textbook
    1 px anti-aliasing, larger values give the soft, slightly blurred rim.
    """
    qx = np.abs(xs - cx) - (hw - r)
    qy = np.abs(ys - cy) - (hh - r)
    d = (
        np.sqrt(np.maximum(qx, 0.0) ** 2 + np.maximum(qy, 0.0) ** 2)
        + np.minimum(np.maximum(qx, qy), 0.0)
        - r
    )
    t = np.clip((0.5 * feather - d) / feather, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)          # smoothstep — no banding at the rim


def _plate_coverage() -> np.ndarray:
    """The capsule outline: a rounded box whose radius equals half the height."""
    return _round_box_cov(
        _XS, _YS,
        WIN_W / 2.0, WIN_H / 2.0,
        WIDTH / 2.0, HEIGHT / 2.0,
        HEIGHT / 2.0,
        EDGE_FEATHER,
    )


PLATE_COV = _plate_coverage()


def _hex_rgb(color: str) -> np.ndarray:
    return np.array(
        [int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)], np.float32
    )


# ── overlay ──────────────────────────────────────────────────────────────────

class RecordingOverlay:
    """The floating pill.  Create on the main thread, then call ``tick`` ~60 Hz."""

    def __init__(self) -> None:
        _bind_prototypes()

        x, y = _anchor()
        self._x, self._y = x, y

        self.root = tk.Tk()
        self.root.withdraw()                      # avoid the default-root flash
        self.root.overrideredirect(True)
        self.root.configure(bg="black")
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

        self.root.update_idletasks()
        self.root.deiconify()
        self.root.update_idletasks()

        # now that the window is mapped, grab the real top-level HWND
        hwnd = _user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        self._hwnd = wt.HWND(hwnd)
        self._dress_window()
        self._setup_surface()

        # bars, centred as a group
        total = BARS * BAR_W + (BARS - 1) * BAR_GAP
        x0 = PAD + (WIDTH - total) / 2.0 + BAR_W / 2.0
        self._xs: List[float] = [x0 + i * (BAR_W + BAR_GAP) for i in range(BARS)]
        self._heights: List[float] = [MIN_BAR] * BARS

        self._state = "idle"
        self._state_t0 = 0.0
        self._fade = 0.0
        self._level = 0.0
        self._bar_rgb = _hex_rgb(C_LISTEN)
        self._last_topmost = 0.0
        self._shown = False
        self._closed = False

        self._show_window(False)

    # -- window decoration ---------------------------------------------------
    def _dress_window(self) -> None:
        ex = _user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(
            self._hwnd,
            GWL_EXSTYLE,
            ex | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED,
        )
        # Style changes only take effect after SWP_FRAMECHANGED.
        _user32.SetWindowPos(
            self._hwnd, wt.HWND(0), 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

    def _setup_surface(self) -> None:
        """Create the DIB we draw into and the DCs UpdateLayeredWindow needs."""
        self._screen_dc = _user32.GetDC(wt.HWND(0))
        self._mem_dc = _gdi32.CreateCompatibleDC(self._screen_dc)

        hdr = _BITMAPINFOHEADER()
        hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        hdr.biWidth = WIN_W
        hdr.biHeight = -WIN_H           # negative = top-down rows, like numpy
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = DIB_RGB_COLORS

        bits = ctypes.c_void_p()
        self._bitmap = _gdi32.CreateDIBSection(
            self._screen_dc, ctypes.byref(hdr), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not self._bitmap or not bits:
            raise OSError("CreateDIBSection failed")
        _gdi32.SelectObject(self._mem_dc, self._bitmap)

        raw = (ctypes.c_ubyte * (WIN_W * WIN_H * 4)).from_address(bits.value)
        self._px = np.frombuffer(raw, dtype=np.uint8).reshape(WIN_H, WIN_W, 4)

    def _show_window(self, show: bool) -> None:
        if show == self._shown:
            return
        _user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE if show else SW_HIDE)
        self._shown = show

    def _reposition(self) -> None:
        """Follow the foreground app across monitors — called each time we start."""
        x, y = _anchor()
        if (x, y) == (self._x, self._y):
            return
        self._x, self._y = x, y
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

    def _reassert_topmost(self) -> None:
        _user32.SetWindowPos(
            self._hwnd,
            wt.HWND(_PTR_MAX),        # HWND_TOPMOST
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    # -- compositing ---------------------------------------------------------
    def _render(self) -> None:
        """Rasterise one frame into the DIB, then hand it to the window."""
        bars = np.zeros((WIN_H, WIN_W), np.float32)
        cy = WIN_H / 2.0
        half_w = BAR_W / 2.0

        for i, h in enumerate(self._heights):
            hh = max(h, 2.0) / 2.0
            bx = self._xs[i]
            # only rasterise the bar's own bounding box — 13 full-frame SDFs
            # would waste ~10x the CPU for the same picture
            x0 = max(0, int(bx - 6))
            x1 = min(WIN_W, int(bx + 7))
            y0 = max(0, int(cy - hh - 3))
            y1 = min(WIN_H, int(cy + hh + 4))
            if x1 <= x0 or y1 <= y0:
                continue
            cov = _round_box_cov(
                _XS[y0:y1, x0:x1], _YS[y0:y1, x0:x1],
                bx, cy, half_w, hh, half_w, BAR_FEATHER,
            )
            np.maximum(bars[y0:y1, x0:x1], cov, out=bars[y0:y1, x0:x1])

        # Plate and bars share one alpha channel (both at PLATE_ALPHA), so the
        # bar colour can simply be mixed into the plate colour without seams.
        alpha = PLATE_COV * (PLATE_ALPHA * self._fade)
        rgb = self._bar_rgb * bars[..., None]

        prem = rgb * alpha[..., None]
        arr = self._px
        arr[..., 0] = prem[..., 2]        # BGRA order in the DIB
        arr[..., 1] = prem[..., 1]
        arr[..., 2] = prem[..., 0]
        arr[..., 3] = alpha * 255.0

        pt_dst = _POINT(self._x, self._y)
        size = _SIZE(WIN_W, WIN_H)
        pt_src = _POINT(0, 0)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        _user32.UpdateLayeredWindow(
            self._hwnd,
            self._screen_dc,
            ctypes.byref(pt_dst),
            ctypes.byref(size),
            self._mem_dc,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )

    # -- state transitions ---------------------------------------------------
    def _set(self, state: str) -> None:
        self._state = state
        self._state_t0 = time.perf_counter()
        color = {
            "listening": C_LISTEN,
            "thinking": C_THINK,
            "done": C_DONE,
            "empty": C_EMPTY,
        }.get(state, C_LISTEN)
        rgb = _hex_rgb(color)
        if not np.array_equal(rgb, self._bar_rgb):
            self._bar_rgb = rgb
        if state != "idle":
            self._reassert_topmost()
            self._show_window(True)

    def show_listening(self) -> None:
        self._reposition()
        self._level = 0.0
        self._set("listening")

    def show_thinking(self) -> None:
        self._set("thinking")

    def show_done(self) -> None:
        self._set("done")

    def show_empty(self) -> None:
        self._set("empty")

    def hide(self) -> None:
        self._set("idle")

    # -- animation -----------------------------------------------------------
    def tick(self, level: float) -> None:
        """Advance one frame.  ``level`` is the current mic level, 0..1."""
        if self._closed:
            return
        now = time.perf_counter()

        target = level if self._state == "listening" else 0.0
        # fast attack, slow release — reads like a real VU meter
        k = 0.55 if target > self._level else 0.14
        self._level += (target - self._level) * k

        visible = self._state != "idle"
        goal = 1.0 if visible else 0.0
        self._fade += (goal - self._fade) * (0.30 if visible else 0.18)
        if self._fade < 0.01:
            self._fade = 0.0

        self._update_heights(now)

        if self._fade > 0.0:
            self._render()
            self._show_window(True)
        else:
            self._show_window(False)

        if now - self._last_topmost > 1.0:
            self._last_topmost = now
            if visible:
                self._reassert_topmost()

    def _update_heights(self, now: float) -> None:
        span = MAX_BAR - MIN_BAR

        if self._state == "listening":
            # small idle breathing so "listening" never looks frozen
            drive = max(self._level, 0.08 + 0.05 * math.sin(now * 2.6))
            drive = min(1.0, drive * 1.1) ** 0.8
            for i in range(BARS):
                wob = 0.80 + 0.20 * math.sin(now * 12.0 + i * 1.41)
                self._heights[i] = MIN_BAR + span * ENVELOPE[i] * drive * wob

        elif self._state == "thinking":
            for i in range(BARS):
                w = 0.5 + 0.5 * math.sin(now * 5.4 - i * 0.62)
                self._heights[i] = 8.0 + 18.0 * w

        elif self._state == "done":
            if now - self._state_t0 > 0.55:
                self._set("idle")
                return
            for i in range(BARS):
                self._heights[i] = MIN_BAR + span * 0.72 * ENVELOPE[i]

        elif self._state == "empty":
            if now - self._state_t0 > 0.40:
                self._set("idle")
                return
            for i in range(BARS):
                self._heights[i] = 5.0

    # -- lifecycle -----------------------------------------------------------
    def schedule(self, fn, interval_ms: int) -> None:
        self.root.after(interval_ms, fn)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fade = 0.0
        try:
            self._show_window(False)
        except Exception:  # noqa: BLE001
            pass
        for name, deleter in (
            ("_bitmap", _gdi32.DeleteObject),
            ("_mem_dc", _gdi32.DeleteDC),
        ):
            handle = getattr(self, name, None)
            if handle:
                try:
                    deleter(handle)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, name, None)
        if getattr(self, "_screen_dc", None):
            try:
                _user32.ReleaseDC(wt.HWND(0), self._screen_dc)
            except Exception:  # noqa: BLE001
                pass
            self._screen_dc = None
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


class NullOverlay:
    """No-op stand-in used when a window cannot be created."""

    root = None

    def show_listening(self) -> None: ...
    def show_thinking(self) -> None: ...
    def show_done(self) -> None: ...
    def show_empty(self) -> None: ...
    def hide(self) -> None: ...
    def tick(self, level: float) -> None: ...
    def schedule(self, fn, interval_ms: int) -> None: ...
    def shutdown(self) -> None: ...


def create_overlay():
    try:
        return RecordingOverlay()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[UI] overlay unavailable ({exc}) — running headless")
        return NullOverlay()
