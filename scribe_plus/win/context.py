"""
Which app is the user dictating into? (foreground window and process)

Store apps run inside ApplicationFrameHost.exe, so for those the real
process is read from the child window that belongs to a different process.
"""
from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

log = logging.getLogger(__name__)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_api = None


def _apis():
    global _api
    if _api is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumChildWindows.argtypes = [wintypes.HWND, enum_proc, wintypes.LPARAM]
        user32.EnumChildWindows.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _api = (user32, kernel32, enum_proc)
    return _api


def _pid_of(hwnd) -> int:
    user32, _k, _e = _apis()
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    _u, kernel32, _e = _apis()
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def _title(hwnd) -> str:
    user32, _k, _e = _apis()
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def foreground() -> tuple[int, str, str]:
    """(hwnd as int, process name, window title) of the foreground window."""
    try:
        user32, _k, enum_proc = _apis()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0, "", ""
        pid = _pid_of(hwnd)
        name = _process_name(pid)
        if name == "applicationframehost.exe":
            found: list[int] = []

            def _cb(child, _lp):
                cpid = _pid_of(child)
                if cpid and cpid != pid:
                    found.append(cpid)
                    return False
                return True

            user32.EnumChildWindows(hwnd, enum_proc(_cb), 0)
            if found:
                name = _process_name(found[0]) or name
        handle = ctypes.cast(hwnd, ctypes.c_void_p).value or 0
        return int(handle), name, _title(hwnd)
    except Exception as exc:
        log.debug("foreground lookup failed: %s", exc)
        return 0, "", ""
