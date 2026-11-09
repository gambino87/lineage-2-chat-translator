"""Price-phrase context and masking checks, without real model or log access."""
import io
import json
import unittest
from unittest.mock import Mock

from game_slang import game_glossary
from translator_core import OllamaClient


class PortuguesePriceSlangTests(unittest.TestCase):
    def test_phrase_requires_bounded_words_and_trade_context(self):
        for text in ("[[L2ITEM0]] no precinho", "[[_L2ITEM0]] NO PRECINHO",
                     "Compro no precinho", "Não vendo no precinho, 20kk",
                     "No precinho: 2 por 10kk cada"):
            with self.subTest(text=text):
                self.assertIn("no precinho = at a good price", game_glossary(text, "English"))
        for text in ("precinho", "Encontrei João no Precinho ontem.",
                     "Meu vizinho consertou a cerca.", "vendo no precinhoPlayer",
                     "vendo deno precinho", "no precinhos 10kk"):
            with self.subTest(text=text):
                self.assertNotIn("no precinho =", game_glossary(text, "English"))

    def client(self, outputs):
        client = OllamaClient()
        client._opener = Mock()
        client._opener.open.side_effect = [io.BytesIO(json.dumps(value).encode()) for value in [
            {}, *({"message": {"content": text}} for text in outputs)]]
        return client

    def payloads(self, client):
        return [json.loads(call.args[0].data) for call in client._opener.open.call_args_list
                if call.args[0].full_url.endswith("/api/chat")]

    def test_protected_item_and_url_words_cannot_trigger_the_hint(self):
        for source, links in (("Look at [no precinho], 10kk.", ("[no precinho]",)),
                              ("See https://example.com/no%20precinho for details, 10kk.", ())):
            with self.subTest(source=source):
                client = self.client(["Look at [[L2ITEM0]], 10kk."])
                client.translate(source, item_links=links)
                self.assertNotIn("no precinho =", self.payloads(client)[0]["messages"][0]["content"])

    def test_recovery_retains_phrase_hint_without_sending_linked_title(self):
        client = self.client(["Missing linked item", '{"part0":"at a good price"}'])
        result = client.translate("[Recipe: Willow Staff] no precinho", item_links=("[Recipe: Willow Staff]",))
        self.assertEqual(result, "[Recipe: Willow Staff] at a good price")
        for payload in self.payloads(client):
            self.assertIn("no precinho = at a good price", payload["messages"][0]["content"])
            self.assertNotIn("Recipe: Willow Staff", payload["messages"][1]["content"])

    def test_non_english_targets_do_not_gain_incoming_phrase_hint(self):
        for target in ("Portuguese", "Russian", "Chinese", "Spanish"):
            self.assertEqual(game_glossary("Vendo no precinho 10kk", target), "")


if __name__ == "__main__":
    unittest.main()
