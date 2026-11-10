"""Optional Windows notification icon; never touches Tk or another app's window.

One native thread owns an invisible top-level window, its message loop, icon
and popup menu. A top-level window is intentional: unlike HWND_MESSAGE it can
receive Explorer's TaskbarCreated broadcast. It is never shown or activated
except to own its explicitly requested popup menu. Only fixed restore/exit
events cross to the launcher's queue; no chat, model access, hooks or packages
belong here. The launcher must wait for ``ready`` before hiding its window.

Win32 contracts: https://learn.microsoft.com/windows/win32/shell/notification-area
and https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-trackpopupmenu.
Importing this module creates no window, thread, icon, or native API handle.
"""
from __future__ import annotations

import ctypes
import math
import os
import threading

from ui_i18n import tr


# Use pointer-sized message parameters/results and fixed-width Win32 integers.
UINT = ctypes.c_uint32
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _CALLBACK(LRESULT, HANDLE, UINT, WPARAM, LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", HANDLE), ("message", UINT), ("wParam", WPARAM),
                ("lParam", LPARAM), ("time", DWORD), ("pt", POINT),
                ("lPrivate", DWORD)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int32), ("cbWndExtra", ctypes.c_int32),
                ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
                ("hbrBackground", HANDLE), ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _Version(ctypes.Union):
    _fields_ = [("uTimeout", UINT), ("uVersion", UINT)]


class NOTIFYICONDATAW(ctypes.Structure):
    _anonymous_ = ("version",)
    _fields_ = [("cbSize", DWORD), ("hWnd", HANDLE), ("uID", UINT),
                ("uFlags", UINT), ("uCallbackMessage", UINT), ("hIcon", HANDLE),
                ("szTip", ctypes.c_wchar * 128), ("dwState", DWORD),
                ("dwStateMask", DWORD), ("szInfo", ctypes.c_wchar * 256),
                ("version", _Version), ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", DWORD), ("guidItem", GUID), ("hBalloonIcon", HANDLE)]


WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_CONTEXTMENU = 0x007B
WM_TRAY = 0x8001  # WM_APP + 1, private to our owned window class.
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIM_ADD = 0
NIM_DELETE = 2
NIM_SETFOCUS = 3
NIM_SETVERSION = 4
NOTIFYICON_VERSION_4 = 4
NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_SHOWTIP = 0x0080
WS_EX_TOOLWINDOW = 0x00000080
WS_POPUP = 0x80000000  # No WS_VISIBLE: the receiver never becomes a taskbar window.
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
MF_STRING = 0
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100
ICON_ID = 1
OPEN_COMMAND = 1
EXIT_COMMAND = 2
TOOLTIP = "Lineage 2 Chat Translator"


class _Windows:
    """Declare the exact native ABI; construction alone does not create UI."""
    def __init__(self):
        if os.name != "nt":
            raise OSError(tr("The notification area is available only on Windows."))
        self.user = ctypes.WinDLL("user32.dll", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32.dll", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        declarations = (
            (self.kernel, "GetModuleHandleW", HANDLE, [ctypes.c_wchar_p]),
            (self.kernel, "GetCurrentThreadId", DWORD, []),
            (self.user, "LoadImageW", HANDLE,
             [HANDLE, ctypes.c_wchar_p, UINT, ctypes.c_int32, ctypes.c_int32, UINT]),
            (self.user, "DestroyIcon", BOOL, [HANDLE]),
            (self.user, "RegisterClassW", ctypes.c_uint16, [ctypes.POINTER(WNDCLASSW)]),
            (self.user, "UnregisterClassW", BOOL, [ctypes.c_wchar_p, HANDLE]),
            (self.user, "CreateWindowExW", HANDLE,
             [DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD,
              ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
              HANDLE, HANDLE, HANDLE, ctypes.c_void_p]),
            (self.user, "DestroyWindow", BOOL, [HANDLE]),
            (self.user, "DefWindowProcW", LRESULT, [HANDLE, UINT, WPARAM, LPARAM]),
            (self.user, "RegisterWindowMessageW", UINT, [ctypes.c_wchar_p]),
            (self.user, "GetMessageW", BOOL, [ctypes.POINTER(MSG), HANDLE, UINT, UINT]),
            (self.user, "TranslateMessage", BOOL, [ctypes.POINTER(MSG)]),
            (self.user, "DispatchMessageW", LRESULT, [ctypes.POINTER(MSG)]),
            (self.user, "PostMessageW", BOOL, [HANDLE, UINT, WPARAM, LPARAM]),
            (self.user, "PostThreadMessageW", BOOL, [DWORD, UINT, WPARAM, LPARAM]),
            (self.user, "PostQuitMessage", None, [ctypes.c_int32]),
            (self.user, "CreatePopupMenu", HANDLE, []),
            (self.user, "AppendMenuW", BOOL, [HANDLE, UINT, ctypes.c_size_t, ctypes.c_wchar_p]),
            (self.user, "DestroyMenu", BOOL, [HANDLE]),
            (self.user, "EndMenu", BOOL, []),
            (self.user, "SetForegroundWindow", BOOL, [HANDLE]),
            (self.user, "GetCursorPos", BOOL, [ctypes.POINTER(POINT)]),
            (self.user, "TrackPopupMenu", UINT,
             [HANDLE, UINT, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
              HANDLE, ctypes.c_void_p]),
            (self.shell, "Shell_NotifyIconW", BOOL, [DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
        )
        for library, name, result, arguments in declarations:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
            setattr(self, name, function)


class NativeTray:
    """One-use asynchronous notification icon with data-only event delivery.

    ``on_event(event, message=None)`` must be a short, nonthrowing queue callback,
    never a Tk operation. Events are ready, restore, exit and error. ``ready``
    means both icon registration and version-4 callbacks succeeded. An error
    requires the controller to keep/show its launcher so it remains reachable.

    start()/close() never wait for the native loop. close() requests teardown;
    wait_closed() is for a shutdown worker, not Tk. A controller must retain
    retiring instances and verify closure before an update handoff. Each new
    attempt uses a new NativeTray rather than restarting an exhausted thread.
    """
    def __init__(self, icon_path, on_event):
        self.icon_path = os.fspath(icon_path)
        self.on_event = on_event
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._started = False
        self._api = None
        self._thread_id = 0
        self._hwnd = None
        self._instance = None
        self._class_name = f"L2ChatTranslatorTray-{os.getpid()}-{id(self):x}"
        self._class_atom = 0
        self._wndproc_ref = None
        self._icon = None
        self._menu = None
        self._menu_open = False
        self._notify = None
        self._icon_added = False
        self._taskbar_created = 0
        self._error_sent = False
        self._cleanup_failed = False

    def start(self):
        """Start once without hiding any launcher or doing native work here."""
        failure = None
        with self._lock:
            if self._started or self._stop.is_set():
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, name="translator-notification-area", daemon=True)
            try:
                self._thread.start()
            except RuntimeError as exc:
                self._thread = None
                failure = exc
        if failure is not None:
            self._report_error(tr("The notification-area worker could not start."))

    def is_alive(self):
        """Report actual worker lifetime, including final native cleanup."""
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def wait_closed(self, timeout: float = 5.0) -> bool:
        """Return whether the worker stopped and released its native resources.

        Intended for a shutdown worker; never waits on its own thread. A false
        result includes native cleanup failures, not only a join timeout, so an
        updater cannot treat an exited but incompletely cleaned thread as ready.
        """
        seconds = float(timeout)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Tray shutdown timeout must be finite and nonnegative.")
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(seconds)
        return not thread.is_alive() and not self._cleanup_failed

    def close(self):
        """Request owner-thread teardown, including a menu currently open there.

        close-before-start and repeated close calls are harmless. The startup
        worker checks the stop flag before reporting readiness, so cancellation
        cannot later authorize hiding the launcher. Never destroy HWNDs from
        this calling thread. WM_QUIT is only a fallback if posting WM_CLOSE fails.
        """
        self._stop.set()
        with self._lock:
            api, hwnd, thread_id = self._api, self._hwnd, self._thread_id
        if api is not None and hwnd:
            try:
                if not api.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                    if not api.PostThreadMessageW(thread_id, WM_QUIT, 0, 0):
                        self._report_error(tr("Windows could not request notification-area cleanup."))
            except Exception:
                self._report_error(tr("Windows could not request notification-area cleanup."))

    def _emit(self, event, message=None):
        # A failed caller callback must not escape a ctypes WNDPROC or leave a
        # native loop alive after the launcher's data queue becomes unavailable.
        try:
            self.on_event(event, message)
            return True
        except Exception:
            self._stop.set()
            return False

    def _report_error(self, message):
        with self._lock:
            if self._error_sent:
                return
            self._error_sent = True
        self._emit("error", message)

    def _add_icon(self):
        if not self._api.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._notify)):
            raise OSError(tr("Windows could not add the notification-area icon."))
        self._icon_added = True
        self._notify.uVersion = NOTIFYICON_VERSION_4
        if not self._api.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(self._notify)):
            raise OSError(tr("Windows could not enable notification-area controls."))

    def _remove_icon(self):
        if self._icon_added:
            if not self._api.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._notify)):
                return False
            self._icon_added = False
        return True

    def _show_menu(self, hwnd, position):
        """Show only two fixed commands; no player text or paths enter a menu."""
        api = self._api
        if self._menu is None:
            self._menu = api.CreatePopupMenu()
            if not self._menu:
                raise OSError(tr("Windows could not create the notification-area menu."))
            for command, label in ((OPEN_COMMAND, tr("Open translator")), (EXIT_COMMAND, tr("Exit"))):
                if not api.AppendMenuW(self._menu, MF_STRING, command, label):
                    raise OSError(tr("Windows could not create the notification-area menu controls."))
        x = ctypes.c_int16(position & 0xFFFF).value
        y = ctypes.c_int16((position >> 16) & 0xFFFF).value
        if (x, y) == (-1, -1):
            point = POINT()
            if not api.GetCursorPos(ctypes.byref(point)):
                raise OSError(tr("Windows could not position the notification-area menu."))
            x, y = point.x, point.y
        # Microsoft requires foreground ownership, then WM_NULL after dismissal,
        # so clicking elsewhere dismisses the menu and a second opening works.
        api.SetForegroundWindow(hwnd)
        self._menu_open = True
        try:
            command = api.TrackPopupMenu(self._menu,
                TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, x, y, 0, hwnd, None)
        finally:
            self._menu_open = False
            if not self._stop.is_set():
                api.PostMessageW(hwnd, WM_NULL, 0, 0)
                api.Shell_NotifyIconW(NIM_SETFOCUS, ctypes.byref(self._notify))
        if not self._stop.is_set():
            if command == OPEN_COMMAND:
                self._emit("restore")
            elif command == EXIT_COMMAND:
                self._emit("exit")

    def _window_proc(self, hwnd, message, wparam, lparam):
        """Handle messages for our HWND only; callbacks queue fixed plain data."""
        try:
            if message == WM_CLOSE:
                if not self._stop.is_set():
                    self._report_error(tr("The notification-area controls closed unexpectedly."))
                self._stop.set()
                if self._menu_open:
                    self._api.EndMenu()
                self._remove_icon()
                if not self._api.DestroyWindow(hwnd):
                    raise OSError(tr("Windows could not close the notification-area receiver."))
                return 0
            if message == WM_DESTROY:
                if not self._stop.is_set():
                    self._report_error(tr("The notification-area controls closed unexpectedly."))
                self._stop.set()
                self._remove_icon()
                with self._lock:
                    self._hwnd = None
                self._api.PostQuitMessage(0)
                return 0
            if self._stop.is_set():
                return self._api.DefWindowProcW(hwnd, message, wparam, lparam)
            if message == self._taskbar_created and self._taskbar_created:
                # Explorer discarded its old registration. Re-add both the icon
                # and callback version, or report failure so the UI restores.
                self._icon_added = False
                self._add_icon()
                return 0
            if message == WM_TRAY:
                event, icon_id = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
                if icon_id != ICON_ID:
                    return 0
                # Version 4 already converts left-button/keyboard activation
                # into NIN_SELECT/NIN_KEYSELECT and a right click into
                # WM_CONTEXTMENU. Handling legacy mouse messages as well can
                # deliver a duplicate action or reopen a dismissed menu.
                if event in (NIN_SELECT, NIN_KEYSELECT):
                    self._emit("restore")
                elif event == WM_CONTEXTMENU:
                    self._show_menu(hwnd, wparam)
                return 0
            return self._api.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception as exc:
            # Exceptions must never unwind through the native callback boundary.
            self._report_error(str(exc) or "The notification-area controls failed.")
            self._stop.set()
            try:
                self._api.PostQuitMessage(1)
            except Exception:
                pass  # No Python exception may escape the native callback ABI.
            return 0

    def _run(self):
        try:
            api = _Windows()
            with self._lock:
                self._api = api
                self._thread_id = api.GetCurrentThreadId()
            if self._stop.is_set():
                return
            self._instance = api.GetModuleHandleW(None)
            if not self._instance:
                raise OSError(tr("Windows could not identify the notification-area module."))
            self._icon = api.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not self._icon:
                raise OSError(tr("The translator notification-area icon could not be loaded."))
            self._taskbar_created = api.RegisterWindowMessageW("TaskbarCreated")
            if not self._taskbar_created:
                raise OSError(tr("Windows could not register notification-area recovery."))
            self._wndproc_ref = WNDPROC(self._window_proc)
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._wndproc_ref
            window_class.hInstance = self._instance
            window_class.lpszClassName = self._class_name
            self._class_atom = api.RegisterClassW(ctypes.byref(window_class))
            if not self._class_atom:
                raise OSError(tr("Windows could not register the notification-area receiver."))
            hwnd = api.CreateWindowExW(WS_EX_TOOLWINDOW, self._class_name, "", WS_POPUP,
                                       0, 0, 0, 0, None, None, self._instance, None)
            if not hwnd:
                raise OSError(tr("Windows could not create the notification-area receiver."))
            with self._lock:
                self._hwnd = hwnd
            self._notify = NOTIFYICONDATAW()
            self._notify.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._notify.hWnd = hwnd
            self._notify.uID = ICON_ID
            self._notify.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
            self._notify.uCallbackMessage = WM_TRAY
            self._notify.hIcon = self._icon
            self._notify.szTip = TOOLTIP
            if self._stop.is_set():
                return
            self._add_icon()
            if self._stop.is_set() or not self._emit("ready"):
                return
            message = MSG()
            while not self._stop.is_set():
                result = api.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise OSError(tr("The notification-area message loop failed."))
                if result == 0:
                    if not self._stop.is_set():
                        raise OSError(tr("The notification-area controls stopped unexpectedly."))
                    break
                api.TranslateMessage(ctypes.byref(message))
                api.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._report_error(str(exc) or "The notification-area controls could not start.")
        finally:
            self._stop.set()
            self._cleanup()

    def _cleanup(self):
        """Release every acquired object on its owner thread, including failures."""
        api = self._api
        if api is None:
            return
        # Continue independent releases after a failure; one bad native return
        # must not prevent deleting the icon or releasing the class callback.
        actions = [self._remove_icon]
        if self._menu_open:
            actions.append(lambda: api.EndMenu())
        with self._lock:
            hwnd = self._hwnd
        if hwnd:
            actions.append(lambda: api.DestroyWindow(hwnd))
        if self._menu:
            actions.append(lambda: api.DestroyMenu(self._menu))
        if self._icon:
            actions.append(lambda: api.DestroyIcon(self._icon))
        if self._class_atom:
            actions.append(lambda: api.UnregisterClassW(self._class_name, self._instance))
        for action in actions:
            try:
                if action() == 0:
                    self._cleanup_failed = True
                    self._report_error(tr("Windows could not finish notification-area cleanup."))
            except Exception:
                self._cleanup_failed = True
                self._report_error(tr("Windows could not finish notification-area cleanup."))
        with self._lock:
            self._hwnd = None
        self._menu = self._icon = None
        self._class_atom = 0
        # Keep _wndproc_ref and its library wrappers through thread exit. A late
        # native callback must never refer to garbage-collected Python code.
