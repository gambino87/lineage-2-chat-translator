"""Existing-process activation, tested without windows, services or foreground changes."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
import unittest
import uuid
from unittest.mock import Mock, call, patch

import app_activation as activation


class Root:
    """Deterministic Tk scheduler; no Tcl interpreter or native window."""
    def __init__(self):
        self.timers = {}
        self.sequence = 0
        self.deiconify = Mock()
        self.lift = Mock()
        self.winfo_id = Mock(return_value=100)

    def after(self, delay, callback):
        self.sequence += 1
        self.timers[self.sequence] = (delay, callback)
        return self.sequence

    def after_cancel(self, timer):
        self.timers.pop(timer, None)

    def fire(self, timer):
        _, callback = self.timers.pop(timer)
        callback()


class SignalTests(unittest.TestCase):
    def api(self, pid=1234):
        self.pid = ctypes.c_uint32(pid)
        api = Mock()
        api.OpenEventW.return_value = 11
        api.OpenFileMappingW.return_value = 22
        api.MapViewOfFile.return_value = ctypes.addressof(self.pid)
        api.AllowSetForegroundWindow.return_value = True
        api.SetEvent.return_value = True
        return api

    def test_permission_is_for_owner_pid_before_signal_and_all_resources_close(self):
        api = self.api()
        self.assertTrue(activation._signal_existing(api, "Local\\TestActivation"))
        api.OpenEventW.assert_called_once_with(activation.EVENT_MODIFY_STATE, False, "Local\\TestActivation")
        api.OpenFileMappingW.assert_called_once_with(activation.FILE_MAP_READ, False, "Local\\TestActivationOwner")
        calls = api.mock_calls
        self.assertLess(calls.index(call.AllowSetForegroundWindow(1234)), calls.index(call.SetEvent(11)))
        api.UnmapViewOfFile.assert_called_once_with(ctypes.addressof(self.pid))
        self.assertEqual(api.CloseHandle.call_args_list, [call(22), call(11)])

    def test_foreground_permission_denial_still_signals_restoration(self):
        api = self.api()
        api.AllowSetForegroundWindow.return_value = False
        self.assertTrue(activation._signal_existing(api, "test"))
        api.SetEvent.assert_called_once_with(11)

    def test_missing_event_never_reads_mapping_or_signals(self):
        api = self.api()
        api.OpenEventW.return_value = None
        self.assertFalse(activation._signal_existing(api, "test"))
        api.OpenFileMappingW.assert_not_called()
        api.SetEvent.assert_not_called()
        api.CloseHandle.assert_not_called()

    def test_mapping_failure_still_closes_open_event(self):
        api = self.api()
        api.OpenFileMappingW.return_value = None
        self.assertFalse(activation._signal_existing(api, "test"))
        api.CloseHandle.assert_called_once_with(11)
        api.SetEvent.assert_not_called()

    def test_view_failure_closes_mapping_and_event_without_unmapping(self):
        api = self.api()
        api.MapViewOfFile.return_value = None
        self.assertFalse(activation._signal_existing(api, "test"))
        self.assertEqual(api.CloseHandle.call_args_list, [call(22), call(11)])
        api.UnmapViewOfFile.assert_not_called()
        api.SetEvent.assert_not_called()

    def test_zero_owner_pid_is_never_granted_permission_or_signaled(self):
        api = self.api(pid=0)
        self.assertFalse(activation._signal_existing(api, "test"))
        api.AllowSetForegroundWindow.assert_not_called()
        api.SetEvent.assert_not_called()
        api.UnmapViewOfFile.assert_called_once()
        self.assertEqual(api.CloseHandle.call_count, 2)

    def test_failed_signal_and_native_exception_release_every_resource(self):
        for failure in (False, OSError("signal unavailable")):
            with self.subTest(failure=failure):
                api = self.api()
                if isinstance(failure, Exception):
                    api.SetEvent.side_effect = failure
                    with self.assertRaises(OSError):
                        activation._signal_existing(api, "test")
                else:
                    api.SetEvent.return_value = failure
                    self.assertFalse(activation._signal_existing(api, "test"))
                api.UnmapViewOfFile.assert_called_once()
                self.assertEqual(api.CloseHandle.call_args_list, [call(22), call(11)])


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(activation.os, "name", "nt"))
        self.factory = self.enterContext(patch.object(activation, "_Windows"))
        self.signal = self.enterContext(patch.object(activation, "_signal_existing"))
        self.sleep = self.enterContext(patch.object(activation.time, "sleep"))

    def test_success_exits_immediately_without_waiting(self):
        self.signal.return_value = True
        self.assertTrue(activation.activate_existing(name="test"))
        self.signal.assert_called_once_with(self.factory.return_value, "test")
        self.sleep.assert_not_called()

    def test_listener_startup_race_retries_then_succeeds(self):
        self.signal.side_effect = [False, True]
        with patch.object(activation.time, "monotonic", side_effect=[10.0, 10.01]):
            self.assertTrue(activation.activate_existing(name="test"))
        self.assertEqual(self.signal.call_count, 2)
        self.sleep.assert_called_once_with(activation.RETRY_INTERVAL_SECONDS)

    def test_retry_deadline_is_bounded(self):
        self.signal.return_value = False
        with patch.object(activation.time, "monotonic", side_effect=[10.0, 10.01, 12.01]):
            self.assertFalse(activation.activate_existing(name="test"))
        self.assertEqual(self.signal.call_count, 2)
        self.sleep.assert_called_once_with(activation.RETRY_INTERVAL_SECONDS)

    def test_native_unavailability_returns_failure_without_waiting(self):
        self.factory.side_effect = OSError("native unavailable")
        self.assertFalse(activation.activate_existing(name="test"))
        self.signal.assert_not_called()
        self.sleep.assert_not_called()

    def test_signal_error_returns_failure_without_falling_back_to_gui(self):
        self.signal.side_effect = OSError("mapping unavailable")
        self.assertFalse(activation.activate_existing(name="test"))
        self.sleep.assert_not_called()

    def test_non_windows_path_does_no_native_work(self):
        with patch.object(activation.os, "name", "posix"):
            self.assertFalse(activation.activate_existing(name="test"))
        self.factory.assert_not_called()
        self.signal.assert_not_called()


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(activation.os, "name", "nt"))
        self.factory = self.enterContext(patch.object(activation, "_Windows"))
        self.api = self.factory.return_value
        self.api.CreateEventW.return_value = 11
        self.api.WaitForSingleObject.return_value = activation.WAIT_TIMEOUT
        self.api.GetAncestor.return_value = 101
        self.mapping_factory = self.enterContext(patch.object(activation.mmap, "mmap"))
        self.mapping = self.mapping_factory.return_value
        self.root = Root()

    def listener(self):
        listener = activation.ActivationListener(name="test")
        self.addCleanup(listener.close)
        return listener

    def test_constructing_only_creates_data_free_event_and_owner_mapping(self):
        listener = self.listener()
        self.api.CreateEventW.assert_called_once_with(None, False, False, "test")
        self.mapping.__setitem__.assert_called_once_with(slice(None), os.getpid().to_bytes(activation.PID_BYTES, "little"))
        self.assertIsNone(listener._root)
        self.api.GetAncestor.assert_not_called()
        self.api.SetForegroundWindow.assert_not_called()

    def test_idle_poll_is_nonblocking_and_never_restores_without_request(self):
        listener = self.listener()
        listener.bind(self.root)
        self.root.fire(listener._timer)
        self.api.WaitForSingleObject.assert_called_once_with(11, 0)
        self.root.deiconify.assert_not_called()
        self.root.lift.assert_not_called()
        self.api.SetForegroundWindow.assert_not_called()
        self.assertEqual(self.root.timers[listener._timer][0], activation.POLL_INTERVAL_MS)

    def test_request_uses_restore_callback_then_owned_wrapper_foreground(self):
        listener = self.listener()
        restore = Mock()
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        listener.bind(self.root, restore)
        self.root.fire(listener._timer)
        restore.assert_called_once_with()
        self.root.deiconify.assert_not_called()
        self.api.GetAncestor.assert_called_once_with(100, activation.GA_ROOT)
        self.api.SetForegroundWindow.assert_called_once_with(101)
        self.assertIn(listener._timer, self.root.timers)

    def test_plain_restore_deiconifies_and_lifts_existing_root(self):
        listener = self.listener()
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        listener.bind(self.root)
        self.root.fire(listener._timer)
        self.root.deiconify.assert_called_once_with()
        self.root.lift.assert_called_once_with()

    def test_missing_native_wrapper_never_foregrounds_another_window(self):
        listener = self.listener()
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        self.api.GetAncestor.return_value = None
        listener.bind(self.root)
        self.root.fire(listener._timer)
        self.root.deiconify.assert_called_once()
        self.api.SetForegroundWindow.assert_not_called()

    def test_unbind_cancels_timer_but_preserves_process_scoped_resources(self):
        listener = self.listener()
        listener.bind(self.root)
        listener.unbind()
        self.assertFalse(self.root.timers)
        self.assertIsNone(listener._root)
        self.api.CloseHandle.assert_not_called()
        self.mapping.close.assert_not_called()
        self.api.WaitForSingleObject.assert_not_called()

    def test_handoff_never_restores_old_root_and_polls_new_root(self):
        listener = self.listener()
        listener.bind(self.root)
        stale = self.root.timers[listener._timer][1]
        other = Root()
        listener.bind(other)
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        stale()
        self.root.deiconify.assert_not_called()
        self.api.WaitForSingleObject.assert_not_called()
        other.fire(next(iter(other.timers)))
        other.deiconify.assert_called_once()

    def test_rebind_same_root_invalidates_queued_old_callback(self):
        listener = self.listener()
        old_restore, current_restore = Mock(), Mock()
        listener.bind(self.root, old_restore)
        stale = self.root.timers[listener._timer][1]
        listener.bind(self.root, current_restore)
        current_timer = listener._timer
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        stale()
        old_restore.assert_not_called()
        self.assertEqual(listener._timer, current_timer)
        self.assertEqual(len(self.root.timers), 1)
        self.root.fire(current_timer)
        current_restore.assert_called_once()

    def test_destroyed_root_stops_poll_without_native_foreground(self):
        listener = self.listener()
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        self.root.deiconify.side_effect = tk.TclError("root destroyed")
        self.root.winfo_id.side_effect = tk.TclError("root destroyed")
        listener.bind(self.root)
        self.root.fire(listener._timer)
        self.assertFalse(self.root.timers)
        self.assertIsNone(listener._root)
        self.api.SetForegroundWindow.assert_not_called()

    def test_restore_cleanup_error_does_not_permanently_disable_activation(self):
        listener = self.listener()
        self.api.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        restore = Mock(side_effect=OSError("tray teardown failed"))
        listener.bind(self.root, restore)
        self.root.fire(listener._timer)
        self.assertIn(listener._timer, self.root.timers)

    def test_close_is_idempotent_and_cancels_poll_before_releasing_resources(self):
        listener = self.listener()
        listener.bind(self.root)
        self.api.CloseHandle.side_effect = lambda _: self.assertFalse(self.root.timers)
        listener.close()
        listener.close()
        self.api.CloseHandle.assert_called_once_with(11)
        self.mapping.close.assert_called_once_with()
        self.assertIsNone(listener._event)
        self.assertIsNone(listener._mapping)

    def test_failed_event_creation_releases_mapping(self):
        self.api.CreateEventW.return_value = None
        with patch.object(activation.ctypes, "WinError", return_value=OSError("event failed"), create=True), \
             patch.object(activation.ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaises(OSError):
                activation.ActivationListener(name="test")
        self.mapping.close.assert_called_once_with()
        self.api.CloseHandle.assert_not_called()

    def test_non_windows_listener_never_creates_native_objects(self):
        with patch.object(activation.os, "name", "posix"):
            listener = activation.ActivationListener(name="test")
            listener.bind(self.root)
            listener.close()
        self.factory.assert_not_called()
        self.mapping_factory.assert_not_called()
        self.assertFalse(self.root.timers)


@unittest.skipUnless(os.name == "nt", "Windows named event/mapping integration")
class NativeSignalTests(unittest.TestCase):
    def test_separate_process_grants_owner_permission_before_signaling_parent(self):
        # A fixed child program opens the named objects independently. It never
        # creates Tk, inspects windows, or calls any actual user32 operation.
        child_program = """
import json
import os
import sys
import app_activation as activation

api = activation._Windows()
calls = []
owner = int(sys.argv[2])
def allow(pid):
    calls.append(["allow", pid])
    return True
def forbidden(*args):
    raise AssertionError("A native window operation was attempted")
set_event = api.SetEvent
def signal(handle):
    if calls != [["allow", owner]]:
        raise AssertionError("Owner foreground permission must precede signal")
    calls.append(["signal"])
    return set_event(handle)
api.AllowSetForegroundWindow = allow
api.GetAncestor = forbidden
api.SetForegroundWindow = forbidden
api.SetEvent = signal
ok = activation._signal_existing(api, sys.argv[1])
print(json.dumps({"ok": ok, "calls": calls, "pid": os.getpid()}))
raise SystemExit(0 if ok else 1)
"""
        api = activation._Windows()
        name = "Local\\L2CTActivationChildTest" + uuid.uuid4().hex
        with patch.object(activation, "_Windows", return_value=api), \
             patch.object(api, "AllowSetForegroundWindow", side_effect=AssertionError("Unexpected parent user32 call")), \
             patch.object(api, "GetAncestor", side_effect=AssertionError("Unexpected parent user32 call")), \
             patch.object(api, "SetForegroundWindow", side_effect=AssertionError("Unexpected parent user32 call")):
            listener = activation.ActivationListener(name=name)
            try:
                self.assertEqual(api.WaitForSingleObject(listener._event, 0), activation.WAIT_TIMEOUT)
                result = subprocess.run(
                    [sys.executable, "-B", "-c", child_program, name, str(os.getpid())],
                    cwd=Path(activation.__file__).resolve().parent,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(report["ok"])
                self.assertNotEqual(report["pid"], os.getpid())
                self.assertEqual(report["calls"], [["allow", os.getpid()], ["signal"]])
                self.assertEqual(api.WaitForSingleObject(listener._event, 0), activation.WAIT_OBJECT_0)
                self.assertEqual(api.WaitForSingleObject(listener._event, 0), activation.WAIT_TIMEOUT)
            finally:
                listener.close()
            self.assertFalse(activation._signal_existing(api, name))

    def test_unique_native_signal_survives_startup_and_root_handoff_and_coalesces(self):
        # Real anonymous-PID mapping and event only. Every user32 operation is
        # replaced so this regression cannot inspect or activate actual windows.
        api = activation._Windows()
        name = "Local\\L2CTActivationTest" + uuid.uuid4().hex
        with patch.object(activation, "_Windows", return_value=api), \
             patch.object(api, "AllowSetForegroundWindow", return_value=True) as allow, \
             patch.object(api, "GetAncestor", return_value=101), \
             patch.object(api, "SetForegroundWindow", return_value=True) as foreground:
            listener = activation.ActivationListener(name=name)
            try:
                self.assertTrue(activation.activate_existing(name=name))
                self.assertTrue(activation.activate_existing(name=name))
                root = Root()
                listener.bind(root)
                root.fire(listener._timer)
                root.deiconify.assert_called_once()
                root.fire(listener._timer)
                root.deiconify.assert_called_once()  # Auto-reset coalesces noise.
                listener.unbind()
                self.assertTrue(activation.activate_existing(name=name))
                other = Root()
                listener.bind(other)
                other.fire(listener._timer)
                other.deiconify.assert_called_once()
                self.assertEqual(allow.call_args_list, [call(os.getpid())] * 3)
                self.assertEqual(foreground.call_count, 2)
            finally:
                listener.close()
            self.assertFalse(activation._signal_existing(api, name))


if __name__ == "__main__":
    unittest.main()
