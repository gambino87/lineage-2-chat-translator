"""Headless close/lifecycle checks; never stop a real process or contact Ollama."""
import json
import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

import translator_app as app_module


def closing_app(shutdown):
    app = app_module.TranslatorApp.__new__(app_module.TranslatorApp)
    app.root = Mock()
    app.stop = threading.Event()
    app.tail, app.reply, app.overlay = Mock(), Mock(), Mock()
    app.save_settings = Mock(return_value=True)
    app._closing = False
    app._shutdown_service = shutdown
    app._show_closing_state = Mock()
    return app


class ServiceShutdownTests(unittest.TestCase):
    def test_close_stops_reader_and_reply_before_service_then_destroys_root(self):
        entered, release = threading.Event(), threading.Event()
        app = closing_app(None)

        def shutdown():
            self.assertTrue(app.stop.is_set())
            app.tail.close.assert_called_once()
            app.reply.close.assert_called_once()
            app.save_settings.assert_called_once()
            app.overlay.destroy.assert_called_once()
            entered.set()
            release.wait(2)

        app._shutdown_service = Mock(side_effect=shutdown)
        app.close()
        try:
            self.assertTrue(entered.wait(1))
            app.close()
            app.root.destroy.assert_not_called()
            app.root.withdraw.assert_not_called()
            app._show_closing_state.assert_called_once()
            self.assertFalse(app._shutdown_thread.daemon)
            app._finish_shutdown()  # Poll while work is still pending.
            app.root.destroy.assert_not_called()
        finally:
            release.set()
            app._shutdown_thread.join(2)
        app._finish_shutdown()
        app._shutdown_service.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_failure_is_reported_on_ui_thread_and_window_still_closes(self):
        app = closing_app(Mock(side_effect=RuntimeError("owner changed")))
        app.close()
        app._shutdown_thread.join(2)
        with patch.object(app_module.messagebox, "showwarning") as warning:
            warning.side_effect = lambda *args, **kwargs: app.root.destroy.assert_not_called()
            app._finish_shutdown()
        self.assertIn("owner changed", warning.call_args.args[1])
        self.assertIn("will close", warning.call_args.args[1])
        self.assertIn("Could not confirm", warning.call_args.args[1])
        self.assertIs(warning.call_args.kwargs["parent"], app.root)
        app.root.destroy.assert_called_once()

    def test_unmanaged_connection_closes_without_service_calls(self):
        app = closing_app(None)
        app.close()
        app.root.destroy.assert_not_called()
        app._show_closing_state.assert_called_once()
        app._finish_shutdown()
        app.root.destroy.assert_called_once()
        app.root.withdraw.assert_not_called()

    def test_closing_view_removes_controls_and_keeps_progress_visible_until_done(self):
        app = closing_app(Mock())
        app._content = Mock()
        with patch.object(app_module.ttk, "Frame"), \
             patch.object(app_module.ttk, "Label") as label, \
             patch.object(app_module.ttk, "Progressbar") as progress:
            app._show_closing_state.side_effect = lambda: app_module.TranslatorApp._show_closing_state(app)
            app.close()
            app._shutdown_thread.join(2)
            app._content.destroy.assert_called_once()
            app.root.deiconify.assert_called_once()
            app.root.lift.assert_called_once()
            app.root.focus_set.assert_called_once()
            app.root.withdraw.assert_not_called()
            app.root.destroy.assert_not_called()
            self.assertTrue(any("Closing" in call.kwargs["text"] for call in label.call_args_list))
            progress.return_value.start.assert_called_once()
            progress.return_value.stop.assert_not_called()
            app._finish_shutdown()
            progress.return_value.stop.assert_called_once()
            app.root.destroy.assert_called_once()

    def test_teardown_errors_do_not_skip_service_shutdown(self):
        app = closing_app(Mock())
        app.tail.close.side_effect = OSError("log handle failed")
        app.overlay.destroy.side_effect = app_module.tk.TclError("overlay already gone")
        app.close()
        app._shutdown_thread.join(2)
        app._shutdown_service.assert_called_once()
        app.reply.close.assert_called_once()
        app.save_settings.assert_called_once()
        with patch.object(app_module.messagebox, "showwarning") as warning:
            app._finish_shutdown()
        self.assertIn("log handle failed", warning.call_args.args[1])
        self.assertIn("overlay already gone", warning.call_args.args[1])
        app.root.destroy.assert_called_once()

    def test_worker_does_not_start_language_detection_after_close(self):
        app = closing_app(None)
        app.jobs, app.events = queue.Queue(), queue.Queue()
        app.config = {"ollama_url": app_module.ENDPOINT, "model": "test-model"}
        record = app_module.ChatRecord("12:00:00", "Test", "Hola", 0)
        app.jobs.put((record, "English", "Live", 0))
        with patch.object(app_module, "OllamaClient") as client:
            def translate(*args, **kwargs):
                app.stop.set()  # Close happens while the first request is in flight.
                return "Hello"
            client.return_value.translate.side_effect = translate
            app._worker()
        client.return_value.translate.assert_called_once()
        client.return_value.detect_language.assert_not_called()
        self.assertEqual(app.jobs.unfinished_tasks, 0)
        self.assertEqual(list(app.events.queue), [("working", None)])

    def test_progress_view_error_does_not_skip_service_shutdown(self):
        app = closing_app(Mock())
        app._show_closing_state.side_effect = app_module.tk.TclError("widget already gone")
        app.close()
        app._shutdown_thread.join(2)
        app._shutdown_service.assert_called_once()
        with patch.object(app_module.messagebox, "showwarning") as warning:
            app._finish_shutdown()
        self.assertIn("widget already gone", warning.call_args.args[1])
        app.root.destroy.assert_called_once()

    def test_save_failure_is_reported_after_service_shutdown(self):
        app = closing_app(Mock())
        app.save_settings.return_value = False
        app.close()
        app._shutdown_thread.join(2)
        app._shutdown_service.assert_called_once()
        with patch.object(app_module.messagebox, "showwarning") as warning:
            app._finish_shutdown()
        self.assertIn("Saving preferences", warning.call_args.args[1])
        app.root.destroy.assert_called_once()

    def test_worker_does_not_start_queued_request_if_closed_while_waiting(self):
        app = closing_app(None)
        app.jobs, app.events = Mock(), queue.Queue()
        def get(**kwargs):
            app.stop.set()
            return (app_module.ChatRecord("12:00:00", "Test", "Hola", 0), "English", "Live", 0)
        app.jobs.get.side_effect = get
        with patch.object(app_module, "OllamaClient") as client:
            app._worker()
        client.assert_not_called()
        app.jobs.task_done.assert_called_once()

    def test_captured_runtime_is_stopped_once_even_when_saved_path_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            location = root / "runtime.local.json"
            location.write_text(json.dumps({"data_root": str(root / "original")}))
            with patch.object(app_module, "PROJECT", root), patch.object(app_module, "RuntimeManager") as manager:
                shutdown = app_module.service_shutdown_callback({"ollama_url": app_module.ENDPOINT})
                location.write_text(json.dumps({"data_root": str(root / "replacement")}))
                shutdown()
                shutdown()
                manager.assert_called_once_with(root / "original", progress=ANY)
                manager.return_value.stop_service.assert_called_once()

    def test_explicit_launcher_root_takes_precedence_and_custom_endpoint_is_unmanaged(self):
        with patch.object(app_module, "RuntimeManager") as manager:
            config = {"ollama_url": app_module.ENDPOINT + "/"}
            shutdown = app_module.service_shutdown_callback(config, Path("selected-runtime"))
            shutdown()
            manager.assert_called_once_with(Path("selected-runtime"), progress=ANY)
            manager.reset_mock()
            self.assertIsNone(app_module.service_shutdown_callback({"ollama_url": "http://127.0.0.1:11434"}, Path("selected-runtime")))
            manager.assert_not_called()

    def test_unavailable_location_does_not_guess_a_service_installation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app_module, "RuntimeManager") as manager:
            with patch.object(app_module, "PROJECT", Path(folder)):
                self.assertIsNone(app_module.service_shutdown_callback({"ollama_url": app_module.ENDPOINT}))
            manager.assert_not_called()

    def test_main_finally_cleans_service_after_constructor_failure(self):
        shutdown = Mock()
        with patch.object(app_module, "load_settings", return_value={}), \
             patch.object(app_module, "service_shutdown_callback", return_value=shutdown), \
             patch.object(app_module.tk, "Tk") as root, \
             patch.object(app_module, "TranslatorApp", side_effect=RuntimeError("UI failed")), \
             patch.object(app_module.messagebox, "showerror"):
            with self.assertRaisesRegex(RuntimeError, "UI failed"):
                app_module.main(runtime_root=Path("selected-runtime"))
        shutdown.assert_called_once()
        root.return_value.destroy.assert_called_once()

    def test_packaged_entrypoint_passes_its_saved_runtime_to_the_app(self):
        import installer_main
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "runtime.json"
            runtime.write_text(json.dumps({"data_root": str(Path(folder) / "packaged-runtime")}))
            with patch.object(installer_main, "RUNTIME_PATH", runtime), \
                 patch.object(installer_main, "SingleInstance") as mutex, \
                 patch.object(installer_main, "uninstall_in_progress", return_value=False), \
                 patch.object(installer_main, "update_in_progress", return_value=False), \
                 patch.object(installer_main, "cleanup_update_cache"), \
                 patch.object(installer_main, "consume_update_result", return_value=None), \
                 patch.object(installer_main, "detached_launch_notice"), \
                 patch.object(installer_main.tk, "Tk"), \
                 patch.object(installer_main, "SetupWindow") as setup, \
                 patch.object(app_module, "SETTINGS", app_module.SETTINGS), \
                 patch.object(app_module, "main") as launch:
                setup.return_value.launch_requested = True
                launch.side_effect = lambda **kwargs: mutex.return_value.close.assert_not_called()
                self.assertEqual(installer_main.main([]), 0)
            launch.assert_called_once_with(migrate_legacy_models=False, runtime_root=Path(folder) / "packaged-runtime")
            mutex.return_value.close.assert_called_once()

    def test_main_cleans_service_when_window_cannot_be_created(self):
        shutdown = Mock()
        with patch.object(app_module, "load_settings", return_value={}), \
             patch.object(app_module, "service_shutdown_callback", return_value=shutdown), \
             patch.object(app_module.tk, "Tk", side_effect=RuntimeError("Tk failed")), \
             patch.object(app_module.messagebox, "showerror") as error:
            with self.assertRaisesRegex(RuntimeError, "Tk failed"):
                app_module.main(runtime_root=Path("selected-runtime"))
        shutdown.assert_called_once()
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
