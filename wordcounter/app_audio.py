"""Record the sound of ONE app (Discord) - not games, YouTube or anything else.

Plain English: Windows 10 (version 2004 or newer) and Windows 11 can hand an app the
audio of a single other program ("process loopback" - the same thing OBS uses for
"Application Audio Capture"). We ask Windows for Discord's sound only and convert it
to the 16 kHz mono format the speech recognition wants.

This talks to Windows' audio system directly through ctypes (no extra installs).
"""

from __future__ import annotations

import logging
import sys
import threading
import time

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
OUT_RATE = 16000


# --------------------------------------------------------------------------- finding the app

def find_app_pid(exe_name: str = "Discord.exe") -> int | None:
    """The main process of an app (its other processes are included automatically)."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    procs = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            procs[e.th32ProcessID] = (e.szExeFile.lower(), e.th32ParentProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    want = exe_name.lower()
    mine = [pid for pid, (name, _) in procs.items() if name == want]
    roots = [pid for pid in mine if procs.get(procs[pid][1], ("", 0))[0] != want]
    return min(roots or mine) if (roots or mine) else None


# --------------------------------------------------------------------------- Windows audio (COM)

if IS_WINDOWS:
    import ctypes
    from ctypes import (POINTER, WINFUNCTYPE, Structure, byref, c_long, c_uint32, c_uint64, c_ulong,
                        c_ushort, c_void_p, c_wchar_p, cast)
    from ctypes import wintypes

    class GUID(Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def of(cls, text: str) -> "GUID":
            import uuid
            u = uuid.UUID(text)
            g = cls()
            g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
            for i, b in enumerate(u.bytes[8:]):
                g.Data4[i] = b
            return g

        def __eq__(self, other):
            return bytes(self) == bytes(other)

    IID_IUnknown = GUID.of("00000000-0000-0000-C000-000000000046")
    IID_IAgileObject = GUID.of("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
    IID_IAudioClient = GUID.of("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
    IID_IAudioCaptureClient = GUID.of("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
    IID_IActivateHandler = GUID.of("41D949AB-9862-444A-80F6-C261334DA5EB")

    class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
        _fields_ = [("ActivationType", c_ulong), ("TargetProcessId", c_ulong), ("ProcessLoopbackMode", c_ulong)]

    class PROPVARIANT_BLOB(Structure):
        _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                    ("cbSize", c_ulong), ("pBlobData", c_void_p)]

    class WAVEFORMATEX(Structure):
        _pack_ = 1
        _fields_ = [("wFormatTag", c_ushort), ("nChannels", c_ushort), ("nSamplesPerSec", c_ulong),
                    ("nAvgBytesPerSec", c_ulong), ("nBlockAlign", c_ushort), ("wBitsPerSample", c_ushort),
                    ("cbSize", c_ushort)]

    QI_T = WINFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
    REF_T = WINFUNCTYPE(c_ulong, c_void_p)
    DONE_T = WINFUNCTYPE(c_long, c_void_p, c_void_p)

    def _method(ptr, index, *argtypes):
        vtbl = cast(ptr, POINTER(POINTER(c_void_p)))[0]
        return WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])

    def _release(ptr):
        if ptr:
            vtbl = cast(ptr, POINTER(POINTER(c_void_p)))[0]
            REF_T(vtbl[2])(ptr)

    class _Handler:
        """A tiny COM object Windows calls when the audio client is ready."""

        def __init__(self):
            self.done = threading.Event()
            self.refs = 1

            def qi(this, riid, out):
                g = riid.contents
                if g == IID_IUnknown or g == IID_IAgileObject or g == IID_IActivateHandler:
                    out[0] = this
                    self.refs += 1
                    return 0
                out[0] = None
                return -2147467262            # E_NOINTERFACE

            def addref(this):
                self.refs += 1
                return self.refs

            def release(this):
                self.refs -= 1
                return max(self.refs, 0)

            def completed(this, op):
                self.done.set()
                return 0

            self._fns = (QI_T(qi), REF_T(addref), REF_T(release), DONE_T(completed))
            self._vtbl = (c_void_p * 4)(*[cast(f, c_void_p) for f in self._fns])
            self._obj = (c_void_p * 1)(cast(self._vtbl, c_void_p))
            self.ptr = cast(self._obj, c_void_p)


def _hr(x: int) -> str:
    return f"0x{x & 0xFFFFFFFF:08X}"


class AppAudio:
    """Records one app's sound. Use as: with AppAudio(pid) as a: a.read(seconds)."""

    def __init__(self, pid: int):
        self.pid = pid
        self.client = None
        self.capture = None
        self.rate = 0
        self.channels = 0
        self.bits = 0
        self.is_float = False
        self._pending = []
        self._last_data = None
        self._com = False

    # -- set-up -------------------------------------------------------------
    loopback = True          # recording another app's output (Discord); False for a microphone

    def __enter__(self):
        if not IS_WINDOWS:
            raise RuntimeError("only on Windows")
        ole32 = ctypes.WinDLL("ole32")
        hr = ole32.CoInitializeEx(None, 0x0)                 # multithreaded
        self._com = hr in (0, 1)
        self.client = self._activate()
        self._init_format()
        cap = c_void_p()
        hr = _method(self.client, 14, POINTER(GUID), POINTER(c_void_p))(self.client, byref(IID_IAudioCaptureClient),
                                                                         byref(cap))
        if hr < 0:
            raise OSError(f"no capture client ({_hr(hr)})")
        self.capture = cap
        hr = _method(self.client, 10)(self.client)            # Start
        if hr < 0:
            raise OSError(f"couldn't start ({_hr(hr)})")
        self._last_data = time.monotonic()
        log.info("%s: recording (%d Hz, %d ch, %d-bit%s)", self.describe(), self.rate,
                 self.channels, self.bits, " float" if self.is_float else "")
        return self

    def describe(self) -> str:
        return f"app audio (process {self.pid})"

    def _activate(self):
        """Get an IAudioClient for just this app's sound."""
        mmdev = ctypes.WinDLL("Mmdevapi")
        act = mmdev.ActivateAudioInterfaceAsync
        act.restype = c_long
        act.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT_BLOB), c_void_p, POINTER(c_void_p)]

        params = AUDIOCLIENT_ACTIVATION_PARAMS(1, self.pid, 0)   # PROCESS_LOOPBACK, include the app's children
        pv = PROPVARIANT_BLOB(65, 0, 0, 0, ctypes.sizeof(params), cast(byref(params), c_void_p))
        handler = _Handler()
        self._handler = handler
        op = c_void_p()
        hr = act("VAD\\Process_Loopback", byref(IID_IAudioClient), byref(pv), handler.ptr, byref(op))
        if hr < 0:
            raise OSError(f"Windows refused app audio ({_hr(hr)}) - needs Windows 10 2004 or newer")
        try:
            if not handler.done.wait(5):
                raise TimeoutError("Windows didn't answer")
            res, unk = c_long(), c_void_p()
            hr = _method(op, 3, POINTER(c_long), POINTER(c_void_p))(op, byref(res), byref(unk))
            if hr < 0 or res.value < 0 or not unk:
                raise OSError(f"app audio not available ({_hr(hr if hr < 0 else res.value)})")
        finally:
            _release(op)
        return unk          # the IAudioClient itself (activated with that IID)

    def _init_format(self):
        init = _method(self.client, 3, c_ulong, c_ulong, ctypes.c_longlong, ctypes.c_longlong,
                       c_void_p, c_void_p)
        LOOPBACK, AUTOCONVERT, SRC_QUALITY = 0x00020000, 0x80000000, 0x08000000
        lb = LOOPBACK if self.loopback else 0
        tries = [(16000, 1, lb | AUTOCONVERT | SRC_QUALITY),
                 (48000, 2, lb | AUTOCONVERT | SRC_QUALITY),
                 (48000, 1, lb | AUTOCONVERT | SRC_QUALITY),
                 (44100, 2, lb), (48000, 2, lb), (48000, 1, lb), (44100, 1, lb)]
        last = 0
        for rate, ch, flags in tries:
            fmt = WAVEFORMATEX(1, ch, rate, rate * ch * 2, ch * 2, 16, 0)     # 16-bit PCM
            hr = init(self.client, 0, flags, 2_000_000, 0, cast(byref(fmt), c_void_p), None)  # shared, 200 ms
            if hr >= 0:
                self.rate, self.channels, self.bits, self.is_float = rate, ch, 16, False
                return
            last = hr
            log.info("%s: %d Hz / %d ch refused (%s)", self.describe(), rate, ch, _hr(hr))
            if (hr & 0xFFFFFFFF) == 0x88890002:      # AUDCLNT_E_ALREADY_INITIALIZED - shouldn't happen
                break
        # last resort: exactly the format Windows itself uses for this device
        mix = c_void_p()
        hr = _method(self.client, 8, POINTER(c_void_p))(self.client, byref(mix))     # GetMixFormat
        if hr >= 0 and mix:
            try:
                wf = cast(mix, POINTER(WAVEFORMATEX)).contents
                hr = init(self.client, 0, lb, 2_000_000, 0, mix, None)
                if hr >= 0:
                    self.rate, self.channels, self.bits = wf.nSamplesPerSec, wf.nChannels, wf.wBitsPerSample
                    self.is_float = wf.wBitsPerSample == 32    # Windows' mix format is 32-bit float
                    return
                last = hr
            finally:
                ctypes.WinDLL("ole32").CoTaskMemFree(mix)
        raise OSError(f"no usable sound format ({_hr(last)})")

    def __exit__(self, *exc):
        try:
            if self.client:
                _method(self.client, 11)(self.client)        # Stop
        except Exception:
            pass
        _release(self.capture)
        _release(self.client)
        self.capture = self.client = None
        if self._com:
            try:
                ctypes.WinDLL("ole32").CoUninitialize()
            except Exception:
                pass
        return False

    # -- reading ------------------------------------------------------------
    def _pull(self):
        """Take everything Windows has collected. While the app is silent Windows sends
        nothing at all, so that time is filled with silence to keep the timing right."""
        import numpy as np
        get_size = _method(self.capture, 5, POINTER(c_uint32))
        get_buf = _method(self.capture, 3, POINTER(c_void_p), POINTER(c_uint32), POINTER(wintypes.DWORD),
                          POINTER(c_uint64), POINTER(c_uint64))
        rel_buf = _method(self.capture, 4, c_uint32)
        got = False
        while True:
            n = c_uint32()
            if get_size(self.capture, byref(n)) < 0 or n.value == 0:
                break
            data, frames, flags = c_void_p(), c_uint32(), wintypes.DWORD()
            hr = get_buf(self.capture, byref(data), byref(frames), byref(flags), None, None)
            if hr < 0:
                raise OSError(f"reading sound failed ({_hr(hr)})")
            count = frames.value * self.channels
            if flags.value & 0x2 or not data:                 # AUDCLNT_BUFFERFLAGS_SILENT
                arr = np.zeros(count, dtype=np.float32)
            else:
                if self.bits == 32:
                    raw = ctypes.string_at(data, count * 4)
                    arr = (np.frombuffer(raw, dtype=np.float32) if self.is_float else
                           np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0).copy()
                else:
                    raw = ctypes.string_at(data, count * 2)
                    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            rel_buf(self.capture, frames.value)
            self._pending.append(self._to_16k_mono(arr))
            got = True
        now = time.monotonic()
        if got:
            self._last_data = now
        elif now - self._last_data > 0.12:
            missing = int((now - self._last_data) * OUT_RATE)
            self._pending.append(np.zeros(missing, dtype=np.float32))
            self._last_data = now

    def _to_16k_mono(self, arr):
        import numpy as np
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels).mean(axis=1)
        if self.rate == OUT_RATE:
            return arr
        if self.rate % OUT_RATE == 0:                        # 48 kHz -> average every 3 samples
            k = self.rate // OUT_RATE
            n = len(arr) // k * k
            return arr[:n].reshape(-1, k).mean(axis=1)
        idx = np.arange(0, len(arr), self.rate / OUT_RATE)
        return np.interp(idx, np.arange(len(arr)), arr).astype(np.float32)

    def read(self, frames: int):
        """Block until `frames` samples (16 kHz mono) are ready; returns a float32 array."""
        import numpy as np
        while sum(len(p) for p in self._pending) < frames:
            self._pull()
            time.sleep(0.01)
        allp = np.concatenate(self._pending)
        out, rest = allp[:frames], allp[frames:]
        self._pending = [rest] if len(rest) else []
        return out.astype(np.float32)


if IS_WINDOWS:
    CLSID_MMDeviceEnumerator = GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
    IID_IMMDeviceEnumerator = GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")


def _default_capture_device():
    """IMMDevice of Windows' default microphone (caller releases it)."""
    enum = c_void_p()
    hr = ctypes.WinDLL("ole32").CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, 23,
                                                 byref(IID_IMMDeviceEnumerator), byref(enum))
    if hr < 0:
        raise OSError(f"Windows sound system unavailable ({_hr(hr)})")
    try:
        dev = c_void_p()
        hr = _method(enum, 4, c_ulong, c_ulong, POINTER(c_void_p))(enum, 1, 0, byref(dev))  # eCapture, eConsole
        if hr < 0 or not dev:
            if (hr & 0xFFFFFFFF) == 0x80070490:
                raise OSError("no microphone found - check it's plugged in and set as default in Windows")
            raise OSError(f"no default microphone ({_hr(hr)})")
        return dev
    finally:
        _release(enum)


def default_mic_id() -> str:
    """An id that changes when the default microphone in Windows changes ('' if none)."""
    try:
        dev = _default_capture_device()
    except OSError:
        return ""
    try:
        sid = c_wchar_p()
        if _method(dev, 5, POINTER(c_wchar_p))(dev, byref(sid)) < 0:
            return ""
        val = sid.value or ""
        ctypes.WinDLL("ole32").CoTaskMemFree(sid)
        return val
    finally:
        _release(dev)


class MicAudio(AppAudio):
    """Records Windows' default microphone directly (no extra libraries)."""

    loopback = False

    def __init__(self):
        super().__init__(0)

    def describe(self) -> str:
        return "microphone"

    def _activate(self):
        dev = _default_capture_device()
        try:
            client = c_void_p()
            hr = _method(dev, 3, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p))(
                dev, byref(IID_IAudioClient), 23, None, byref(client))
            if hr < 0 or not client:
                if (hr & 0xFFFFFFFF) == 0x8889000A:          # AUDCLNT_E_DEVICE_IN_USE
                    raise OSError("another app is using the microphone in exclusive mode")
                raise OSError(f"couldn't open the microphone ({_hr(hr)})")
            return client
        finally:
            _release(dev)


def probe(pid: int, seconds: float = 1.5) -> dict:
    """Try listening to `pid` for a moment (used in a separate process, so a problem
    can never crash the overlay)."""
    rep: dict = {"pid": pid}
    try:
        import numpy as np
        with AppAudio(pid) as a:
            rep["format"] = f"{a.rate} Hz, {a.channels} ch"
            x = a.read(int(OUT_RATE * seconds))
            rep["seconds"] = round(len(x) / OUT_RATE, 2)
            rep["rms"] = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        rep["ok"] = True
    except Exception as e:
        rep["ok"] = False
        rep["error"] = str(e) or repr(e)
    return rep
