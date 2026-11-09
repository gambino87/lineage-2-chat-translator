"""Reply controller checks without Tk windows, clipboard changes, or HTTP."""
import queue
from dataclasses import replace
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from reply_composer import CHOOSE_LANGUAGE, DETECTING_LANGUAGE, ReplyComposer, whisper_recipient
from translator_core import ChatRecord


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def composer():
    panel = ReplyComposer.__new__(ReplyComposer)
    panel.config = {"ollama_url": "http://127.0.0.1:11435", "model": "qwen2.5:3b"}
    panel.root = Mock()
    panel.connected = True
    panel._closed = False
    panel._stop = threading.Event()
    panel._jobs = queue.Queue(maxsize=1)
    panel._events = queue.Queue()
    panel._selection = 0
    panel._revision = 0
    panel._record = None
    panel._recipient = ""
    panel._translating = None
    panel._waiting_reply = None
    panel._result_context = None
    panel._timer = None
    panel.title = Value()
    panel.language = Value(CHOOSE_LANGUAGE)
    panel.draft = Value()
    panel.output = Value()
    panel.status = Value()
    panel.input = Mock()
    panel.translate_button = Mock()
    panel.copy_button = Mock()
    return panel


def take_job(panel):
    job = panel._jobs.get_nowait()
    panel._jobs.task_done()
    return job


def deliver(panel, job, value=None, error=None):
    panel._events.put((job, value, error))
    panel._drain_events()


class ReplyComposerTests(unittest.TestCase):
    def setUp(self):
        self.panel = composer()
        self.russian = ChatRecord("12:00:00", "Player1", "Привет! Ищем хилера.", 25)
        self.spanish = ChatRecord("12:00:01", "Player2", "Hola, buscamos un sanador.", 25)

    def ready_reply(self, text="I can heal. How many people are in the party?", language="Russian"):
        self.panel.select(self.russian)
        detected = take_job(self.panel)
        deliver(self.panel, detected, language)
        self.assertEqual(self.panel.title.get(), f"Reply in {language}")
        self.panel.draft.set(text)
        self.panel._draft_changed()
        self.panel.translate()
        return take_job(self.panel)

    def test_select_detects_exact_original_and_keeps_english_draft(self):
        self.panel.draft.set("Do you still need a healer?")
        record = ChatRecord("12:00:00", "Player1", "Продам [Sword]", 25, ("[Sword]",))
        self.panel.select(record)
        job = take_job(self.panel)
        self.assertEqual((job.kind, job.text, job.item_links),
                         ("detect", record.message, record.item_links))
        self.assertEqual(self.panel.title.get(), "Reply · detecting language…")
        self.assertEqual(self.panel.language.get(), DETECTING_LANGUAGE)
        self.assertEqual(self.panel.draft.get(), "Do you still need a healer?")
        self.panel.input.focus_set.assert_called_once()
        self.panel.input.selection_range.assert_called_once_with(0, "end")

    def test_party_language_detection_retains_source_metadata_without_constraining_reply(self):
        record = replace(self.russian, message="Ищу хилера [12~19] (8/9); [Shining Knife]",
                         item_links=("[Shining Knife]",), party_details=("[12~19]", "(8/9)"))
        self.panel.select(record)
        detection = take_job(self.panel)
        self.assertEqual(detection.item_links, record.item_links)
        self.assertEqual(detection.party_details, record.party_details)
        source_client = Mock()
        source_client.detect_language.return_value = "Russian"
        self.panel._execute_job(detection, {"English": source_client})
        self.panel._drain_events()
        source_client.detect_language.assert_called_once_with(
            record.message, item_links=record.item_links, party_details=record.party_details)
        self.panel.root.clipboard_append.assert_not_called()

        draft = "Can our 2 healers join? We are levels 15 and 16."
        self.panel.draft.set(draft)
        self.panel._draft_changed()
        self.panel.translate()
        reply = take_job(self.panel)
        self.assertEqual((reply.item_links, reply.party_details), ((), ()))
        reply_client = Mock()
        reply_client.translate.return_value = "Могут наши 2 хилера присоединиться?"
        self.panel._execute_job(reply, {"Russian": reply_client})
        reply_client.translate.assert_called_once_with(draft)

    def test_plain_source_detection_does_not_add_empty_link_options(self):
        self.panel.select(self.russian)
        detection = take_job(self.panel)
        client = Mock()
        self.panel._execute_job(detection, {"English": client})
        client.detect_language.assert_called_once_with(self.russian.message)

    def test_known_overlay_language_is_selected_immediately_without_detection(self):
        self.panel.select(replace(self.russian, source_language="Russian"))
        self.assertEqual(self.panel.title.get(), "Reply in Russian")
        self.assertEqual(self.panel.language.get(), "Russian")
        self.assertTrue(self.panel._jobs.empty())
        self.panel.input.focus_set.assert_called_once()
        self.panel.draft.set("Can I join?")
        self.panel._draft_changed()
        self.panel.translate()
        job = take_job(self.panel)
        self.assertEqual((job.kind, job.target), ("translate", "Russian"))
        deliver(self.panel, job, "Можно к вам?")
        self.panel.root.clipboard_append.assert_called_once_with("Можно к вам?")

    def test_known_ambiguous_language_asks_for_choice_without_repeating_detection(self):
        self.panel.select(replace(self.russian, message="WTS MOIRAI HEAVY", source_language="Unknown"))
        self.assertEqual(self.panel.language.get(), CHOOSE_LANGUAGE)
        self.assertEqual(self.panel.title.get(), "Reply · choose language")
        self.assertTrue(self.panel._jobs.empty())
        self.panel.input.focus_set.assert_called_once()

    def test_universal_trade_keeps_reply_focus_and_requires_manual_language(self):
        for whisper in (False, True):
            with self.subTest(whisper=whisper):
                self.panel = composer()
                record = replace(self.russian, sender="Trader", message="WTS [Club] 10kk",
                                 source_language="Universal trade")
                self.panel.select(record, whisper=whisper)
                self.assertEqual(self.panel.language.get(), CHOOSE_LANGUAGE)
                self.assertIn("choose language", self.panel.title.get())
                self.assertIn("Universal trade", self.panel.status.get())
                self.assertTrue(self.panel._jobs.empty())
                self.panel.input.focus_set.assert_called_once()
                self.assertEqual(self.panel._recipient, "Trader" if whisper else "")

    def test_whisper_name_is_exact_and_does_not_rewrite_unsafe_names(self):
        for source, expected in (("ExamplePlayer", "ExamplePlayer"), (" Player_2 ", "Player_2"),
                                 ("Игрок猫", "Игрок猫"), ("A" * 120, "A" * 120)):
            with self.subTest(source=source):
                self.assertEqual(whisper_recipient(source), expected)
        for source in ("", "   ", None, 'Name"', "O'Name", "Two Names", "A\tB",
                       "Name\n", "\rName", "A\u200bB", "A\x00B", "“Name”"):
            with self.subTest(source=source):
                self.assertEqual(whisper_recipient(source), "")

    def test_whisper_selects_language_focuses_and_prefixes_only_after_translation(self):
        record = replace(self.spanish, sender="ExamplePlayer", source_language="Spanish")
        self.panel.select(record, whisper=True)
        self.assertEqual(self.panel.language.get(), "Spanish")
        self.assertEqual(self.panel.title.get(), "Reply to ExamplePlayer in Spanish")
        self.panel.input.focus_set.assert_called_once()
        self.panel.input.selection_range.assert_called_once_with(0, "end")
        self.panel.draft.set("Can I join you?")
        self.panel._draft_changed()
        self.panel.translate()
        job = take_job(self.panel)
        self.assertEqual((job.text, job.recipient, job.target), ("Can I join you?", "ExamplePlayer", "Spanish"))
        client = Mock()
        client.translate.return_value = "¿Puedo\nacompañarte?"
        self.panel._execute_job(job, {"Spanish": client})
        client.translate.assert_called_once_with("Can I join you?")
        self.panel._drain_events()
        expected = '"ExamplePlayer ¿Puedo acompañarte?'
        self.assertEqual(self.panel.output.get(), expected)
        self.panel.root.clipboard_append.assert_called_once_with(expected)
        self.assertIn("Copied whisper to ExamplePlayer in Spanish", self.panel.status.get())
        self.panel.copy()
        self.assertEqual(self.panel.root.clipboard_append.call_count, 2)
        self.panel.root.clipboard_append.assert_called_with(expected)

    def test_whisper_waits_for_language_with_one_enter_and_keeps_recipient(self):
        self.panel.select(replace(self.spanish, sender="ExamplePlayer"), whisper=True)
        detection = take_job(self.panel)
        self.assertEqual(detection.recipient, "ExamplePlayer")
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        deliver(self.panel, detection, "Spanish")
        self.assertEqual(self.panel.title.get(), "Reply to ExamplePlayer in Spanish")
        job = take_job(self.panel)
        self.assertEqual(job.recipient, "ExamplePlayer")
        deliver(self.panel, job, "Hola")
        self.panel.root.clipboard_append.assert_called_once_with('"ExamplePlayer Hola')

    def test_switching_whisper_recipient_discards_old_result_and_copy(self):
        record = replace(self.spanish, sender="ExamplePlayer", source_language="Spanish")
        self.panel.select(record, whisper=True)
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        old = take_job(self.panel)
        self.panel.select(replace(record, sender="OtherPlayer"), whisper=True)
        deliver(self.panel, old, "Hola ExamplePlayer")
        self.panel.root.clipboard_append.assert_not_called()
        self.assertEqual(self.panel.output.get(), "")
        self.panel.translate()
        current = take_job(self.panel)
        deliver(self.panel, current, "Hola")
        self.panel.root.clipboard_append.assert_called_once_with('"OtherPlayer Hola')

    def test_language_link_selection_clears_whisper_recipient(self):
        record = replace(self.spanish, sender="ExamplePlayer", source_language="Spanish")
        self.panel.select(record, whisper=True)
        self.panel.select(record)
        self.assertEqual(self.panel._recipient, "")
        self.assertEqual(self.panel.title.get(), "Reply in Spanish")
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        job = take_job(self.panel)
        self.assertEqual(job.recipient, "")
        deliver(self.panel, job, "Hola")
        self.panel.root.clipboard_append.assert_called_once_with("Hola")

    def test_language_override_keeps_whisper_recipient_and_discards_old_detection(self):
        self.panel.select(replace(self.spanish, sender="ExamplePlayer"), whisper=True)
        detection = take_job(self.panel)
        self.panel.language.set("Portuguese")
        self.panel._language_changed()
        deliver(self.panel, detection, "Spanish")
        self.assertEqual(self.panel.title.get(), "Reply to ExamplePlayer in Portuguese")
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        job = take_job(self.panel)
        self.assertEqual((job.target, job.recipient), ("Portuguese", "ExamplePlayer"))
        deliver(self.panel, job, "Olá")
        self.panel.root.clipboard_append.assert_called_once_with('"ExamplePlayer Olá')

    def test_reconnect_keeps_selected_whisper_while_redetecting_language(self):
        self.panel.set_connected(False)
        self.panel.select(replace(self.spanish, sender="ExamplePlayer"), whisper=True)
        self.panel.set_connected(True)
        detection = take_job(self.panel)
        self.assertEqual(detection.recipient, "ExamplePlayer")
        self.assertEqual(self.panel.title.get(), "Reply to ExamplePlayer · detecting language…")
        deliver(self.panel, detection, "Spanish")
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        deliver(self.panel, take_job(self.panel), "Hola")
        self.panel.root.clipboard_append.assert_called_once_with('"ExamplePlayer Hola')

    def test_clear_selection_resets_whisper_before_manual_reply(self):
        self.panel.select(replace(self.spanish, sender="ExamplePlayer", source_language="Spanish"), whisper=True)
        self.panel.clear_selection()
        self.assertEqual(self.panel._recipient, "")
        self.panel.language.set("Spanish")
        self.panel._language_changed()
        self.assertEqual(self.panel.title.get(), "Reply in Spanish")
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        deliver(self.panel, take_job(self.panel), "Hola")
        self.panel.root.clipboard_append.assert_called_once_with("Hola")

    def test_invalid_whisper_selection_clears_previous_target_and_result(self):
        self.panel.select(replace(self.spanish, sender="ExamplePlayer", source_language="Spanish"), whisper=True)
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        old = take_job(self.panel)
        self.panel.select(replace(self.spanish, sender='Bad"Name'), whisper=True)
        deliver(self.panel, old, "Hola")
        self.assertEqual(self.panel._recipient, "")
        self.assertEqual(self.panel.language.get(), CHOOSE_LANGUAGE)
        self.assertEqual(self.panel.output.get(), "")
        self.assertIsNone(self.panel._record)
        self.assertIn("no valid player name", self.panel.status.get())
        self.panel.root.clipboard_append.assert_not_called()

    def test_recipient_snapshot_also_guards_embedding_changes_without_selection_event(self):
        self.panel.select(replace(self.spanish, sender="ExamplePlayer", source_language="Spanish"), whisper=True)
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        job = take_job(self.panel)
        self.panel._recipient = "OtherPlayer"
        deliver(self.panel, job, "Hola")
        self.assertEqual(self.panel.output.get(), "")
        self.panel.root.clipboard_append.assert_not_called()

    def test_new_selection_replaces_pending_detection_and_rejects_old_result(self):
        self.panel.select(self.russian)
        old = take_job(self.panel)
        self.panel.select(self.spanish)
        current = take_job(self.panel)
        deliver(self.panel, old, "Russian")
        self.assertEqual(self.panel.language.get(), DETECTING_LANGUAGE)
        deliver(self.panel, current, "Spanish")
        self.assertEqual(self.panel.language.get(), "Spanish")

    def test_repeated_selection_leaves_only_latest_queued_job(self):
        for record in (self.russian, self.spanish, self.russian, self.spanish):
            self.panel.select(record)
        self.assertEqual(self.panel._jobs.qsize(), 1)
        self.assertEqual(self.panel._jobs.unfinished_tasks, 1)
        self.assertEqual(take_job(self.panel).text, self.spanish.message)
        self.assertEqual(self.panel._jobs.unfinished_tasks, 0)

    def test_draft_edit_during_detection_still_accepts_language(self):
        self.panel.select(self.russian)
        job = take_job(self.panel)
        self.panel.draft.set("Hello!")
        self.panel._draft_changed()
        deliver(self.panel, job, "Russian")
        self.assertEqual(self.panel.language.get(), "Russian")
        self.panel.translate_button.configure.assert_called_with(state="normal")

    def test_manual_language_override_cannot_be_overwritten_by_detection(self):
        self.panel.select(self.russian)
        old = take_job(self.panel)
        self.panel.language.set("Ukrainian")
        self.panel._language_changed()
        deliver(self.panel, old, "Russian")
        self.assertEqual(self.panel.language.get(), "Ukrainian")

    def test_unknown_or_failed_detection_requires_manual_language(self):
        for value, error in ((None, None), (None, "Local server unavailable")):
            with self.subTest(error=error):
                self.panel.select(self.russian)
                job = take_job(self.panel)
                deliver(self.panel, job, value, error)
                self.assertEqual(self.panel.language.get(), CHOOSE_LANGUAGE)
                self.assertIn("manually", self.panel.status.get())
                self.panel.translate_button.configure.assert_called_with(state="disabled")

    def test_enter_translates_to_selected_language_and_copies_automatically(self):
        job = self.ready_reply()
        self.assertEqual(job.target, "Russian")
        self.assertEqual(job.text, self.panel.draft.get())
        deliver(self.panel, job, "Я могу лечить.\nСколько человек в группе?")
        self.assertEqual(self.panel.output.get(), "Я могу лечить. Сколько человек в группе?")
        self.panel.copy_button.configure.assert_called_with(state="normal")
        self.panel.root.clipboard_clear.assert_called_once()
        self.panel.root.clipboard_append.assert_called_once_with(self.panel.output.get())
        self.assertIn("Copied in Russian", self.panel.status.get())
        self.panel.copy()
        self.assertEqual(self.panel.root.clipboard_append.call_count, 2)

    def test_enter_during_detection_continues_without_a_second_enter(self):
        self.panel.select(self.russian)
        detection = take_job(self.panel)
        self.panel.draft.set("Hello, can I join?")
        self.panel._draft_changed()
        self.panel.translate()
        self.panel.translate()  # Repeated Enter still produces one request.
        self.assertTrue(self.panel._jobs.empty())
        self.panel.root.clipboard_append.assert_not_called()
        deliver(self.panel, detection, "Russian")
        self.assertEqual(self.panel._jobs.qsize(), 1)
        translation = take_job(self.panel)
        self.assertEqual((translation.kind, translation.target, translation.text),
                         ("translate", "Russian", "Hello, can I join?"))
        deliver(self.panel, translation, "Привет, можно к вам?")
        self.panel.root.clipboard_append.assert_called_once_with("Привет, можно к вам?")

    def test_editing_after_early_enter_cancels_the_submitted_draft(self):
        self.panel.select(self.russian)
        detection = take_job(self.panel)
        self.panel.draft.set("Old draft")
        self.panel._draft_changed()
        self.panel.translate()
        self.panel.draft.set("New draft")
        self.panel._draft_changed()
        deliver(self.panel, detection, "Russian")
        self.assertTrue(self.panel._jobs.empty())
        self.panel.root.clipboard_append.assert_not_called()
        self.panel.translate()
        self.assertEqual(take_job(self.panel).text, "New draft")

    def test_unknown_or_failed_language_does_not_submit_or_copy_waiting_reply(self):
        for error in (None, "Offline"):
            with self.subTest(error=error):
                self.panel.select(self.russian)
                detection = take_job(self.panel)
                self.panel.draft.set("Hello")
                self.panel._draft_changed()
                self.panel.translate()
                deliver(self.panel, detection, None, error)
                self.assertIsNone(self.panel._waiting_reply)
                self.assertTrue(self.panel._jobs.empty())
                self.panel.root.clipboard_append.assert_not_called()

    def test_manual_language_change_cancels_early_enter_before_old_detection_returns(self):
        self.panel.select(self.russian)
        detection = take_job(self.panel)
        self.panel.draft.set("Hello")
        self.panel._draft_changed()
        self.panel.translate()
        self.panel.language.set("Spanish")
        self.panel._language_changed()
        deliver(self.panel, detection, "Russian")
        self.assertTrue(self.panel._jobs.empty())
        self.panel.root.clipboard_append.assert_not_called()
        self.assertEqual(self.panel.language.get(), "Spanish")

    def test_clipboard_failure_keeps_translation_and_allows_copy_retry(self):
        job = self.ready_reply()
        self.panel.root.clipboard_append.side_effect = [tk.TclError("Clipboard busy"), None]
        deliver(self.panel, job, "Привет")
        self.assertEqual(self.panel.output.get(), "Привет")
        self.assertIn("Could not copy", self.panel.status.get())
        self.panel.copy_button.configure.assert_called_with(state="normal")
        self.panel.copy()
        self.assertIn("Copied in Russian", self.panel.status.get())
        self.assertEqual(self.panel.root.clipboard_append.call_count, 2)

    def test_invisible_editing_controls_from_model_are_never_copied(self):
        for control in ("\x00", "\x08", "\x1b", "\x7f"):
            with self.subTest(control=repr(control)):
                self.panel = composer()
                job = self.ready_reply()
                deliver(self.panel, job, "Привет" + control + "bad")
                self.assertEqual(self.panel.output.get(), "")
                self.assertIn("unsupported control characters", self.panel.status.get())
                self.panel.root.clipboard_clear.assert_not_called()
                self.panel.root.clipboard_append.assert_not_called()

    def test_draft_edit_discards_inflight_translation_and_previous_copy(self):
        job = self.ready_reply()
        self.panel.draft.set("I changed my mind.")
        self.panel._draft_changed()
        deliver(self.panel, job, "OLD RESULT")
        self.assertEqual(self.panel.output.get(), "")
        self.panel.copy()
        self.panel.root.clipboard_append.assert_not_called()
        self.panel.translate_button.configure.assert_called_with(state="normal")

    def test_language_change_discards_inflight_translation(self):
        job = self.ready_reply()
        self.panel.language.set("Spanish")
        self.panel._language_changed()
        deliver(self.panel, job, "OLD RUSSIAN RESULT")
        self.assertEqual(self.panel.output.get(), "")
        self.assertEqual(self.panel.language.get(), "Spanish")
        self.panel.root.clipboard_append.assert_not_called()

    def test_selection_change_discards_inflight_translation(self):
        job = self.ready_reply()
        draft = self.panel.draft.get()
        self.panel.select(self.spanish)
        deliver(self.panel, job, "OLD RESULT")
        self.assertEqual(self.panel.output.get(), "")
        self.assertEqual(self.panel.draft.get(), draft)
        self.assertEqual(self.panel.title.get(), "Reply · detecting language…")
        self.panel.root.clipboard_append.assert_not_called()

    def test_reply_error_clears_output_and_allows_retry(self):
        job = self.ready_reply()
        deliver(self.panel, job, error="Local request timed out")
        self.assertEqual(self.panel.output.get(), "")
        self.assertIn("timed out", self.panel.status.get())
        self.panel.translate_button.configure.assert_called_with(state="normal")
        self.panel.copy_button.configure.assert_called_with(state="disabled")
        self.panel.root.clipboard_append.assert_not_called()

    def test_clear_invalidates_requests_and_preserves_draft(self):
        job = self.ready_reply()
        draft = self.panel.draft.get()
        self.panel.clear_selection()
        deliver(self.panel, job, "OLD RESULT")
        self.assertEqual(self.panel.draft.get(), draft)
        self.assertEqual(self.panel.output.get(), "")
        self.assertEqual(self.panel.language.get(), CHOOSE_LANGUAGE)
        self.assertIsNone(self.panel._record)

    def test_offline_selection_queues_nothing_then_reconnect_detects(self):
        self.panel.set_connected(False)
        self.panel.select(self.russian)
        self.assertTrue(self.panel._jobs.empty())
        self.panel.set_connected(True)
        self.assertEqual(take_job(self.panel).text, self.russian.message)

    def test_worker_uses_snapshot_and_skips_stale_jobs(self):
        job = self.ready_reply(text="Hello.", language="Spanish")
        client = Mock()
        client.translate.return_value = "Hola."
        # Reading any Tk variable inside the background method would fail.
        with patch.object(self.panel.draft, "get", side_effect=AssertionError("Worker read Tk")), \
             patch.object(self.panel.language, "get", side_effect=AssertionError("Worker read Tk")), \
             patch("reply_composer.OllamaClient", return_value=client) as factory:
            self.panel._execute_job(job, {})
        factory.assert_called_once_with("http://127.0.0.1:11435", "qwen2.5:3b", "Spanish")
        client.translate.assert_called_once_with("Hello.")
        self.panel._drain_events()
        self.assertEqual(self.panel.output.get(), "Hola.")
        self.panel.clear_selection()
        client.reset_mock()
        self.panel._execute_job(job, {"Spanish": client})
        client.translate.assert_not_called()

    def test_close_cancels_polling_and_suppresses_late_ui_work(self):
        job = self.ready_reply()
        self.panel._timer = "pending"
        self.panel.root.reset_mock()
        self.panel.close()
        self.assertTrue(self.panel._stop.is_set())
        self.panel.root.after_cancel.assert_called_once_with("pending")
        deliver(self.panel, job, "LATE RESULT")
        self.assertEqual(self.panel.output.get(), "")
        self.panel.root.after.assert_not_called()
        self.panel.copy()
        self.panel.root.clipboard_append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
