"""Price-integrity regressions with mocked local model responses."""
import io
import json
import unittest
from unittest.mock import Mock

from translator_core import OllamaClient, OllamaError, _PRICE, _protect_item_links


class PriceRecoveryTests(unittest.TestCase):
    def client(self, *outputs, target="English"):
        client = OllamaClient(target_language=target)
        client._opener = Mock()
        responses = [{}, *({"message": {"content": value}} for value in outputs)]
        client._opener.open.side_effect = [io.BytesIO(json.dumps(value).encode()) for value in responses]
        return client

    def test_changed_price_is_recovered_from_source_once(self):
        client = self.client("15000k each, no discounts.", '{"part0":"/each, no discounts"}')
        self.assertEqual(client.translate("1.5kk/each, no discounts."), "1.5kk/each, no discounts.")
        self.assertEqual(client._opener.open.call_count, 3)

    def test_missing_and_duplicate_prices_trigger_recovery(self):
        for output in ("Selling it.", "Selling for 2kk 2kk."):
            with self.subTest(output=output):
                client = self.client(output, '{"part0":"Selling for"}')
                self.assertEqual(client.translate("Selling for 2kk."), "Selling for 2kk.")

    def test_correct_price_does_not_change_prompt_or_add_requests(self):
        source = "Куплю соски Б грейда за 15kk."
        client = self.client("Buying B-grade shots for 15kk.")
        self.assertEqual(client.translate(source), "Buying B-grade shots for 15kk.")
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], source)
        self.assertEqual(client._opener.open.call_count, 2)

    def test_signed_comma_prices_and_negation_restore_exactly(self):
        client = self.client("Price 1.5kk, not 2k.", '{"part0":"Price","part1":"not"}')
        self.assertEqual(client.translate("Price -1,5kk, not +2K."), "Price -1,5kk, not +2K.")

    def test_linked_titles_and_urls_are_excluded_from_price_comparison(self):
        source = "See [10kk Sword] at https://example.com/25kk."
        client = self.client("See [[L2ITEM0]] at [[L2ITEM1]]")
        self.assertEqual(client.translate(source, item_links=("[10kk Sword]",)), source)
        self.assertEqual(client._opener.open.call_count, 2)

    def test_price_failure_restores_item_and_trade_direction_together(self):
        client = self.client("Selling [[L2ITEM0]] for 20kk.",
                             '{"part0":"Buying","part1":"for"}')
        self.assertEqual(client.translate("WTB Buying [Club] for 2kk.", item_links=("[Club]",)),
                         "WTB Buying [Club] for 2kk.")
        self.assertEqual(client._opener.open.call_count, 3)

    def test_recovery_cannot_add_a_second_price(self):
        client = self.client("15000k each.", '{"part0":"each, 2kk extra"}')
        with self.assertRaisesRegex(OllamaError, "price"):
            client.translate("1.5kk each.")
        self.assertEqual(client._opener.open.call_count, 3)

    def test_item_marker_recovery_cannot_invent_an_extra_price(self):
        client = self.client("Selling for 10kk.", '{"part0":"Selling 20kk","part1":"for"}')
        with self.assertRaisesRegex(OllamaError, "price"):
            client.translate("Selling [Club] for 10kk.", item_links=("[Club]",))
        self.assertEqual(client._opener.open.call_count, 3)

    def test_compact_lists_match_both_prices_without_matching_decimal_tails(self):
        for source in ("10kk,20kk", "10kk.20kk", "10kk;20kk"):
            with self.subTest(source=source):
                self.assertEqual(_PRICE.findall(source), ["10kk", "20kk"])
        self.assertEqual(_PRICE.findall("Player1.5kk Player1,5kk"), [])

    def test_swapped_prices_trigger_source_order_recovery(self):
        client = self.client("Price 20kk, not 10kk.", '{"part0":"Price","part1":"not"}')
        self.assertEqual(client.translate("Price 10kk, not 20kk."), "Price 10kk, not 20kk.")

    def test_invalid_recovery_is_an_error_without_more_retries(self):
        client = self.client("15000k each.", "not JSON")
        with self.assertRaises(OllamaError):
            client.translate("1.5kk each.")
        self.assertEqual(client._opener.open.call_count, 3)

    def test_other_target_behavior_is_unchanged(self):
        client = self.client("По 1.5кк за штуку.", target="Russian")
        self.assertEqual(client.translate("1.5kk each."), "По 1.5кк за штуку.")
        self.assertEqual(client._opener.open.call_count, 2)

    def test_amounts_inside_names_are_not_price_tokens(self):
        for source in ("Player10kk", "Player1.5kk", "10kkSword", "Player_2kk"):
            with self.subTest(source=source):
                self.assertFalse(_PRICE.search(source))
                self.assertEqual(_protect_item_links(source, (), protect_prices=True), (source, {}))


if __name__ == "__main__":
    unittest.main()
