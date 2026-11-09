"""Synthetic universal-trade classification, without logs or model inference."""
import unittest
from unittest.mock import patch

import translator_core as core


def raw_link(title, *, kind=1, class_id=217):
    return (f"\x08\tType={kind} \tID=12345 \tColor=0 \tUnderline=0 "
            f"\tClassID={class_id} \tTitle=\x1b{title}\x1b\x08")


class UniversalTradeTests(unittest.TestCase):
    def setUp(self):
        self.request = self.enterContext(patch.object(
            core.OllamaClient, "_request", side_effect=AssertionError("Classification must not call a model.")))

    def test_marker_is_not_a_reply_language(self):
        self.assertEqual(core.UNIVERSAL_TRADE_LANGUAGE, "Universal trade")
        self.assertNotIn(core.UNIVERSAL_TRADE_LANGUAGE, core.REPLY_LANGUAGES)

    def test_linked_items_prices_and_trade_shorthand_are_universal(self):
        samples = (
            ("WTS [Club] 10kk", ("[Club]",)),
            ("wtb [Club] +6 2x 100kk PM me", ("[Club]",)),
            ("WTT [Club] / [Sword of Solidarity]", ("[Club]", "[Sword of Solidarity]")),
            ("WTS[Club]!!+", ("[Club]",)),
            ("WTS [Club] 1.800kk PM", ("[Club]",)),
        )
        for text, labels in samples:
            with self.subTest(text=text):
                self.assertTrue(core.is_universal_trade_message(text, item_links=labels))
        self.request.assert_not_called()

    def test_raw_item_link_is_decoded_before_classifying(self):
        source = "WTS " + raw_link("闪耀刀") + " 10kk PM me"
        self.assertTrue(core.is_universal_trade_message(source))
        self.request.assert_not_called()

    def test_complete_english_catalog_names_are_universal_without_links(self):
        for text in ("WTS Vorpal Tunic +6 150kk PM me", "WTB Sword of Solidarity 10kk",
                     "wts vorpal tunic 10kk"):
            with self.subTest(text=text):
                self.assertTrue(core.is_universal_trade_message(text))
        self.request.assert_not_called()

    def test_multilingual_and_short_meaningful_residue_is_not_universal(self):
        for suffix in ("no", "да", "yes", "need help", "aceito ofertas", "necesito ayuda",
                       "这个多少钱？", "продам дешевле", "sthn katapsuksh hsoun"):
            with self.subTest(suffix=suffix):
                self.assertFalse(core.is_universal_trade_message(
                    "WTS [Club] " + suffix, item_links=("[Club]",)))
        self.assertFalse(core.is_universal_trade_message("WTS这个多少钱？"))
        self.assertFalse(core.is_universal_trade_message("WTS Vorpal Tunic no"))

    def test_recognized_devotion_nickname_listings_are_universal(self):
        for text in ("WTS devo 10kk", "wtb DeVo +6 100k PM me", "WTT DEVO",
                     "WTS devo tunic 10kk", "WTB devo stockings", "WTS devo set",
                     "WTS [devo] 5kk"):
            with self.subTest(text=text):
                self.assertTrue(core.is_universal_trade_message(text))
        for text in ("vendo devo barato", "WTS devo barato", "WTS devo no",
                     "WTS devoPlayer", "devo comprar poções", "devo vendere tutto"):
            with self.subTest(text=text):
                self.assertFalse(core.is_universal_trade_message(text))
        self.request.assert_not_called()

    def test_party_and_recruitment_are_not_universal_trade(self):
        samples = (
            "LFG 40 lvl PM me",
            "LFM WTS [Club] 10kk",
            "WTS " + raw_link("LFG [12~19] (8/9)", kind=10),
            "WTS " + raw_link("WTS", kind=10),
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertFalse(core.is_universal_trade_message(text, item_links=("[Club]",)))
        self.assertFalse(core.is_universal_trade_message(
            "WTS [Club] [12~19] (8/9)", item_links=("[Club]",),
            party_details=("[12~19]", "(8/9)")))

    def test_trade_tag_must_be_outside_item_and_url_spans(self):
        for text, labels in (
            ("[WTS] 10kk", ("[WTS]",)),
            ("https://example.com/WTB/10kk", ()),
            ("wtb.example.com [Club] 10kk", ("[Club]",)),
            (raw_link("WTS", class_id=999999) + " 10kk", ()),
            ("WTSeller [Club] 10kk", ("[Club]",)),
        ):
            with self.subTest(text=text):
                self.assertFalse(core.is_universal_trade_message(text, item_links=labels))

    def test_unknown_and_unverified_item_shorthand_stay_unclassified(self):
        for text in ("", ".", "123", "hello", "[Club] 10kk", "WTS glorp",
                     "WTS Moirai Heavy", "WTS [need help]"):
            with self.subTest(text=text):
                self.assertFalse(core.is_universal_trade_message(text))

    def test_missing_catalog_fails_closed_for_unlinked_names(self):
        core._english_trade_item_names.cache_clear()
        self.addCleanup(core._english_trade_item_names.cache_clear)
        with patch.object(core, "get_catalog", side_effect=OSError("synthetic unavailable catalog")):
            self.assertFalse(core.is_universal_trade_message("WTS Vorpal Tunic 10kk"))
            self.assertTrue(core.is_universal_trade_message("WTS [Club] 10kk", item_links=("[Club]",)))
        self.request.assert_not_called()

    def test_oversized_raw_message_is_rejected_before_decoding_or_model_use(self):
        text = "WTS " + "x" * core.MAX_CHAT_RECORD_CHARACTERS
        with patch.object(core, "decode_chat_links", side_effect=AssertionError("Oversized input must stop first.")):
            self.assertFalse(core.is_universal_trade_message(text))
        self.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
