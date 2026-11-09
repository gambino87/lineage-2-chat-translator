"""Real overlay/app/composer handlers with mocked windows, model and clipboard."""
import unittest
from unittest.mock import Mock

import test_chat_overlay as overlay_helpers
import test_overlay_integration as app_helpers
import test_reply_composer as reply_helpers
from translator_core import ChatRecord


class WhisperFlowTests(unittest.TestCase):
    def test_party_name_click_detects_source_but_reply_is_a_new_message(self):
        app = app_helpers.headless_app()
        app.reply = reply_helpers.composer()
        overlay, _ = overlay_helpers.OverlayStateTests().make_overlay((0, 1))
        overlay.on_reply = app.select_reply
        record = ChatRecord("20:00:00", "PartyLeader", "Buscamos sanador [12~19] (8/9)", 25,
                            party_details=("[12~19]", "(8/9)"))
        overlay.add_message(record, "Looking for a healer [12~19] (8/9)")
        row_tag = next(iter(overlay._reply_records))
        overlay.text.tag_names.return_value = ("sender", "whisper", row_tag)
        overlay._whisper_clicked(Mock(x=5, y=6))
        detection = reply_helpers.take_job(app.reply)
        detector = Mock()
        detector.detect_language.return_value = "Spanish"
        app.reply._execute_job(detection, {"English": detector})
        app.reply._drain_events()
        detector.detect_language.assert_called_once_with(record.message, party_details=record.party_details)
        self.assertEqual(app.reply.title.get(), "Reply to PartyLeader in Spanish")

        app.reply.draft.set("Can our 2 healers join?")
        app.reply._draft_changed()
        app.reply.translate()
        reply = reply_helpers.take_job(app.reply)
        self.assertEqual((reply.item_links, reply.party_details), ((), ()))
        client = Mock()
        client.translate.return_value = "¿Pueden unirse nuestros 2 sanadores?"
        app.reply._execute_job(reply, {"Spanish": client})
        app.reply._drain_events()
        client.translate.assert_called_once_with("Can our 2 healers join?")
        app.reply.root.clipboard_append.assert_called_once_with('"PartyLeader ¿Pueden unirse nuestros 2 sanadores?')

    def test_name_click_to_translated_whisper_and_language_link_reset(self):
        app = app_helpers.headless_app()
        app.reply = reply_helpers.composer()
        overlay, _ = overlay_helpers.OverlayStateTests().make_overlay((0, 1))
        overlay.on_reply = app.select_reply
        record = ChatRecord("20:00:00", "ExamplePlayer", "Hola", 25, source_language="Spanish")
        overlay.add_message(record, "Hello")
        row_tag = next(iter(overlay._reply_records))
        overlay.text.tag_names.return_value = ("sender", "whisper", row_tag)
        overlay._whisper_clicked(Mock(x=5, y=6))
        self.assertEqual(app.reply.language.get(), "Spanish")
        self.assertEqual(app.reply.title.get(), "Reply to ExamplePlayer in Spanish")
        app.reply.input.focus_set.assert_called_once()
        app.reply.input.focus_force.assert_not_called()
        app.root.after_idle.call_args.args[0]()  # Finish the overlay click dispatch.
        app.reply.input.focus_force.assert_called_once()
        app.reply.draft.set("Hello, can I join?")
        app.reply._draft_changed()
        app.reply.translate()
        job = reply_helpers.take_job(app.reply)
        client = Mock()
        client.translate.return_value = "Hola, ¿puedo unirme?"
        app.reply._execute_job(job, {"Spanish": client})
        app.reply._drain_events()
        client.translate.assert_called_once_with("Hello, can I join?")
        expected = '"ExamplePlayer Hola, ¿puedo unirme?'
        self.assertEqual(app.reply.output.get(), expected)
        app.reply.root.clipboard_append.assert_called_once_with(expected)
        app.reply.copy()
        self.assertEqual(app.reply.root.clipboard_append.call_args.args, (expected,))
        self.assertEqual(app.reply.root.clipboard_append.call_count, 2)

        overlay.text.tag_names.return_value = ("reply", row_tag)
        overlay._reply_clicked(Mock(x=1, y=6))
        self.assertEqual(app.reply._recipient, "")
        self.assertEqual(app.reply.language.get(), "Spanish")
        self.assertEqual(app.reply.title.get(), "Reply in Spanish")
        self.assertEqual(app.reply.output.get(), "")
        app.root.after_idle.call_args.args[0]()
        self.assertEqual(app.reply.input.focus_force.call_count, 2)


if __name__ == "__main__":
    unittest.main()
