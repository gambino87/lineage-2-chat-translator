"""Synthetic pipeline checks for immutable whole-source URL protection."""
import json
import unittest
from unittest.mock import Mock

from translator_core import OllamaClient, OllamaError


class BareUrlPipelineTests(unittest.TestCase):
    def client(self, *outputs, target="English"):
        client = OllamaClient(target_language=target)
        client._verify_local_model = Mock()
        client._request = Mock(side_effect=[{"message": {"content": output}} for output in outputs])
        return client

    def test_whole_url_is_hidden_while_words_and_real_price_stay_contextual(self):
        source = "Vendo [Club]10kk, detalhes em exemplo.com/item/1"
        client = self.client("Selling [[L2ITEM0]]10kk, details at [[L2ITEM1]]")
        self.assertEqual(client.translate(source, item_links=("[Club]",)),
                         "Selling [Club]10kk, details at exemplo.com/item/1")
        self.assertEqual(client._request.call_count, 1)
        prompt = client._request.call_args.args[1]["messages"][1]["content"]
        for literal in ("Vendo", "10kk", "detalhes em"):
            self.assertIn(literal, prompt)
        for immutable in ("[Club]", "exemplo.com", "/item/1"):
            self.assertNotIn(immutable, prompt)

    def test_price_looking_url_is_restored_before_normal_price_check(self):
        client = self.client("See [[L2ITEM0]] for 2kk")
        self.assertEqual(client.translate("Смотри source.com/10kk за 2kk"),
                         "See source.com/10kk for 2kk")
        self.assertEqual(client._request.call_count, 1)
        prompt = client._request.call_args.args[1]["messages"][-1]["content"]
        self.assertNotIn("10kk", prompt)
        self.assertIn("2kk", prompt)

    def test_price_looking_url_is_restored_after_item_recovery(self):
        client = self.client("See [[L2ITEM1]] for 2kk")
        client._translate_fragments = Mock(return_value="See [Club] source.com/10kk for 2kk")
        self.assertEqual(client.translate("Смотри [Club] source.com/10kk за 2kk", item_links=("[Club]",)),
                         "See [Club] source.com/10kk for 2kk")
        client._translate_fragments.assert_called_once()
        self.assertIn("source.com/10kk", client._translate_fragments.call_args.args[1].values())

    def test_price_looking_url_is_restored_after_price_recovery(self):
        client = self.client("See [[L2ITEM0]] for 3kk")
        client._translate_fragments = Mock(return_value="See source.com/10kk for 2kk")
        self.assertEqual(client.translate("Смотри source.com/10kk за 2kk"),
                         "See source.com/10kk for 2kk")
        client._translate_fragments.assert_called_once()
        invariants = client._translate_fragments.call_args.args[1]
        self.assertIn("source.com/10kk", invariants.values())
        self.assertIn("2kk", invariants.values())

    def test_recovery_still_rejects_a_changed_real_price(self):
        client = self.client("See [[L2ITEM0]] for 3kk")
        client._translate_fragments = Mock(return_value="See source.com/path for 4kk")
        with self.assertRaisesRegex(OllamaError, "price during recovery"):
            client.translate("Смотри source.com/path за 2kk")

    def test_missing_url_gets_one_bounded_fragment_recovery(self):
        client = self.client("Watch now", json.dumps({"part0": "Watch"}))
        self.assertEqual(client.translate("Смотри source.com/path"), "Watch source.com/path")
        self.assertEqual(client._request.call_count, 2)
        recovery_prompt = client._request.call_args.args[1]["messages"][-1]["content"]
        self.assertNotIn("source.com", recovery_prompt)
        self.assertNotIn("Watch now", recovery_prompt)

    def test_invalid_missing_url_recovery_does_not_retry_again(self):
        client = self.client("Watch now", "not JSON")
        with self.assertRaises(OllamaError):
            client.translate("Смотри source.com/path")
        self.assertEqual(client._request.call_count, 2)

    def test_acronym_host_and_price_paths_are_one_immutable_url(self):
        for source in (
            "Смотри pvp.example.com/path", "Смотри source.com/WTB/10kk", "Смотри source.com/WTB",
        ):
            with self.subTest(source=source):
                client = self.client("See [[L2ITEM0]]")
                self.assertEqual(client.translate(source), source.replace("Смотри", "See"))
                self.assertEqual(client._request.call_count, 1)
                prompt = client._request.call_args.args[1]["messages"][-1]["content"]
                for hidden in ("example.com", "source.com", "10kk", "WTB"):
                    self.assertNotIn(hidden, prompt)

    def test_cosmetic_whole_url_marker_changes_remain_supported(self):
        for marker in ("l2item0", "[[l2item0]]", "[L2ITEM0]"):
            with self.subTest(marker=marker):
                client = self.client("See " + marker)
                self.assertEqual(client.translate("Смотри source.com/WTB/10kk"),
                                 "See source.com/WTB/10kk")
                self.assertEqual(client._request.call_count, 1)

    def test_mixed_bare_and_explicit_urls_both_remain_exact(self):
        client = self.client("See [[L2ITEM0]] and [[L2ITEM1]]")
        self.assertEqual(client.translate("Смотри pvp.example.com/path и https://second.com/10kk"),
                         "See pvp.example.com/path and https://second.com/10kk")
        self.assertEqual(client._request.call_count, 1)

    def test_unrelated_multiple_and_outgoing_paths_follow_their_contracts(self):
        for source, output, expected, target in (
            ("Price is 10kk", "Price is 10kk", "Price is 10kk", "English"),
            ("Watch source.com/path", "Смотри [[L2ITEM0]]", "Смотри source.com/path", "Russian"),
            ("Смотри source.com/path.", "See [[L2ITEM0]]", "See source.com/path.", "English"),
            ("source.com/a and second.com/b", "[[L2ITEM0]] and [[L2ITEM1]]",
             "source.com/a and second.com/b", "English"),
        ):
            with self.subTest(source=source):
                client = self.client(output, target=target)
                self.assertEqual(client.translate(source), expected)
                self.assertEqual(client._request.call_count, 1)

    def test_recovery_cannot_silently_change_or_omit_a_url(self):
        for recovered in ("See changed.com/path", "See source.com/path/extra", "See nothing",
                          "See source.com/path and invented.com/path"):
            with self.subTest(recovered=recovered):
                client = self.client("The model omitted the marker")
                client._translate_fragments = Mock(return_value=recovered)
                with self.assertRaisesRegex(OllamaError, "URL during recovery"):
                    client.translate("Смотри source.com/path")
                client._translate_fragments.assert_called_once()
                self.assertEqual(client._request.call_count, 1)

    def test_appended_path_text_query_or_prefix_requires_recovery(self):
        for altered in ("[[L2ITEM0]]/extra", "[[L2ITEM0]]now", "[[L2ITEM0]]?q=invented",
                        "https://[[L2ITEM0]]", "www.[[L2ITEM0]]"):
            with self.subTest(altered=altered):
                client = self.client("See " + altered, json.dumps({"part0": "See"}))
                self.assertEqual(client.translate("Смотри source.com/path"), "See source.com/path")
                self.assertEqual(client._request.call_count, 2)

    def test_appended_path_text_or_prefix_in_outgoing_reply_is_rejected(self):
        for altered in ("[[L2ITEM0]]/extra", "[[L2ITEM0]]now", "[[L2ITEM0]]?q=invented",
                        "https://[[L2ITEM0]]", "www.[[L2ITEM0]]"):
            with self.subTest(altered=altered):
                client = self.client("Смотри " + altered, target="Russian")
                with self.assertRaisesRegex(OllamaError, "URL"):
                    client.translate("Watch source.com/path")
                self.assertEqual(client._request.call_count, 1)

    def test_extra_invented_link_is_not_accepted_beside_a_valid_marker(self):
        client = self.client("See [[L2ITEM0]] or invented.com/path", json.dumps({"part0": "See"}))
        self.assertEqual(client.translate("Смотри source.com/path"), "See source.com/path")
        self.assertEqual(client._request.call_count, 2)

    def test_model_punctuation_after_url_marker_does_not_change_source_address(self):
        for source_url in ("source.com/path", "source.com/path.", "source.com/path!", "source.com/path?"):
            for appended in (".", ",", ";", ":", "!", "?", "...?!"):
                with self.subTest(source_url=source_url, appended=appended):
                    client = self.client("See [[L2ITEM0]]" + appended)
                    self.assertEqual(client.translate("Смотри " + source_url), "See " + source_url)
                    self.assertEqual(client._request.call_count, 1)

    def test_punctuation_after_item_and_acronym_markers_is_retained(self):
        client = self.client("Send [[L2ITEM0]], sell [[L2ITEM1]]; see [[L2ITEM2]].")
        self.assertEqual(client.translate("Напиши PM, продай [Club]; смотри source.com/path",
                                          item_links=("[Club]",)),
                         "Send PM, sell [Club]; see source.com/path")
        self.assertEqual(client._request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
