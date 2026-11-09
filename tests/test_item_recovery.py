"""Recover failed protected-item translations without trusting the bad output."""
import io
import json
import unittest
from unittest.mock import Mock

from translator_core import OllamaClient, OllamaError


class ItemRecoveryTests(unittest.TestCase):
    def client(self, parts=None, *, fallback_content=None, fallback_fields=None,
               first_content="The model omitted an item."):
        client = OllamaClient()
        client._opener = Mock()
        if fallback_content is None:
            fallback_content = json.dumps(parts, ensure_ascii=False)
        responses = [
            {},
            {"message": {"content": first_content}},
            {"message": {"content": fallback_content}, **(fallback_fields or {})},
        ]
        client._opener.open.side_effect = [
            io.BytesIO(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            for response in responses
        ]
        return client

    def fallback_payload(self, client):
        self.assertEqual(client._opener.open.call_count, 3)
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        return json.loads(request.data)

    def test_reported_club_listing_recovers_without_losing_the_trade_prefix(self):
        client = self.client({"part0": "TI"}, first_content="TI")
        self.assertEqual(client.translate("WTS [Club] TI", item_links=("[Club]",)),
                         "WTS [Club] TI")
        payload = self.fallback_payload(client)
        self.assertEqual(json.loads(payload["messages"][-1]["content"]), {"part0": "TI"})
        self.assertEqual(set(payload["format"]["properties"]), {"part0"})
        self.assertEqual(set(payload["format"]["required"]), {"part0"})

    def test_prices_and_source_punctuation_stay_outside_the_recovery_model(self):
        client = self.client({"part0": "Selling", "part1": "for"})
        self.assertEqual(client.translate("Продам [Club] за 10kk.", item_links=("[Club]",)),
                         "Selling [Club] for 10kk.")
        payload = self.fallback_payload(client)
        self.assertEqual(json.loads(payload["messages"][-1]["content"]),
                         {"part0": "Продам", "part1": "за"})
        self.assertEqual(set(payload["format"]["properties"]), {"part0", "part1"})
        self.assertEqual(set(payload["format"]["required"]), {"part0", "part1"})

    def test_repeated_items_are_restored_in_original_order(self):
        client = self.client({"part0": "Trade", "part1": "for", "part2": "or", "part3": "for"},
                             first_content="Sell [[L2ITEM2]] and [[L2ITEM2]].")
        original = "Обмен [Club] на [Shield], или [Club] за 10kk."
        self.assertEqual(client.translate(original, item_links=("[Club]", "[Shield]")),
                         "Trade [Club] for [Shield], or [Club] for 10kk.")
        self.fallback_payload(client)

    def test_urls_and_inline_acronyms_are_reconstructed_with_items(self):
        client = self.client({"part0": "Send", "part1": "see", "part2": "and", "part3": "for"})
        original = "Напиши PM, смотри https://example.com/WTB и [Club] за 5kk."
        self.assertEqual(client.translate(original, item_links=("[Club]",)),
                         "Send PM, see https://example.com/WTB and [Club] for 5kk.")
        payload = self.fallback_payload(client)
        self.assertEqual(json.loads(payload["messages"][-1]["content"]),
                         {"part0": "Напиши", "part1": "смотри", "part2": "и", "part3": "за"})

    def test_original_whitespace_survives_fragment_reconstruction(self):
        client = self.client({"part0": "Sell", "part1": "for"})
        self.assertEqual(client.translate("Продам  [Club]\tза 10kk!", item_links=("[Club]",)),
                         "Sell  [Club]\tfor 10kk!")
        self.fallback_payload(client)

    def test_invalid_fragment_shapes_fail_without_another_retry(self):
        invalid = [
            {"part0": "Selling"},
            {"part0": "Selling", "part1": "for", "extra": "ignored?"},
            {"part0": None, "part1": "for"},
            {"part0": ["Selling"], "part1": "for"},
            {"part0": 123, "part1": "for"},
            {"part0": True, "part1": "for"},
            {"part0": "", "part1": "for"},
            {"part0": "   ", "part1": "for"},
            ["Selling", "for"],
            "Selling for",
        ]
        for answer in invalid:
            with self.subTest(answer=answer):
                client = self.client(answer)
                with self.assertRaises(OllamaError):
                    client.translate("Продам [Club] за 10kk.", item_links=("[Club]",))
                self.fallback_payload(client)

    def test_unreadable_json_fails_without_another_retry(self):
        for answer in ("not JSON", '```json\n{"part0":"Selling","part1":"for"}\n```', "{",
                       '{"part0":"Selling","part0":"Buying","part1":"for"}'):
            with self.subTest(answer=answer):
                client = self.client(fallback_content=answer)
                with self.assertRaises(OllamaError):
                    client.translate("Продам [Club] за 10kk.", item_links=("[Club]",))
                self.fallback_payload(client)

    def test_truncated_recovery_is_rejected_even_when_json_is_complete(self):
        client = self.client({"part0": "Selling", "part1": "for"},
                             fallback_fields={"done_reason": "length"})
        with self.assertRaises(OllamaError):
            client.translate("Продам [Club] за 10kk.", item_links=("[Club]",))
        self.fallback_payload(client)

    def test_reasoning_inside_a_fragment_is_rejected(self):
        for content in ("<think>consider</think>Selling", "Selling</THINK>"):
            with self.subTest(content=content):
                client = self.client({"part0": content, "part1": "for"})
                with self.assertRaises(OllamaError):
                    client.translate("Продам [Club] за 10kk.", item_links=("[Club]",))
                self.fallback_payload(client)

    def test_json_escaped_reasoning_is_rejected_after_decoding(self):
        client = self.client(fallback_content=r'{"part0":"\u003cthink\u003eReasoning","part1":"for"}')
        with self.assertRaises(OllamaError):
            client.translate("Продам [Club] за 10kk.", item_links=("[Club]",))
        self.fallback_payload(client)

    def test_other_targets_do_not_use_unvalidated_fragment_replies(self):
        client = self.client({"part0": "hold"})
        client.target_language = "Russian"
        with self.assertRaisesRegex(OllamaError, "changed a linked item"):
            client.translate("Can you hold [Club]?", item_links=("[Club]",))
        self.assertEqual(client._opener.open.call_count, 2)

    def test_generated_active_markers_are_not_allowed_in_fragments(self):
        for content in ("Selling [[L2ITEM0]]", "Selling L2ITEM9", "Selling [[l2item0]]"):
            with self.subTest(content=content):
                client = self.client({"part0": content, "part1": "for"})
                with self.assertRaises(OllamaError):
                    client.translate("Продам [Club] за 10kk.", item_links=("[Club]",))
                self.fallback_payload(client)

    def test_literal_marker_like_source_text_is_allowed_when_the_active_prefix_differs(self):
        client = self.client({"part0": "Keep L2ITEM0"})
        self.assertEqual(client.translate("Сохрани L2ITEM0 [Club]", item_links=("[Club]",)),
                         "Keep L2ITEM0 [Club]")
        self.fallback_payload(client)

    def test_valid_whole_sentence_response_does_not_need_recovery(self):
        client = self.client(first_content="Selling [[L2ITEM0]] for 10kk.")
        self.assertEqual(client.translate("Продам [Club] за 10kk.", item_links=("[Club]",)),
                         "Selling [Club] for 10kk.")
        self.assertEqual(client._opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
