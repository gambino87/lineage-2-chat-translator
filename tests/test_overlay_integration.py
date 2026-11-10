"""Headless integration checks: no Tk instance, windows, or network services."""
import json
from collections import deque
from dataclasses import replace
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, create_autospec, patch

import translator_app
from chat_overlay import make_overlay_message, normalize_overlay_settings
from translator_core import ChatRecord, parse_chat_line


ORIGINS_LOG = r"C:\Games\Reborn\Origins\system\Network.log"


def placed_overlay():
    return normalize_overlay_settings({
        "enabled": True, "x": -900, "y": 80, "width": 720, "height": 310,
        "opacity": 0.72, "font_size": 15, "locked": True, "show_timestamps": True,
    })


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Transcript:
    """Only the text-widget operations used by message delivery and Clear."""
    def __init__(self):
        self.text = ""
        self.insertions = []
        self.marks = {}
        self.click_tags = ()
        self.view = (0.0, 1.0)

    def configure(self, **_kwargs):
        pass

    def insert(self, _position, text, tag):
        self.text += text
        self.insertions.append((text, tag))

    def index(self, _position):
        return f"{self.text.count(chr(10)) + 1}.0"

    def see(self, _position):
        pass

    def delete(self, _start, _end):
        end = len(self.text) if _end == "end" else self.marks[_end]
        self.text = self.text[end:]
        self.marks = {name: max(0, position - end) for name, position in self.marks.items()}
        if _end == "end":
            self.insertions.clear()

    def mark_set(self, name, position):
        self.marks[name] = len(self.text) if position == "end-1c" else 0

    def mark_gravity(self, *_args):
        pass

    def mark_unset(self, name):
        self.marks.pop(name, None)

    def tag_delete(self, *_args):
        pass

    def tag_names(self, _position):
        return self.click_tags

    def yview(self, *_args):
        return self.view

    def yview_moveto(self, _position):
        pass


def headless_app():
    """Use production handlers without constructing TranslatorApp's Tk UI."""
    app = translator_app.TranslatorApp.__new__(translator_app.TranslatorApp)
    app.root = Mock()
    app.config = dict(translator_app.DEFAULTS)
    app.config["overlay"] = normalize_overlay_settings({})
    app.jobs = queue.Queue(maxsize=80)
    app.events = queue.Queue()
    app.stop = threading.Event()
    app.tail = None
    app.reading = True
    app.connected = True
    app.completed = 0
    app.busy = False
    app.screen_generation = 0
    app.translation_error = False
    app.last_file_error = ""
    app._rows = deque()
    app._reply_records = {}
    app._row_sequence = 0
    app.target = Value("English")
    app.log_path = Value(ORIGINS_LOG)
    app.notices = Value(False)
    app.show_original = Value(True)
    app.status = Value("")
    app.counter = Value("")
    app.connection = Value("LOCAL · READY")
    app.transcript = Transcript()
    app.overlay = create_autospec(translator_app.ChatOverlay, instance=True)
    app.overlay.settings = normalize_overlay_settings({})
    app.overlay_button = Mock()
    app.read_button = Mock()
    app.reply = Mock()
    return app


class OverlayMessageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = headless_app()
        self.record = ChatRecord("23:59:58", "Player", "Ищем хилера в пати.", 25)

    def finish_one_job(self, *, translated=None, failure=None, language="Russian", detection_failure=None):
        def translate(_text, **_options):
            if failure is not None:
                raise RuntimeError(failure)
            return translated

        task_done = self.app.jobs.task_done
        def finish_job():
            task_done()
            self.app.stop.set()  # End after delivery, not during translation.

        client = Mock()
        client.translate.side_effect = translate
        client.translate_record.side_effect = translate
        client.detect_language.return_value = language
        if detection_failure:
            client.detect_language.side_effect = RuntimeError(detection_failure)
        with patch.object(translator_app, "OllamaClient", return_value=client), \
             patch.object(self.app.jobs, "task_done", side_effect=finish_job):
            self.app._worker()
        self.app.stop.clear()
        self.app._drain_events()
        return client

    def test_worker_result_reaches_main_transcript_and_overlay_once(self):
        self.app.enqueue(self.record, "Live")
        client = self.finish_one_job(translated="Looking for a healer for the party.")
        client.translate.assert_called_once_with(self.record.message)
        self.app.overlay.add_message.assert_called_once_with(
            replace(self.record, source_language="Russian"), "Looking for a healer for the party.", source="Live", error=False)
        self.assertIn(self.record.message, self.app.transcript.text)
        self.assertIn("Player: Looking for a healer for the party.  Reply", self.app.transcript.text)
        self.assertEqual(self.app.completed, 1)
        self.assertEqual(self.app.jobs.unfinished_tasks, 0)
        self.app._drain_events()
        self.assertEqual(self.app.overlay.add_message.call_count, 1)

    def test_worker_failure_preserves_original_and_marks_overlay_error(self):
        self.app.enqueue(self.record, "Live")
        self.finish_one_job(failure="Local model is unavailable")
        self.app.overlay.add_message.assert_called_once_with(
            self.record, "Local model is unavailable", source="Live", error=True)
        self.assertIn(self.record.message, self.app.transcript.text)
        self.assertIn("Translation unavailable", self.app.transcript.text)
        self.assertEqual(self.app.completed, 0)
        self.assertTrue(self.app.translation_error)

    def test_detection_failure_keeps_translation_and_allows_reply_retry(self):
        self.app.enqueue(self.record, "Live")
        self.finish_one_job(translated="Looking for a healer.", detection_failure="Language service timed out")
        record, translated = self.app.overlay.add_message.call_args.args
        self.assertIsNone(record.source_language)
        self.assertEqual(translated, "Looking for a healer.")
        self.assertEqual(self.app.completed, 1)
        self.assertFalse(self.app.translation_error)

    def test_ambiguous_source_is_marked_unknown_instead_of_translation_target(self):
        self.app.enqueue(self.record, "Live")
        self.finish_one_job(translated="WTS sword", language=None)
        record = self.app.overlay.add_message.call_args.args[0]
        self.assertEqual(record.source_language, "Unknown")

    def test_universal_trade_reaches_overlay_with_neutral_language_without_model_detection(self):
        source = replace(self.record, message="WTS [Club] +6 10kk PM me", item_links=("[Club]",))
        self.app.enqueue(source, "Live")
        client = self.finish_one_job(translated=source.message, language="English")
        delivered = self.app.overlay.add_message.call_args.args[0]
        self.assertEqual(delivered.source_language, "Universal trade")
        self.assertEqual(delivered.item_links, source.item_links)
        client.detect_language.assert_not_called()

    def test_trade_with_natural_language_keeps_detected_language(self):
        source = replace(self.record, message="WTS [Club] aceito ofertas", item_links=("[Club]",))
        self.app.enqueue(source, "Live")
        client = self.finish_one_job(translated="WTS [Club] accepting offers", language="Portuguese")
        self.assertEqual(self.app.overlay.add_message.call_args.args[0].source_language, "Portuguese")
        client.detect_language.assert_called_once_with(source.message, item_links=source.item_links)

    def test_repeated_text_reuses_language_and_translation_for_each_sender(self):
        second = replace(self.record, sender="Another player")
        self.app.enqueue(self.record, "Live")
        self.app.enqueue(second, "Live")
        get_job = self.app.jobs.get

        def next_job(timeout):
            if self.app.jobs.empty():
                self.app.stop.set()
                raise queue.Empty
            return get_job(timeout=timeout)

        client = Mock()
        client.translate.return_value = "Looking for a healer."
        client.detect_language.return_value = "Russian"
        with patch.object(self.app.jobs, "get", side_effect=next_job), \
             patch.object(translator_app, "OllamaClient", return_value=client):
            self.app._worker()
        self.app.stop.clear()
        self.app._drain_events()
        client.translate.assert_called_once_with(self.record.message)
        client.detect_language.assert_called_once_with(self.record.message)
        delivered = [call.args[0] for call in self.app.overlay.add_message.call_args_list]
        self.assertEqual([record.sender for record in delivered], [self.record.sender, second.sender])
        self.assertEqual([record.source_language for record in delivered], ["Russian", "Russian"])

    def test_party_details_reach_translation_detection_and_both_displays(self):
        record = replace(self.record,
                         message="Ищу хилера [12~19] (8/9), продаю [Shining Knife]",
                         item_links=("[Shining Knife]",), party_details=("[12~19]", "(8/9)"))
        self.app.enqueue(record, "Live")
        translated = "Looking for a healer [12~19] (8/9), selling [Shining Knife]"
        client = self.finish_one_job(translated=translated)
        options = {"item_links": record.item_links, "party_details": record.party_details}
        client.translate.assert_called_once_with(record.message, **options)
        client.detect_language.assert_called_once_with(record.message, **options)
        delivered = replace(record, source_language="Russian")
        self.app.overlay.add_message.assert_called_once_with(delivered, translated, source="Live", error=False)
        self.assertIn(translated, self.app.transcript.text)
        self.assertEqual(next(iter(self.app._reply_records.values())).party_details, record.party_details)

    def test_plain_text_and_party_details_have_separate_cache_entries(self):
        plain = replace(self.record, message="Ищу хилера [12~19] (8/9)")
        party = replace(plain, sender="PartyPlayer", party_details=("[12~19]", "(8/9)"))
        repeated_party = replace(party, sender="AnotherPartyPlayer")
        for record in (plain, party, repeated_party):
            self.app.enqueue(record, "Live")
        get_job = self.app.jobs.get

        def next_job(timeout):
            if self.app.jobs.empty():
                self.app.stop.set()
                raise queue.Empty
            return get_job(timeout=timeout)

        client = Mock()
        client.translate.side_effect = ["Plain chat translation", "Party listing translation"]
        client.detect_language.return_value = "Russian"
        with patch.object(self.app.jobs, "get", side_effect=next_job), \
             patch.object(translator_app, "OllamaClient", return_value=client):
            self.app._worker()
        self.app.stop.clear()
        self.app._drain_events()
        self.assertEqual(client.translate.call_count, 2)
        self.assertEqual(client.translate.call_args_list[0].kwargs, {})
        self.assertEqual(client.translate.call_args_list[1].kwargs, {"party_details": party.party_details})
        self.assertEqual(client.detect_language.call_count, 2)
        delivered = [call.args[1] for call in self.app.overlay.add_message.call_args_list]
        self.assertEqual(delivered, ["Plain chat translation", "Party listing translation", "Party listing translation"])

    def test_compact_row_has_reply_without_repeating_original_or_language_labels(self):
        self.app.show_original.set(False)
        self.app.enqueue(self.record, "Live")
        self.finish_one_job(translated="Looking for a healer.")
        self.assertEqual(self.app.transcript.text, "23:59:58  Player: Looking for a healer.  Reply\n")
        self.assertNotIn(self.record.message, self.app.transcript.text)

    def test_reply_link_selects_the_exact_original_message(self):
        self.app._append(self.record, "Hello", "English", "Live", .1)
        second = ChatRecord("00:00:00", "Player", "Hola, buscamos un grupo.", 25)
        self.app._append(second, "Hello, looking for a group.", "English", "Live", .1)
        self.app.transcript.click_tags = ("reply", self.app._rows[0][0])
        self.app._reply_clicked(Mock(x=10, y=10))
        self.app.reply.select.assert_called_once_with(self.record, whisper=False)
        self.app.root.deiconify.assert_called_once()

    def test_overlay_name_reply_forwards_whisper_mode_and_exact_record(self):
        record = replace(self.record, sender="ExamplePlayer", source_language="Spanish")
        self.app.select_reply(record, whisper=True)
        self.app.reply.select.assert_called_once_with(record, whisper=True)
        self.app.root.deiconify.assert_called_once()
        self.app.root.lift.assert_called_once()

    def test_latest_reply_click_owns_the_deferred_focus_request(self):
        self.app.root.after_idle.side_effect = ["first-focus", "second-focus"]
        self.app.select_reply(self.record)
        first = self.app.root.after_idle.call_args.args[0]
        self.app.select_reply(replace(self.record, sender="OtherPlayer"), whisper=True)
        second = self.app.root.after_idle.call_args.args[0]
        self.app.root.after_cancel.assert_called_once_with("first-focus")
        first()
        self.app.reply.input.focus_force.assert_not_called()
        second()
        self.app.reply.input.focus_force.assert_called_once()
        self.assertIsNone(self.app._reply_focus_timer)

    def test_clear_cancels_pending_reply_focus(self):
        self.app.root.after_idle.return_value = "pending-focus"
        self.app.select_reply(self.record)
        focus = self.app.root.after_idle.call_args.args[0]
        self.app.clear()
        self.app.root.after_cancel.assert_called_once_with("pending-focus")
        focus()
        self.app.reply.input.focus_force.assert_not_called()

    def test_close_cancels_reply_focus_and_ignores_further_selections(self):
        self.app.root.after_idle.return_value = "pending-focus"
        self.app.save_settings = Mock(return_value=True)
        self.app.select_reply(self.record)
        focus = self.app.root.after_idle.call_args.args[0]
        self.app.close()
        self.app.root.after_cancel.assert_called_once_with("pending-focus")
        focus()
        self.app.select_reply(self.record, whisper=True)
        self.app.reply.input.focus_force.assert_not_called()
        self.app.reply.select.assert_called_once_with(self.record, whisper=False)
        self.app.root.deiconify.assert_called_once()

    def test_incoming_chat_never_requests_reply_focus(self):
        self.app._append(self.record, "Hello", "English", "Live", .1)
        self.app.root.after_idle.assert_not_called()
        self.app.root.deiconify.assert_not_called()
        self.app.reply.input.focus_force.assert_not_called()

    def test_original_toggle_rerenders_without_duplicate_translations_or_lost_links(self):
        self.app._append(self.record, "Hello", "English", "Live", .1)
        tag = self.app._rows[0][0]
        self.app.show_original.set(False)
        self.app._render_rows()
        self.assertNotIn(self.record.message, self.app.transcript.text)
        self.app.show_original.set(True)
        self.app._render_rows()
        self.assertIn(self.record.message, self.app.transcript.text)
        self.assertIs(self.app._reply_records[tag], self.record)
        self.assertEqual(self.app.completed, 1)
        self.app.overlay.add_message.assert_called_once()

    def test_bounded_history_releases_old_reply_records_and_clear_resets_selection(self):
        self.app.show_original.set(False)
        for index in range(252):
            self.app._append(self.record, f"Message {index}", "English", "Live", 0)
        self.assertEqual(len(self.app._rows), 250)
        self.assertEqual(len(self.app._reply_records), 250)
        self.assertNotIn("main_reply_1", self.app._reply_records)
        self.assertNotIn("Player: Message 0  Reply", self.app.transcript.text)
        self.assertIn("Message 251", self.app.transcript.text)
        self.app.clear()
        self.assertEqual(len(self.app._reply_records), 0)
        self.assertEqual(len(self.app._rows), 0)
        self.app.reply.clear_selection.assert_called_once()

    def test_linked_item_names_reach_model_and_both_displays_without_metadata(self):
        raw = ("Продам \x08\tType=1 \tID=123 \tColor=0 \tUnderline=0 "
               "\tClassID=8191 \tTitle=\x1bNecklace of Frintezza\x1b\x08 за 10kk")
        record = parse_chat_line(
            f"(19:51:50) Log: (Receive)[S_SAY2] SayPacket2ID : 123 Name : ExampleTrader Say Msg:{raw} Type:25")
        self.app.enqueue(record, "Live")
        translated = "Selling [Necklace of Frintezza] for 10kk"
        client = self.finish_one_job(translated=translated)
        client.translate_record.assert_called_once_with(record)
        client.translate.assert_not_called()
        self.app.overlay.add_message.assert_called_once_with(
            replace(record, source_language="Russian"), translated, source="Live", error=False)
        overlay_line = make_overlay_message(*self.app.overlay.add_message.call_args.args)
        self.assertEqual(overlay_line.translated, translated)
        self.assertEqual(overlay_line.original, record.message)
        self.assertEqual(overlay_line.channel, 25)
        self.assertIn(record.message, self.app.transcript.text)
        self.assertIn(translated, self.app.transcript.text)
        for unwanted in ("Type=", "ClassID=", "Title=", "\x08", "\x1b"):
            self.assertNotIn(unwanted, self.app.transcript.text)

    def test_clear_cancels_pending_and_suppresses_inflight_old_generation(self):
        for _ in range(3):
            self.app.enqueue(self.record, "Live")
        inflight = self.app.jobs.get_nowait()
        self.app.clear()
        self.assertTrue(self.app.jobs.empty())
        self.assertEqual(self.app.jobs.unfinished_tasks, 1)
        self.app.overlay.clear.assert_called_once()
        record, target, source, generation = inflight
        self.app.events.put(("result", (record, "Old result", target, source, 0.1, False, generation)))
        self.app.jobs.task_done()
        self.app._drain_events()
        self.app.overlay.add_message.assert_not_called()
        self.assertEqual(self.app.transcript.text, "")
        self.assertEqual(self.app.completed, 0)
        self.assertEqual(self.app.jobs.unfinished_tasks, 0)

        self.app.enqueue(self.record, "Live")
        self.finish_one_job(translated="Fresh result")
        self.app.overlay.add_message.assert_called_once_with(
            replace(self.record, source_language="Russian"), "Fresh result", source="Live", error=False)
        self.assertNotIn("Old result", self.app.transcript.text)

    def test_delayed_error_from_cleared_generation_does_not_poison_new_screen(self):
        self.app.clear()
        self.app.events.put(("result", (self.record, "Old error", "English", "Live", 0, True, 0)))
        self.app._drain_events()
        self.app.overlay.add_message.assert_not_called()
        self.assertFalse(self.app.translation_error)
        self.assertEqual(self.app.connection.get(), "LOCAL · READY")

    def test_clear_removes_error_state_and_reference_to_deleted_message(self):
        self.app.enqueue(self.record, "Live")
        self.finish_one_job(failure="Model failed")
        self.assertTrue(self.app.translation_error)
        self.app.clear()
        self.assertFalse(self.app.translation_error)
        self.assertEqual(self.app.connection.get(), "LOCAL · READY")
        self.assertNotIn("failed", self.app.status.get().lower())
        self.assertNotIn("above", self.app.status.get().lower())
        self.assertEqual(self.app.transcript.text, "")


class OverlaySettingsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.folder.name) / "settings.json"
        self.patch_settings = patch.object(translator_app, "SETTINGS", self.settings_path)
        self.patch_settings.start()

    def tearDown(self):
        self.patch_settings.stop()
        self.folder.cleanup()

    def test_old_settings_gain_overlay_defaults_without_losing_origins_path(self):
        self.settings_path.write_text(json.dumps({
            "log_path": ORIGINS_LOG,
            "model": "qwen3:4b",
            "target_language": "English",
            "include_notices": True,
        }), encoding="utf-8")
        restored = translator_app.load_settings()
        self.assertEqual(restored["log_path"], ORIGINS_LOG)
        self.assertEqual(restored["model"], "qwen3.5:9b-q4_K_M")
        self.assertTrue(restored["include_notices"])
        self.assertEqual(restored["overlay"], normalize_overlay_settings({}))
        self.assertIsNot(restored["overlay"], translator_app.DEFAULTS["overlay"])

    def test_removed_model_saved_by_old_window_migrates_without_losing_preferences(self):
        saved = {
            "log_path": ORIGINS_LOG, "model": "qwen2.5:3b",
            "ollama_url": "http://127.0.0.1:11435", "target_language": "Portuguese",
            "overlay": placed_overlay(), "include_notices": True, "show_original": True,
        }
        self.settings_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = translator_app.load_settings()
        self.assertEqual(restored, {**saved, "model": "qwen3.5:9b-q4_K_M", "minimize_to_tray": False,
                                    "close_to_tray": False, "ui_language": "en"})

    def test_upgrade_keeps_new_model_and_unrelated_model_selections(self):
        for model in ("qwen3.5:9b-q4_K_M", "another-local-model:7b"):
            with self.subTest(model=model):
                self.settings_path.write_text(json.dumps({"model": model}), encoding="utf-8")
                self.assertEqual(translator_app.load_settings()["model"], model)

    def test_malformed_overlay_settings_do_not_discard_other_preferences(self):
        self.settings_path.write_text(json.dumps({
            "log_path": ORIGINS_LOG, "target_language": "Portuguese", "overlay": "corrupt value"
        }), encoding="utf-8")
        restored = translator_app.load_settings()
        self.assertEqual(restored["log_path"], ORIGINS_LOG)
        self.assertEqual(restored["target_language"], "Portuguese")
        self.assertEqual(restored["overlay"], normalize_overlay_settings({}))

    def test_windows_bom_settings_preserve_selected_log_and_overlay(self):
        expected_overlay = placed_overlay()
        self.settings_path.write_text(json.dumps({
            "log_path": ORIGINS_LOG, "target_language": "Russian", "overlay": expected_overlay
        }), encoding="utf-8-sig")
        restored = translator_app.load_settings()
        self.assertEqual(restored["log_path"], ORIGINS_LOG)
        self.assertEqual(restored["target_language"], "Russian")
        self.assertEqual(restored["overlay"], expected_overlay)

    def test_overlay_change_persists_active_log_and_normalized_preferences(self):
        app = headless_app()
        app.overlay.settings = placed_overlay()
        app._overlay_changed(app.overlay.settings)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["log_path"], ORIGINS_LOG)
        self.assertEqual(saved["overlay"], app.overlay.settings)
        self.assertTrue(saved["overlay"]["enabled"])
        self.assertEqual((saved["overlay"]["x"], saved["overlay"]["y"]), (-900, 80))
        self.assertEqual((saved["overlay"]["width"], saved["overlay"]["height"]), (720, 310))
        self.assertEqual(translator_app.load_settings()["overlay"], app.overlay.settings)
        self.assertFalse(self.settings_path.with_suffix(".tmp").exists())

    def test_close_preserves_active_log_and_latest_overlay_settings(self):
        app = headless_app()
        app.overlay.settings = placed_overlay()
        app.tail = Mock()
        app.close()
        restored = translator_app.load_settings()
        self.assertEqual(restored["log_path"], ORIGINS_LOG)
        self.assertEqual(restored["overlay"], app.overlay.settings)
        self.assertTrue(app.stop.is_set())
        app.tail.close.assert_called_once()
        app.overlay.destroy.assert_called_once()

    def test_failed_write_preserves_previous_settings(self):
        original = {"log_path": ORIGINS_LOG, "target_language": "English"}
        self.settings_path.write_text(json.dumps(original), encoding="utf-8")
        app = headless_app()
        with patch.object(Path, "write_text", side_effect=OSError("disk unavailable")):
            app.save_settings()
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8")), original)
        self.assertIn("Could not save preferences", app.status.get())


if __name__ == "__main__":
    unittest.main()
