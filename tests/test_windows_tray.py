"""Native tray lifecycle/ABI checks with fake APIs only; no desktop UI is made."""
import ctypes
import os
import queue
import threading
import unittest
from unittest.mock import Mock, patch

import windows_tray as tray


class FakeWindows:
    """Inert message queue, native return values and resource accounting."""
    HWND = 0x123400001000
    ICON = 0x123400002000
    INSTANCE = 0x123400003000
    MENU = 0x123400004000
    TASKBAR_CREATED = 0xC123

    def __init__(self):
        self.messages = queue.Queue()
        self.waiting = threading.Event()
        self.calls = []
        self.notifications = []
        self.fail_shell = set()
        self.callback = None
        self.menu_command = 0
        self.GetCurrentThreadId = self.function("thread_id", 123)
        self.GetModuleHandleW = self.function("module", self.INSTANCE)
        self.LoadImageW = self.function("load_icon", self.ICON)
        self.RegisterWindowMessageW = self.function("register_message", self.TASKBAR_CREATED)
        self.RegisterClassW = self.function("register_class", self.register_class)
        self.CreateWindowExW = self.function("create_window", self.HWND)
        self.Shell_NotifyIconW = self.function("notify", self.notify)
        self.GetMessageW = self.function("get_message", self.get_message)
        self.TranslateMessage = self.function("translate", 1)
        self.DispatchMessageW = self.function("dispatch", self.dispatch)
        self.DefWindowProcW = self.function("default", 71)
        self.PostMessageW = self.function("post", self.post)
        self.PostThreadMessageW = self.function("post_thread", self.post_thread)
        self.PostQuitMessage = self.function("quit", lambda code: self.messages.put(None))
        self.DestroyWindow = self.function("destroy_window", self.destroy_window)
        self.DestroyIcon = self.function("destroy_icon", 1)
        self.UnregisterClassW = self.function("unregister_class", 1)
        self.CreatePopupMenu = self.function("create_menu", self.MENU)
        self.AppendMenuW = self.function("append_menu", 1)
        self.TrackPopupMenu = self.function("track_menu", lambda *args: self.menu_command)
        self.SetForegroundWindow = self.function("foreground", 1)
        self.GetCursorPos = self.function("cursor", self.cursor)
        self.EndMenu = self.function("end_menu", 1)
        self.DestroyMenu = self.function("destroy_menu", 1)

    def function(self, name, value):
        def invoke(*args):
            self.calls.append(name)
            return value(*args) if callable(value) else value
        return Mock(side_effect=invoke)

    def register_class(self, pointer):
        self.callback = pointer._obj.lpfnWndProc
        return 12

    def notify(self, operation, pointer):
        data = pointer._obj
        self.notifications.append((operation, data.hWnd, data.uID, data.uFlags,
                                   data.uCallbackMessage, data.hIcon, data.szTip, data.uVersion))
        return int(operation not in self.fail_shell)

    def send(self, message, wparam=0, lparam=0):
        self.messages.put((message, wparam, lparam))

    def send_icon(self, event, position=0, icon_id=tray.ICON_ID):
        self.send(tray.WM_TRAY, position, (icon_id << 16) | event)

    def get_message(self, pointer, hwnd, lower, upper):
        self.waiting.set()
        message = self.messages.get(timeout=2)
        if message is None:
            return 0
        if isinstance(message, int):
            return message
        target = pointer._obj
        target.hwnd = self.HWND
        target.message, target.wParam, target.lParam = message
        return 1

    def dispatch(self, pointer):
        message = pointer._obj
        return self.callback(message.hwnd, message.message, message.wParam, message.lParam)

    def post(self, hwnd, message, wparam, lparam):
        self.send(message, wparam, lparam)
        return 1

    def post_thread(self, thread_id, message, wparam, lparam):
        self.messages.put(None)
        return 1

    def destroy_window(self, hwnd):
        self.callback(hwnd, tray.WM_DESTROY, 0, 0)
        return 1

    def cursor(self, pointer):
        pointer._obj.x, pointer._obj.y = -600, 300
        return 1


class NativeTrayTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWindows()
        self.events = queue.Queue()
        self.instances = []
        self.patch = patch.object(tray, "_Windows", return_value=self.api)
        self.factory = self.patch.start()

    def tearDown(self):
        for instance in self.instances:
            instance.close()
            instance.wait_closed(3)
        self.patch.stop()

    def create(self, callback=None):
        instance = tray.NativeTray("assets/L2CT.ico", callback or
                                  (lambda event, message=None: self.events.put((event, message))))
        self.instances.append(instance)
        return instance

    def next_event(self, expected):
        event, message = self.events.get(timeout=2)
        self.assertEqual(event, expected)
        return message

    def start_ready(self):
        instance = self.create()
        instance.start()
        self.assertIsNone(self.next_event("ready"))
        self.assertTrue(self.api.waiting.wait(2))
        return instance

    def test_constructing_backend_performs_no_native_work(self):
        instance = self.create()
        self.factory.assert_not_called()
        self.assertFalse(instance.is_alive())
        self.assertTrue(instance.wait_closed(0))
        self.assertTrue(self.events.empty())

    def test_ready_follows_add_and_version_then_close_releases_native_objects(self):
        instance = self.start_ready()
        self.assertTrue(instance.is_alive())
        operations = [row[0] for row in self.api.notifications]
        self.assertEqual(operations, [tray.NIM_ADD, tray.NIM_SETVERSION])
        added = self.api.notifications[0]
        self.assertEqual(added[1:3], (self.api.HWND, tray.ICON_ID))
        self.assertEqual(added[3], tray.NIF_MESSAGE | tray.NIF_ICON | tray.NIF_TIP | tray.NIF_SHOWTIP)
        self.assertEqual(added[4:7], (tray.WM_TRAY, self.api.ICON, tray.TOOLTIP))
        self.assertEqual(self.api.notifications[1][-1], tray.NOTIFYICON_VERSION_4)
        args = self.api.CreateWindowExW.call_args.args
        self.assertEqual(args[0], tray.WS_EX_TOOLWINDOW)
        self.assertEqual(args[3], tray.WS_POPUP)
        self.assertFalse(args[3] & 0x10000000)  # WS_VISIBLE must never be added.
        self.assertIsNone(args[8])  # Top-level, not HWND_MESSAGE; receives broadcasts.
        instance.start()
        self.factory.assert_called_once()
        instance.close()
        self.assertTrue(instance.wait_closed(2))
        instance.close()
        self.assertFalse(instance.is_alive())
        self.assertEqual([row[0] for row in self.api.notifications],
                         [tray.NIM_ADD, tray.NIM_SETVERSION, tray.NIM_DELETE])
        self.api.DestroyWindow.assert_called_once_with(self.api.HWND)
        self.api.DestroyIcon.assert_called_once_with(self.api.ICON)
        self.api.UnregisterClassW.assert_called_once_with(instance._class_name, self.api.INSTANCE)
        self.assertLess(self.api.calls.index("destroy_window"), self.api.calls.index("destroy_icon"))
        self.assertLess(self.api.calls.index("destroy_icon"), self.api.calls.index("unregister_class"))

    def test_close_before_start_does_not_spawn_or_emit_readiness(self):
        instance = self.create()
        instance.close()
        instance.start()
        self.factory.assert_not_called()
        self.assertTrue(instance.wait_closed(0))
        self.assertTrue(self.events.empty())

    def test_close_during_startup_never_emits_ready_and_releases_acquired_handles(self):
        entered, release = threading.Event(), threading.Event()
        def load(*args):
            entered.set()
            release.wait(2)
            return self.api.ICON
        self.api.LoadImageW.side_effect = load
        instance = self.create()
        instance.start()
        self.assertTrue(entered.wait(2))
        try:
            instance.close()
        finally:
            release.set()
        self.assertTrue(instance.wait_closed(2))
        self.assertTrue(self.events.empty())
        self.assertEqual(self.api.notifications, [])
        self.api.DestroyIcon.assert_called_once_with(self.api.ICON)
        self.api.DestroyWindow.assert_called_once_with(self.api.HWND)

    def test_missing_icon_reports_error_and_never_registers_window(self):
        self.api.LoadImageW.side_effect = None
        self.api.LoadImageW.return_value = 0
        instance = self.create()
        instance.start()
        self.assertIn("could not be loaded", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.api.RegisterClassW.assert_not_called()
        self.api.CreateWindowExW.assert_not_called()
        self.assertTrue(self.events.empty())

    def test_failed_add_cannot_emit_ready_and_still_cleans_window_icon_class(self):
        self.api.fail_shell.add(tray.NIM_ADD)
        instance = self.create()
        instance.start()
        self.assertIn("could not add", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.assertEqual([row[0] for row in self.api.notifications], [tray.NIM_ADD])
        self.api.DestroyWindow.assert_called_once()
        self.api.DestroyIcon.assert_called_once()
        self.api.UnregisterClassW.assert_called_once()
        self.assertTrue(self.events.empty())

    def test_failed_version_removes_already_added_icon_and_never_emits_ready(self):
        self.api.fail_shell.add(tray.NIM_SETVERSION)
        instance = self.create()
        instance.start()
        self.assertIn("could not enable", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.assertEqual([row[0] for row in self.api.notifications],
                         [tray.NIM_ADD, tray.NIM_SETVERSION, tray.NIM_DELETE])
        self.assertTrue(self.events.empty())

    def test_left_click_and_keyboard_activation_emit_only_plain_restore_events(self):
        instance = self.start_ready()
        for event in (tray.NIN_SELECT, tray.NIN_KEYSELECT):
            self.api.send_icon(event)
            self.assertIsNone(self.next_event("restore"))
        self.api.send_icon(tray.NIN_SELECT, icon_id=99)
        self.api.send_icon(tray.NIN_KEYSELECT)
        self.next_event("restore")  # Prior wrong icon ID emitted nothing.
        self.assertTrue(self.events.empty())
        instance.close()
        self.assertTrue(instance.wait_closed(2))

    def test_context_menu_fixed_commands_signed_position_and_dismissal_order(self):
        instance = self.start_ready()
        self.api.menu_command = tray.OPEN_COMMAND
        position = ((-300 & 0xFFFF) << 16) | (-1200 & 0xFFFF)
        self.api.send_icon(tray.WM_CONTEXTMENU, position)
        self.next_event("restore")
        self.assertEqual(self.api.AppendMenuW.call_args_list[0].args,
                         (self.api.MENU, tray.MF_STRING, tray.OPEN_COMMAND, "Open translator"))
        self.assertEqual(self.api.AppendMenuW.call_args_list[1].args,
                         (self.api.MENU, tray.MF_STRING, tray.EXIT_COMMAND, "Exit"))
        args = self.api.TrackPopupMenu.call_args.args
        self.assertEqual(args[2:4], (-1200, -300))
        self.assertEqual(args[1], tray.TPM_RIGHTBUTTON | tray.TPM_RETURNCMD | tray.TPM_NONOTIFY)
        self.assertLess(self.api.calls.index("foreground"), self.api.calls.index("track_menu"))
        self.assertIn((self.api.HWND, tray.WM_NULL, 0, 0),
                      [call.args for call in self.api.PostMessageW.call_args_list])
        self.api.menu_command = tray.EXIT_COMMAND
        self.api.send_icon(tray.WM_CONTEXTMENU, position)
        self.next_event("exit")
        self.api.CreatePopupMenu.assert_called_once()
        instance.close()
        self.assertTrue(instance.wait_closed(2))
        self.api.DestroyMenu.assert_called_once_with(self.api.MENU)

    def test_keyboard_context_menu_uses_cursor_fallback_and_cancel_emits_no_command(self):
        instance = self.start_ready()
        self.api.send_icon(tray.WM_CONTEXTMENU, 0xFFFFFFFF)
        # A subsequent activation is a queue barrier after menu cancellation.
        self.api.send_icon(tray.NIN_KEYSELECT)
        self.next_event("restore")
        self.api.GetCursorPos.assert_called_once()
        self.assertEqual(self.api.TrackPopupMenu.call_args.args[2:4], (-600, 300))
        self.assertTrue(self.events.empty())
        instance.close()

    def test_close_cancels_active_menu_and_never_delivers_its_late_command(self):
        entered = threading.Event()
        def menu(*args):
            entered.set()
            message = self.api.messages.get(timeout=2)
            self.assertEqual(message[0], tray.WM_CLOSE)
            self.api.callback(self.api.HWND, *message)
            return tray.EXIT_COMMAND
        self.api.TrackPopupMenu.side_effect = menu
        instance = self.start_ready()
        self.api.send_icon(tray.WM_CONTEXTMENU)
        self.assertTrue(entered.wait(2))
        instance.close()
        self.assertTrue(instance.wait_closed(2))
        self.api.EndMenu.assert_called_once()
        self.api.DestroyMenu.assert_called_once()
        self.assertTrue(self.events.empty())

    def test_explorer_restart_readds_icon_and_callback_version(self):
        instance = self.start_ready()
        self.api.send(self.api.TASKBAR_CREATED)
        self.api.send_icon(tray.NIN_KEYSELECT)
        self.next_event("restore")
        self.assertEqual([row[0] for row in self.api.notifications],
                         [tray.NIM_ADD, tray.NIM_SETVERSION, tray.NIM_ADD, tray.NIM_SETVERSION])
        self.assertTrue(self.events.empty())  # No duplicate initial ready event.
        instance.close()

    def test_failed_explorer_recovery_reports_error_and_closes_receiver(self):
        instance = self.start_ready()
        self.api.fail_shell.add(tray.NIM_ADD)
        self.api.send(self.api.TASKBAR_CREATED)
        self.assertIn("could not add", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.api.DestroyWindow.assert_called_once()
        self.api.DestroyIcon.assert_called_once()

    def test_message_loop_error_reports_failure_and_releases_every_resource(self):
        instance = self.start_ready()
        self.api.messages.put(-1)
        self.assertIn("message loop failed", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.api.DestroyWindow.assert_called_once()
        self.api.DestroyIcon.assert_called_once()
        self.api.UnregisterClassW.assert_called_once()

    def test_unexpected_message_loop_exit_reports_error_so_controller_restores(self):
        instance = self.start_ready()
        self.api.messages.put(None)
        self.assertIn("stopped unexpectedly", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))

    def test_unrequested_receiver_close_reports_error_so_hidden_launcher_can_restore(self):
        instance = self.start_ready()
        self.api.send(tray.WM_CLOSE)
        self.assertIn("closed unexpectedly", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.api.DestroyWindow.assert_called_once()

    def test_unrequested_receiver_destruction_reports_error_and_releases_icon(self):
        instance = self.start_ready()
        self.api.send(tray.WM_DESTROY)
        self.assertIn("closed unexpectedly", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.assertIn(tray.NIM_DELETE, [row[0] for row in self.api.notifications])
        self.api.DestroyIcon.assert_called_once()

    def test_menu_failure_never_escapes_callback_and_releases_partial_menu(self):
        instance = self.start_ready()
        self.api.AppendMenuW.side_effect = OSError("synthetic menu failure")
        self.api.send_icon(tray.WM_CONTEXTMENU)
        self.assertIn("synthetic menu failure", self.next_event("error"))
        self.assertTrue(instance.wait_closed(2))
        self.api.DestroyMenu.assert_called_once_with(self.api.MENU)

    def test_cleanup_failure_blocks_success_but_does_not_skip_other_releases(self):
        instance = self.start_ready()
        self.api.DestroyIcon.side_effect = OSError("synthetic icon cleanup failure")
        instance.close()
        self.assertIn("cleanup", self.next_event("error"))
        self.assertFalse(instance.wait_closed(2))
        self.assertFalse(instance.is_alive())
        self.api.UnregisterClassW.assert_called_once()
        self.assertTrue(self.events.empty())

    def test_close_post_failure_falls_back_to_thread_quit_then_owner_cleanup(self):
        instance = self.start_ready()
        self.api.PostMessageW.side_effect = None
        self.api.PostMessageW.return_value = 0
        instance.close()
        self.assertTrue(instance.wait_closed(2))
        self.api.PostThreadMessageW.assert_called_once_with(123, tray.WM_QUIT, 0, 0)
        self.api.DestroyWindow.assert_called_once()
        self.api.DestroyIcon.assert_called_once()

    def test_caller_callback_exception_does_not_strand_native_loop(self):
        instance = self.create(callback=Mock(side_effect=RuntimeError("queue unavailable")))
        instance.start()
        self.assertTrue(instance.wait_closed(2))
        self.api.DestroyWindow.assert_called_once()
        self.api.DestroyIcon.assert_called_once()

    def test_wait_closed_bounds_timeout_and_rejects_unbounded_values(self):
        instance = self.start_ready()
        self.assertFalse(instance.wait_closed(0))
        for value in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                instance.wait_closed(value)
        instance.close()
        self.assertTrue(instance.wait_closed(2))


class NativeAbiTests(unittest.TestCase):
    def test_native_signatures_use_pointer_width_for_handles_and_message_parameters(self):
        libraries = {}
        def library(name, **kwargs):
            libraries[name] = Mock()
            return libraries[name]
        with patch.object(tray.os, "name", "nt"), \
             patch.object(tray.ctypes, "WinDLL", side_effect=library, create=True):
            api = tray._Windows()
        self.assertIs(api.CreateWindowExW.restype, ctypes.c_void_p)
        self.assertIs(api.LoadImageW.restype, ctypes.c_void_p)
        self.assertIs(api.DefWindowProcW.restype, ctypes.c_ssize_t)
        self.assertEqual(api.DefWindowProcW.argtypes, [tray.HANDLE, tray.UINT, tray.WPARAM, tray.LPARAM])
        self.assertEqual(api.PostMessageW.argtypes, [tray.HANDLE, tray.UINT, tray.WPARAM, tray.LPARAM])
        self.assertIs(api.AppendMenuW.argtypes[2], ctypes.c_size_t)
        self.assertEqual(ctypes.sizeof(tray.WPARAM), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(tray.LPARAM), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(tray.UINT), 4)
        self.assertEqual(ctypes.sizeof(tray.LONG), 4)
        for item in libraries.values():
            self.assertEqual(item.mock_calls, [])  # Assigning declarations calls no API.

    @unittest.skipUnless(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8,
                         "Windows x64 Unicode structure ABI")
    def test_windows_x64_structure_sizes_and_offsets_match_sdk(self):
        self.assertEqual(ctypes.sizeof(tray.MSG), 48)
        self.assertEqual(ctypes.sizeof(tray.WNDCLASSW), 72)
        self.assertEqual(ctypes.sizeof(tray.NOTIFYICONDATAW), 976)
        self.assertEqual(tray.NOTIFYICONDATAW.hWnd.offset, 8)
        self.assertEqual(tray.NOTIFYICONDATAW.hIcon.offset, 32)
        self.assertEqual(tray.NOTIFYICONDATAW.szTip.offset, 40)
        self.assertEqual(tray.NOTIFYICONDATAW.guidItem.offset, 952)
        self.assertEqual(tray.NOTIFYICONDATAW.hBalloonIcon.offset, 968)
