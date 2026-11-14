"""Tiny ctypes screen grabber (no Pillow needed). Writes a 24-bit BMP."""

import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# Handles are pointer-sized.  Leaving them to the default c_int conversion
# truncates them on 64-bit Windows — and once *any* module declares the real
# restype on this shared gdi32 object, the stale truncated value blows up.
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.BitBlt.argtypes = [
    wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD,
]
gdi32.BitBlt.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [
    wt.HDC, wt.HANDLE, wt.UINT, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO), wt.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL


def grab(path, crop=None):
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

    if crop:
        cx, cy, cw, ch = crop
    else:
        cx, cy, cw, ch = vx, vy, vw, vh
    # clamp into the virtual desktop
    cx = max(vx, min(cx, vx + vw - 1))
    cy = max(vy, min(cy, vy + vh - 1))
    cw = max(1, min(cw, vx + vw - cx))
    ch = max(1, min(ch, vy + vh - cy))

    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, cw, ch)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, cw, ch, hdc, cx, cy, SRCCOPY)

    stride = ((cw * 3 + 3) // 4) * 4
    size = stride * ch
    buf = ctypes.create_string_buffer(size)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = cw
    bi.bmiHeader.biHeight = -ch          # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 24
    bi.bmiHeader.biCompression = BI_RGB

    gdi32.GetDIBits(mem, bmp, 0, ch, ctypes.cast(buf, ctypes.c_void_p),
                    ctypes.byref(bi), DIB_RGB_COLORS)

    header = bytearray()
    header += b"BM"
    header += (14 + 40 + size).to_bytes(4, "little")
    header += (0).to_bytes(4, "little")
    header += (14 + 40).to_bytes(4, "little")
    header += (40).to_bytes(4, "little")
    header += cw.to_bytes(4, "little", signed=True)
    header += (-ch).to_bytes(4, "little", signed=True)
    header += (1).to_bytes(2, "little")
    header += (24).to_bytes(2, "little")
    header += (0).to_bytes(4, "little")
    header += size.to_bytes(4, "little")
    header += (2835).to_bytes(4, "little")
    header += (2835).to_bytes(4, "little")
    header += (0).to_bytes(4, "little")
    header += (0).to_bytes(4, "little")

    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(buf.raw)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)


if __name__ == "__main__":
    out = sys.argv[1]
    crop = tuple(int(v) for v in sys.argv[2:6]) if len(sys.argv) >= 6 else None
    grab(out, crop)
    print("ok", out, crop or "full")
