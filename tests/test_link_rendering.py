"""Production message rendering with synthetic spans; no Tk windows or models."""
from collections import deque
from dataclasses import replace
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from chat_links import ChatLinkSpan, LINK_COLORS, LinkedText
from chat_overlay import (ChatOverlay, DEFAULT_OVERLAY, MAX_CHAT_CHARACTERS,
                          OverlayHistory, make_overlay_message, message_parts)
import translator_app
from translator_core import ChatRecord


def span(text, label, kind, *, last=False):
    start = text.rindex(label) if last else text.index(label)
    return ChatLinkSpan(kind, start, start + len(label))


def widget_runs(widget):
    runs = []
    for call in widget.insert.call_args_list:
        tags = call.args[2] if len(call.args) > 2 else ()
        runs.append((str(call.args[1]), (tags,) if isinstance(tags, str) else tuple(tags)))
    return runs


def highlighted(widget, tag):
    return [text for text, tags in widget_runs(widget) if tag in tags]


def rendered(widget):
    return "".join(text for text, _tags in widget_runs(widget))


def headless_overlay(*, show_original=False):
    overlay = ChatOverlay.__new__(ChatOverlay)
    overlay.settings = dict(DEFAULT_OVERLAY, show_original=show_original)
    overlay.on_reply = Mock()
    overlay._reply_records = {}
    overlay._marks = deque()
    overlay._sequence = 0
    overlay.history = OverlayHistory()
    overlay.text = Mock()
    overlay.text.yview.return_value = (0, 1)
    overlay._latest = Mock()
    overlay._schedule_layout = Mock()
    return overlay


def headless_launcher(*, show_original=False):
    app = translator_app.TranslatorApp.__new__(translator_app.TranslatorApp)
    app.transcript = Mock()
    app.transcript.yview.return_value = (0, 1)
    app.show_original = Mock()
    app.show_original.get.return_value = show_original
    app._reply_records = {}
    app._rows = deque()
    return app


class LinkRenderingTests(unittest.TestCase):
    def mixed_message(self):
        original_party = "Ищем хилера [12~19] (8/9)"
        translated_party = "Looking for a healer [12~19] (8/9)"
        original = "WTS [Club] [notice] / " + original_party + " / [Club]"
        translated = "Sell [Club] [notice] / " + translated_party + " / [Club]"
        record = ChatRecord("12:34:56", "Synthetic", original, 1,
                            item_links=("[Club]",), source_language="Russian",
                            party_details=("[12~19]", "(8/9)"),
                            link_spans=(span(original, "[Club]", "item"),
                                        span(original, original_party, "party")))
        result = LinkedText(translated, (span(translated, "[Club]", "item"),
                                        span(translated, translated_party, "party")))
        return record, result, original_party, translated_party

    def test_both_renderers_color_only_actual_links_and_keep_channel_sender_and_reply(self):
        record, result, _source_party, translated_party = self.mixed_message()
        overlay = headless_overlay()
        message = make_overlay_message(record, result)
        self.assertIs(message_parts(message, overlay.settings)[3], message.translated)
        overlay._write_message(message)
        app = headless_launcher()
        app._write_row("row", (record, result, "English", "Live", .1, False))

        for widget in (overlay.text, app.transcript):
            with self.subTest(widget=widget):
                self.assertEqual(highlighted(widget, "item_link"), ["[Club]"])
                self.assertEqual(highlighted(widget, "party_link"), [translated_party])
                self.assertIn("[notice]", rendered(widget))
                self.assertIn("/ [Club]", rendered(widget))
                for _text, tags in widget_runs(widget):
                    if "item_link" in tags or "party_link" in tags:
                        self.assertIn("channel1", tags)
                        self.assertTrue(set(tags).isdisjoint({"reply", "sender", "whisper"}))
                sender = next(tags for _text, tags in widget_runs(widget) if "sender" in tags)
                self.assertIn("channel1", sender)
                self.assertTrue(set(sender).isdisjoint(LINK_COLORS))
                widget.tag_bind.assert_not_called()
        self.assertIs(next(iter(overlay._reply_records.values())), record)
        self.assertIs(app._reply_records["row"], record)

    def test_originals_use_source_spans_and_redraw_keeps_each_translation_span(self):
        record, result, original_party, translated_party = self.mixed_message()
        overlay = headless_overlay(show_original=True)
        overlay.history.append(make_overlay_message(record, result))
        app = headless_launcher(show_original=True)
        app._rows.append(("row", (record, result, "English", "Live", .1, False)))
        for _redraw in range(2):
            overlay.text.insert.reset_mock()
            app.transcript.insert.reset_mock()
            overlay._render_all()
            app._render_rows()
            for widget in (overlay.text, app.transcript):
                self.assertEqual(highlighted(widget, "party_link"), [translated_party, original_party])
                self.assertEqual(highlighted(widget, "item_link"), ["[Club]", "[Club]"])
                original_runs = [(text, tags) for text, tags in widget_runs(widget)
                                 if "original" in tags and "party_link" in tags]
                self.assertEqual([text for text, _tags in original_runs], [original_party])
                self.assertIn("\n  " + record.message, rendered(widget))
            self.assertIs(app._reply_records["row"], record)
            self.assertIs(next(iter(overlay._reply_records.values())), record)

    def test_failure_explanation_never_inherits_links_but_source_body_retains_them(self):
        record, _result, original_party, _translated_party = self.mixed_message()
        error = LinkedText("Unavailable [Club]", (ChatLinkSpan("item", 12, 18),))
        overlay = headless_overlay(show_original=True)
        message = make_overlay_message(record, error, error=True)
        self.assertEqual(message.translated.link_spans, ())
        overlay._write_message(message)
        app = headless_launcher(show_original=True)
        app._write_row("row", (record, error, "English", "Live", 0, True))
        for widget in (overlay.text, app.transcript):
            self.assertEqual(highlighted(widget, "party_link"), [original_party])
            self.assertEqual(highlighted(widget, "item_link"), ["[Club]"])
            error_runs = [(text, tags) for text, tags in widget_runs(widget) if "error" in tags]
            self.assertIn("Unavailable [Club]", "".join(text for text, _tags in error_runs))
            self.assertTrue(all(set(tags).isdisjoint(LINK_COLORS) for _text, tags in error_runs))
        self.assertTrue(all("original" in tags for _text, tags in widget_runs(overlay.text)
                            if "item_link" in tags or "party_link" in tags))

    def test_whitespace_normalization_moves_spans_and_overlay_truncation_clips_them(self):
        source = " \tWTS\n [Club] \t /\r\n party title  "
        spans = (span(source, "[Club]", "item"), span(source, "party title", "party"))
        record = ChatRecord("12:34:56", "Synthetic", source, 1, link_spans=spans)
        result = LinkedText(source, spans)
        overlay = headless_overlay()
        overlay._write_message(make_overlay_message(record, result))
        app = headless_launcher()
        app._write_row("row", (record, result, "English", "Live", .1, False))
        for widget in (overlay.text, app.transcript):
            self.assertIn("WTS [Club] / party title", rendered(widget))
            self.assertEqual(highlighted(widget, "item_link"), ["[Club]"])
            self.assertEqual(highlighted(widget, "party_link"), ["party title"])

        long_text = "x" * (MAX_CHAT_CHARACTERS - 3) + "[Club]"
        long_result = LinkedText(long_text, (span(long_text, "[Club]", "item"),))
        message = make_overlay_message(record, long_result)
        overlay.text.insert.reset_mock()
        overlay._write_message(message)
        self.assertEqual(len(message.translated), MAX_CHAT_CHARACTERS)
        self.assertEqual(highlighted(overlay.text, "item_link"), ["[Cl"])

    def test_plain_brackets_and_party_channel_do_not_invent_link_styles(self):
        record = ChatRecord("12:34:56", "Synthetic", "LFG [12~19] (8/9) [Club]", 3)
        overlay = headless_overlay()
        overlay._write_message(make_overlay_message(record, record.message))
        app = headless_launcher()
        app._write_row("row", (record, record.message, "English", "Live", .1, False))
        for widget in (overlay.text, app.transcript):
            self.assertEqual(highlighted(widget, "item_link"), [])
            self.assertEqual(highlighted(widget, "party_link"), [])
            self.assertIn((record.message, ("channel3",)), widget_runs(widget))

    def test_link_foregrounds_follow_channel_and_original_tags_without_adding_actions(self):
        overlay = headless_overlay()
        overlay.window = Mock()
        overlay._native = None
        overlay._update_caption = Mock()
        overlay._render_all = Mock()
        overlay._apply_appearance()
        configurations = overlay.text.tag_configure.call_args_list
        last_base = max(index for index, call in enumerate(configurations)
                        if call.args[0].startswith("channel") or call.args[0] == "original")
        for tag, color in LINK_COLORS.items():
            index, configured = next((index, call) for index, call in enumerate(configurations)
                                     if call.args[0] == tag)
            self.assertGreater(index, last_base)
            self.assertEqual(configured.kwargs, {"foreground": color})
        overlay.text.tag_bind.assert_not_called()


class LinkedWorkerTests(unittest.TestCase):
    def test_cache_separates_occurrence_provenance_and_reuses_only_matching_rich_results(self):
        text = "[Club] [Club]"
        first = ChatRecord("12:34:56", "First", text, 1, item_links=("[Club]",),
                           link_spans=(span(text, "[Club]", "item"),))
        second = replace(first, sender="Second", link_spans=(span(text, "[Club]", "item", last=True),))
        repeated = replace(first, sender="Repeated")
        plain = replace(first, sender="Plain", item_links=(), link_spans=())
        app = headless_launcher()
        app.config = dict(translator_app.DEFAULTS)
        app.jobs = queue.Queue()
        app.events = queue.Queue()
        app.stop = threading.Event()
        for record in (first, second, repeated, plain):
            app.jobs.put((record, "English", "Live", 0))
        get_job = app.jobs.get

        def next_job(timeout):
            if app.jobs.empty():
                app.stop.set()
                raise queue.Empty
            return get_job(timeout=timeout)

        client = Mock()
        client.translate_record.side_effect = lambda record: LinkedText(record.message, record.link_spans)
        client.translate.return_value = text
        client.detect_language.return_value = "English"
        with patch.object(app.jobs, "get", side_effect=next_job), \
             patch.object(translator_app, "OllamaClient", return_value=client):
            app._worker()

        self.assertEqual([call.args[0] for call in client.translate_record.call_args_list], [first, second])
        client.translate.assert_called_once_with(text)
        self.assertEqual(client.detect_language.call_count, 3)
        self.assertEqual([call.kwargs for call in client.detect_language.call_args_list],
                         [{"item_links": ("[Club]",)}, {"item_links": ("[Club]",)}, {}])
        delivered = [data for kind, data in app.events.queue if kind == "result"]
        self.assertEqual(len(delivered), 4)
        for index, (record, translated, target, source, seconds, error, generation) in enumerate(delivered):
            self.assertFalse(error)
            self.assertEqual(record.link_spans, (first, second, repeated, plain)[index].link_spans)
            self.assertEqual(getattr(translated, "link_spans", ()), record.link_spans)
            app.transcript.insert.reset_mock()
            app._write_row(f"row{index}", (record, translated, target, source, seconds, error))
            self.assertEqual(highlighted(app.transcript, "item_link"), ["[Club]"] if record.link_spans else [])
            body_runs = [(value, tags) for value, tags in widget_runs(app.transcript)
                         if "channel1" in tags and "sender" not in tags]
            if index == 0:
                self.assertEqual(body_runs[0], ("[Club]", ("channel1", "item_link")))
            elif index == 1:
                self.assertEqual(body_runs[-1], ("[Club]", ("channel1", "item_link")))
        self.assertIs(delivered[0][1], delivered[2][1])
        self.assertEqual(app.jobs.unfinished_tasks, 0)


if __name__ == "__main__":
    unittest.main()
