"""Read-only folder setup and headless first-run integration checks."""
import codecs
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from game_log_setup import resolve_system_log
from translator_core import LogTail
import translator_app


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def app_without_ui(log_path="", connected=False, reading=False):
    app = translator_app.TranslatorApp.__new__(translator_app.TranslatorApp)
    app.root = Mock()
    app.config = dict(translator_app.DEFAULTS)
    app.log_path = Value(log_path)
    app.target = Value("English")
    app.notices = Value(False)
    app.show_original = Value(False)
    app.status = Value("")
    app.connected = connected
    app.reading = reading
    app.tail = Mock() if reading else None
    app.stop = threading.Event()
    app.events = queue.Queue()
    app.jobs = queue.Queue()
    app.read_button = Mock()
    app.recent_button = Mock()
    app.settings_panel = Mock()
    app.settings_button = Mock()
    app.reply = Mock()
    app.connection = Value("")
    app._note = Mock()
    app._choosing_system_folder = False
    return app


class SystemFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Lineage II 玩家"
        self.system = self.root / "system"
        self.system.mkdir(parents=True)
        (self.system / "L2.ini").write_bytes(b"Original client configuration\r\n")

    def test_system_folder_resolves_log_without_creating_it(self):
        expected = self.system / "Network.log"
        self.assertEqual(resolve_system_log(self.system), expected)
        self.assertFalse(expected.exists())

    def test_game_root_resolves_system_child_in_space_and_unicode_path(self):
        self.assertEqual(resolve_system_log(self.root), self.system / "Network.log")

    def test_existing_log_retains_actual_spelling(self):
        log = self.system / "network.LOG"
        log.write_bytes(b"Saved client log")
        self.assertEqual(resolve_system_log(self.system), log)

    def test_network_log_itself_can_identify_system_folder(self):
        (self.system / "L2.ini").unlink()
        log = self.system / "Network.log"
        log.write_bytes(b"")
        self.assertEqual(resolve_system_log(self.system), log)

    def test_missing_empty_unrelated_and_file_paths_are_rejected(self):
        unrelated = Path(self.temp.name) / "another" / "system"
        unrelated.mkdir(parents=True)
        for selected in ("", " ", self.root / "missing", self.system / "L2.ini",
                         Path(self.temp.name), unrelated):
            with self.subTest(selected=selected):
                with self.assertRaises(ValueError):
                    resolve_system_log(selected)

    def test_log_directory_is_rejected(self):
        (self.system / "Network.log").mkdir()
        with self.assertRaisesRegex(ValueError, "Network.log is a folder"):
            resolve_system_log(self.system)

    def test_resolution_never_changes_game_files(self):
        (self.system / "Network.log").write_bytes(b"Original client chat\r\n")
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        resolve_system_log(self.root)
        resolve_system_log(self.system)
        after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_missing_selected_log_is_read_when_game_creates_it(self):
        log = resolve_system_log(self.system)
        tail = LogTail(log, start_at_end=True)
        self.addCleanup(tail.close)
        self.assertEqual(tail.poll(), [])
        self.assertFalse(log.exists())
        line = ("(12:34:56) Log: (Receive)[S_SAY2] SayPacket2ID : 123 "
                "Name : Player Say Msg:Hello from the game Type:25\r\n")
        # Simulate the game creating its own log after setup, not the resolver.
        log.write_bytes(codecs.BOM_UTF16_LE + line.encode("utf-16-le"))
        messages = tail.poll()
        self.assertEqual([record.message for record in messages], ["Hello from the game"])
        self.assertEqual(tail.poll(), [])


class FolderSelectionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.system = self.root / "Game folder" / "system"
        self.system.mkdir(parents=True)
        (self.system / "L2.ini").write_text("Unmodified", encoding="utf-8")
        self.settings = self.root / "preferences.json"
        patcher = patch.object(translator_app, "SETTINGS", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_selecting_folder_saves_exact_log_path_before_connection(self):
        app = app_without_ui()
        with patch.object(translator_app.filedialog, "askdirectory", return_value=str(self.system)), \
             patch.object(translator_app, "LogTail") as tail:
            app.choose_system_folder()
        expected = str(self.system / "Network.log")
        self.assertEqual(app.log_path.get(), expected)
        self.assertEqual(json.loads(self.settings.read_text())["log_path"], expected)
        self.assertFalse(app.reading)
        tail.assert_not_called()
        app._note.assert_called_once()
        self.assertFalse((self.system / "Network.log").exists())

    def test_selected_folder_starts_reader_when_connected(self):
        app = app_without_ui(connected=True)
        with patch.object(translator_app.filedialog, "askdirectory", return_value=str(self.system)), \
             patch.object(translator_app, "LogTail") as tail:
            app.choose_system_folder()
        tail.assert_called_once_with(str(self.system / "Network.log"), start_at_end=True)
        tail.return_value.poll.assert_called_once()
        self.assertTrue(app.reading)

    def test_save_failure_preserves_previous_file_and_reports_temporary_selection(self):
        original = {"log_path": "previous/Network.log"}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        (self.system / "Network.log").write_bytes(b"")
        app = app_without_ui("previous/Network.log", connected=True)
        with patch.object(translator_app.filedialog, "askdirectory", return_value=str(self.system)), \
             patch.object(Path, "write_text", side_effect=OSError("disk unavailable")), \
             patch.object(translator_app, "LogTail"):
            app.choose_system_folder()
        self.assertEqual(json.loads(self.settings.read_text()), original)
        self.assertEqual(app.log_path.get(), str(self.system / "Network.log"))
        app._note.assert_called_once()
        self.assertIn("could not be saved", app._note.call_args.args[0])
        self.assertTrue(app._note.call_args.kwargs["error"])

    def test_changing_folder_closes_old_reader_and_starts_new_one_once(self):
        app = app_without_ui("previous/Network.log", connected=True, reading=True)
        old_tail = app.tail
        with patch.object(translator_app.filedialog, "askdirectory", return_value=str(self.system)), \
             patch.object(translator_app, "LogTail") as tail:
            app.choose_system_folder()
        old_tail.close.assert_called_once()
        tail.assert_called_once_with(str(self.system / "Network.log"), start_at_end=True)
        self.assertTrue(app.reading)

    def test_cancel_preserves_path_and_running_reader(self):
        app = app_without_ui("previous/Network.log", connected=True, reading=True)
        old_tail = app.tail
        with patch.object(translator_app.filedialog, "askdirectory", return_value=""), \
             patch.object(app, "restart_log") as restart:
            app.choose_system_folder()
        self.assertEqual(app.log_path.get(), "previous/Network.log")
        self.assertIs(app.tail, old_tail)
        self.assertTrue(app.reading)
        restart.assert_not_called()
        self.assertFalse(self.settings.exists())

    def test_invalid_selection_keeps_old_path_and_reports_error(self):
        app = app_without_ui("previous/Network.log", connected=True, reading=True)
        with patch.object(translator_app.filedialog, "askdirectory", return_value=str(self.root)), \
             patch.object(translator_app.messagebox, "showerror") as error, \
             patch.object(app, "restart_log") as restart:
            app.choose_system_folder()
        error.assert_called_once()
        restart.assert_not_called()
        self.assertEqual(app.log_path.get(), "previous/Network.log")
        self.assertTrue(app.reading)

    def test_fresh_settings_have_no_machine_specific_log_path(self):
        self.assertEqual(translator_app.load_settings()["log_path"], "")
        self.assertEqual(translator_app.DEFAULTS["log_path"], "")

    def test_existing_saved_log_path_and_preferences_survive_loading(self):
        original = {"log_path": r"D:\My L2\system\Network.log", "target_language": "Spanish"}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        loaded = translator_app.load_settings()
        self.assertEqual(loaded["log_path"], original["log_path"])
        self.assertEqual(loaded["target_language"], "Spanish")

    def test_setup_path_reaches_first_launch_reader_without_another_folder_dialog(self):
        import installer_main
        log = str(self.system / "Network.log")
        installer_main.save_configuration(self.root / "data", translator_app.DEFAULTS["model"], log,
                                          self.settings, self.root / "runtime.json")
        app = app_without_ui(translator_app.load_settings()["log_path"])
        app.events.put(("connected", None))
        with patch.object(translator_app.filedialog, "askdirectory") as picker, \
             patch.object(translator_app, "LogTail") as tail:
            app._first_run_log_setup()
            app._drain_events()
        picker.assert_not_called()
        app.settings_panel.grid.assert_not_called()
        tail.assert_called_once_with(log, start_at_end=True)
        self.assertTrue(app.reading)
        self.assertFalse(Path(log).exists())

    def test_first_run_explains_missing_folder_inline_without_opening_picker(self):
        for path, stopped, expected in (("", False, 1), ("saved/Network.log", False, 0), ("", True, 0)):
            with self.subTest(path=path, stopped=stopped):
                app = app_without_ui(path)
                if stopped:
                    app.stop.set()
                with patch.object(app, "choose_system_folder") as choose, \
                     patch.object(translator_app.filedialog, "askdirectory") as picker:
                    app._first_run_log_setup()
                choose.assert_not_called()
                picker.assert_not_called()
                self.assertEqual(app.settings_panel.grid.call_count, expected)
                if expected:
                    self.assertIn("Settings → System folder", app.status.get())
                    app.settings_button.configure.assert_called_once_with(text="Hide settings")
                else:
                    app.settings_button.configure.assert_not_called()

    def test_blank_path_never_opens_current_directory_as_log(self):
        app = app_without_ui(" ", connected=True)
        with patch.object(app, "choose_system_folder") as choose, \
             patch.object(translator_app, "LogTail") as tail, \
             patch.object(translator_app, "read_recent_chat") as recent:
            app.toggle_reading()
            app.translate_recent()
        self.assertEqual(choose.call_count, 2)
        tail.assert_not_called()
        recent.assert_not_called()
        self.assertFalse(app.reading)

    def test_connecting_without_path_does_not_open_another_picker(self):
        app = app_without_ui()
        app.events.put(("connected", None))
        with patch.object(app, "choose_system_folder") as choose, \
             patch.object(translator_app, "LogTail") as tail:
            app._drain_events()
        self.assertTrue(app.connected)
        self.assertFalse(app.reading)
        choose.assert_not_called()
        tail.assert_not_called()

    def test_reentrant_picker_is_ignored(self):
        app = app_without_ui()

        def modal_callback(**_kwargs):
            if picker.call_count == 1:
                app.choose_system_folder()
            return ""

        with patch.object(translator_app.filedialog, "askdirectory", side_effect=modal_callback) as picker:
            app.choose_system_folder()
        self.assertEqual(picker.call_count, 1)


if __name__ == "__main__":
    unittest.main()
