"""Small Windows helpers using the standard Win32 API via ctypes.

Plain English:
* "Click-through" means mouse clicks pass straight through the overlay to the
  game underneath. Windows does this for windows marked WS_EX_TRANSPARENT.
* "Always on top" is kept by re-asserting HWND_TOPMOST every couple of seconds,
  because some games push themselves to the front.

None of this touches the game. It only changes our own window.
On non-Windows systems every function here does nothing, so the rest of
the app can still be tested.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_APPWINDOW = 0x00040000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

VK = {
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
}

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # 64-bit safe signatures
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _GetWindowLong = user32.GetWindowLongPtrW
        _SetWindowLong = user32.SetWindowLongPtrW
        _LONG = ctypes.c_longlong
    else:
        _GetWindowLong = user32.GetWindowLongW
        _SetWindowLong = user32.SetWindowLongW
        _LONG = ctypes.c_long
    _GetWindowLong.restype = _LONG
    _GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
    _SetWindowLong.restype = _LONG
    _SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, _LONG]

    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL


def desired_exstyle(current: int, click_through: bool) -> int:
    """Work out the extended style we want, starting from what Qt already set.

    WS_EX_LAYERED is only *added* for click-through (Windows needs LAYERED +
    TRANSPARENT for clicks to pass through). Qt already makes translucent
    windows layered, so we never strip it either.
    """
    style = current | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    style &= ~WS_EX_APPWINDOW
    if click_through:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    return style


def ensure_styles(hwnd: int, click_through: bool) -> bool:
    """Re-apply our styles only if something changed them. Returns True if changed."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        cur = _GetWindowLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        want = desired_exstyle(cur, click_through)
        if want == cur:
            return False
        _SetWindowLong(hwnd, GWL_EXSTYLE, want)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        log.exception("ensure_styles failed")
        return False


def set_overlay_styles(hwnd: int, click_through: bool) -> bool:
    """Apply our extended window styles and push the window to the top."""
    if not IS_WINDOWS or not hwnd:
        return False
    ensure_styles(hwnd, click_through)
    force_topmost(hwnd)
    return True


def force_topmost(hwnd: int) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
    except Exception:
        log.exception("force_topmost failed")
