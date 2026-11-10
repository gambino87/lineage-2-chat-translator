"""Session-local, data-free requests to reveal the existing application window."""
from __future__ import annotations

import ctypes
import mmap
import os
import time
import tkinter as tk

ACTIVATION_NAME = "Local\\L2ProminenceChatTranslatorActivation"
POLL_INTERVAL_MS = 100
REQUEST_TIMEOUT_SECONDS = 2.0
RETRY_INTERVAL_SECONDS = 0.05
EVENT_MODIFY_STATE = 0x0002
FILE_MAP_READ = 0x0004
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
GA_ROOT = 2
PID_BYTES = 4


class _Windows:
    """Native declarations only; no windows, threads or processes are created."""
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        for library, name, result, arguments in (
            (self.kernel, "CreateEventW", ctypes.c_void_p,
             [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_wchar_p]),
            (self.kernel, "OpenEventW", ctypes.c_void_p,
             [ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p]),
            (self.kernel, "SetEvent", ctypes.c_int32, [ctypes.c_void_p]),
            (self.kernel, "WaitForSingleObject", ctypes.c_uint32, [ctypes.c_void_p, ctypes.c_uint32]),
            (self.kernel, "CloseHandle", ctypes.c_int32, [ctypes.c_void_p]),
            (self.kernel, "OpenFileMappingW", ctypes.c_void_p,
             [ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p]),
            (self.kernel, "MapViewOfFile", ctypes.c_void_p,
             [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]),
            (self.kernel, "UnmapViewOfFile", ctypes.c_int32, [ctypes.c_void_p]),
            (self.user, "AllowSetForegroundWindow", ctypes.c_int32, [ctypes.c_uint32]),
            (self.user, "GetAncestor", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_uint32]),
            (self.user, "SetForegroundWindow", ctypes.c_int32, [ctypes.c_void_p]),
        ):
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
            setattr(self, name, function)


def _signal_existing(api, name: str) -> bool:
    event = api.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not event:
        return False
    mapping = view = None
    try:
        # Read only the owner's PID; the launcher supplies no commands or paths.
        mapping = api.OpenFileMappingW(FILE_MAP_READ, False, name + "Owner")
        if not mapping:
            return False
        view = api.MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, PID_BYTES)
        if not view:
            return False
        pid = ctypes.c_uint32.from_address(view).value
        if not pid:
            return False
        # The user just launched this copy. Transfer its foreground permission
        # to the specific existing process before waking that process's Tk loop.
        # Windows can decline permission; restoration still remains useful.
        api.AllowSetForegroundWindow(pid)
        return bool(api.SetEvent(event))
    finally:
        if view:
            api.UnmapViewOfFile(view)
        if mapping:
            api.CloseHandle(mapping)
        api.CloseHandle(event)


def activate_existing(*, name: str = ACTIVATION_NAME) -> bool:
    """Signal the owner, allowing a bounded mutex-to-listener startup race."""
    if os.name != "nt":
        return False
    try:
        api = _Windows()
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while True:
            if _signal_existing(api, name):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(RETRY_INTERVAL_SECONDS)
    except OSError:
        return False


class ActivationListener:
    """Owned with the instance mutex, across setup/translator root transitions."""
    def __init__(self, *, name: str = ACTIVATION_NAME):
        self._api = None
        self._mapping = None
        self._event = None
        self._root = None
        self._timer = None
        self._generation = 0
        if os.name != "nt":
            return
        self._api = _Windows()
        try:
            self._mapping = mmap.mmap(-1, PID_BYTES, tagname=name + "Owner", access=mmap.ACCESS_WRITE)
            self._mapping[:] = os.getpid().to_bytes(PID_BYTES, "little")
            # Auto-reset coalesces repeated launches and retains a pending request
            # while neither Tk root exists during the setup-to-translator handoff.
            self._event = self._api.CreateEventW(None, False, False, name)
            if not self._event:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.close()
            raise

    def bind(self, root, restore=None):
        """Poll only on the active Tk thread; never touch Tk from another thread."""
        self.unbind()
        if self._event is None:
            return
        self._root = root
        generation = self._generation

        def poll():
            if self._root is not root or self._generation != generation:
                return
            self._timer = None
            try:
                if self._api.WaitForSingleObject(self._event, 0) == WAIT_OBJECT_0:
                    try:
                        if restore is None:
                            root.deiconify()
                            root.lift()
                        else:
                            restore()
                    finally:
                        # Tk's ID is the client child; foreground its wrapper.
                        hwnd = self._api.GetAncestor(root.winfo_id(), GA_ROOT)
                        if hwnd:
                            self._api.SetForegroundWindow(hwnd)
            except tk.TclError:
                self._root = None
            except OSError:
                pass  # A native restore failure must not disable later requests.
            finally:
                if self._root is root and self._generation == generation:
                    self._timer = root.after(POLL_INTERVAL_MS, poll)

        self._timer = root.after(0, poll)

    def unbind(self):
        self._generation += 1
        root, timer = self._root, self._timer
        self._root = self._timer = None
        if root is not None and timer is not None:
            try:
                root.after_cancel(timer)
            except tk.TclError:
                pass

    def close(self):
        self.unbind()
        if self._event is not None:
            self._api.CloseHandle(self._event)
            self._event = None
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
