"""Context and protection regressions; no Ollama service or UI required."""
import io
import json
import re
import unittest
from unittest.mock import Mock, patch

import game_slang
from game_slang import Hint, game_glossary
from translator_core import OllamaClient


class GameSlangTests(unittest.TestCase):
    def test_literal_words_do_not_receive_game_meanings(self):
        for source in (
            "I bought a wig yesterday.",
            "The train leaves at six.",
            "Я купил парик вчера.",
            "Паровоз приехал на вокзал.",
            "我想喝奶茶。",
            "我想喝奶茶，没蓝色的杯子。",
            "这个打手很奇怪。",
            "开火车很累，别怪我没精神。",
            "La sala es oscura.",
            "Hola pjLuna, buenas noches.",
        ):
            with self.subTest(source=source):
                self.assertEqual(game_glossary(source, "English"), "")

    def test_pj_requires_a_whole_token(self):
        for source in ("Mi pj es nivel 40.", "Cambio de PJ.", "Tengo dos pjs."):
            with self.subTest(source=source):
                self.assertIn("player character", game_glossary(source, "English"))
        for source in ("pjLuna", "Lunapj", "pj_Luna", "pjsmith"):
            with self.subTest(source=source):
                self.assertEqual(game_glossary(source, "English"), "")

    def test_mob_train_requires_combat_context(self):
        self.assertEqual(game_glossary("парик", "English"), "")
        self.assertEqual(game_glossary("паровоз", "English"), "")
        self.assertIn("mob train", game_glossary("Не агри парик, хил без маны.", "English"))

    def test_single_chinese_characters_do_not_trigger_hints(self):
        for source in ("奶", "魔", "蓝", "藍", "怪", "拉", "引", "魂", "弹", "彈", "收", "密"):
            with self.subTest(source=source):
                self.assertEqual(game_glossary(source, "English"), "")

    def test_moscow_time_hint_requires_a_time_and_keeps_other_uses_unforced(self):
        for source in ("20-24 мск", "Встречаемся в 21:00 МСК.", "Играю после 18мск."):
            with self.subTest(source=source):
                self.assertIn("Moscow time", game_glossary(source, "English"))
        for source in ("Я живу в МСК.", "mskPlayer", "Player21мск", "21мскPlayer", "500мск"):
            with self.subTest(source=source):
                self.assertNotIn("Moscow time", game_glossary(source, "English"))
        self.assertEqual(game_glossary("В 21:00 мск", "Russian"), "")

    def test_non_english_targets_receive_no_incoming_glossary(self):
        source = "Куплю соски. Bora upar, pj. 奶妈拉怪。"
        for target in ("Russian", "Chinese", "Portuguese", "Spanish", "GERMAN"):
            with self.subTest(target=target):
                self.assertEqual(game_glossary(source, target), "")
        self.assertTrue(game_glossary(source, "eNgLiSh"))

    def test_hint_count_is_bounded_independently_of_candidate_inventory(self):
        count = game_slang.MAX_HINTS + 4
        meanings = [f"hint_{number:02}" for number in range(count)]
        rules = tuple(Hint(re.compile("trigger"), meaning) for meaning in meanings)
        with patch.object(game_slang, "_INCOMING", rules):
            result = game_glossary("trigger", "English")
        selected = [meaning for meaning in meanings if meaning in result]
        self.assertGreater(len(selected), 0)
        self.assertLessEqual(len(selected), game_slang.MAX_HINTS)

    def test_hint_body_character_budget_includes_separators(self):
        # Force the character limit before the count limit without depending
        # on which real-world terms survive the model-quality benchmark.
        meanings = [f"budget_{number}:" + "x" * (game_slang.MAX_HINT_CHARS // 3)
                    for number in range(5)]
        rules = tuple(Hint(re.compile("trigger"), meaning) for meaning in meanings)
        with patch.object(game_slang, "_INCOMING", rules):
            result = game_glossary("trigger", "English")
        selected = [meaning for meaning in meanings if meaning in result]
        self.assertGreater(len(selected), 0)
        self.assertLess(len(selected), len(meanings))
        self.assertLessEqual(len("; ".join(selected)), game_slang.MAX_HINT_CHARS)
        # The exported limit covers meanings; fixed explanatory wording is
        # allowed a small, separate overhead rather than an unbounded prompt.
        self.assertLessEqual(len(result), game_slang.MAX_HINT_CHARS + 200)

    def test_single_oversized_hint_is_not_inserted(self):
        huge = Hint(re.compile("trigger"), "x" * (game_slang.MAX_HINT_CHARS + 1))
        with patch.object(game_slang, "_INCOMING", (huge,)):
            self.assertEqual(game_glossary("trigger", "English"), "")


class SlangPromptIntegrationTests(unittest.TestCase):
    def client(self, *outputs, target="English"):
        client = OllamaClient(target_language=target)
        client._opener = Mock()
        responses = [{}, *({"message": {"content": output}} for output in outputs)]
        client._opener.open.side_effect = [
            io.BytesIO(json.dumps(response).encode("utf-8")) for response in responses
        ]
        return client

    def chat_payloads(self, client):
        return [json.loads(call.args[0].data)
                for call in client._opener.open.call_args_list
                if call.args[0].full_url.endswith("/api/chat")]

    def test_hints_do_not_rewrite_source_sent_to_model(self):
        source = "Bora upar com meu pj?"
        client = self.client("Let's level up with my character?")
        client.translate(source)
        payload = self.chat_payloads(client)[0]
        self.assertEqual(payload["messages"][1], {"role": "user", "content": source})
        self.assertIn("vocabulary for this message", payload["messages"][0]["content"])
        self.assertIn("player character", payload["messages"][0]["content"])

    def test_link_and_url_contents_do_not_supply_hints(self):
        cases = (
            ("Look at [upar pj 奶妈] for details.", ("[upar pj 奶妈]",)),
            ("Look at [20 мск] for details.", ("[20 мск]",)),
            ("See https://example.com/upar/pj for details.", ()),
        )
        for source, links in cases:
            with self.subTest(source=source):
                masked = source.replace(links[0] if links else "https://example.com/upar/pj",
                                        "[[L2ITEM0]]")
                client = self.client(masked)
                self.assertEqual(client.translate(source, item_links=links), source)
                payload = self.chat_payloads(client)[0]
                self.assertEqual(payload["messages"][1]["content"], masked)
                self.assertNotIn("vocabulary for this message", payload["messages"][0]["content"])

    def test_masked_item_does_not_hide_real_chat_or_add_its_own_hint(self):
        source = "Mi pj vende [upar]."
        client = self.client("My character sells [[L2ITEM0]].")
        self.assertEqual(client.translate(source, item_links=("[upar]",)),
                         "My character sells [upar].")
        payload = self.chat_payloads(client)[0]
        system = payload["messages"][0]["content"]
        self.assertIn("player character", system)
        self.assertNotIn("upar =", system)
        self.assertEqual(payload["messages"][1]["content"], "Mi pj vende [[L2ITEM0]].")

    def test_non_english_translation_prompt_remains_without_incoming_hints(self):
        source = "Bora upar com meu pj?"
        for target in ("Spanish", "Russian", "Chinese", "Portuguese"):
            with self.subTest(target=target):
                client = self.client("Translation", target=target)
                client.translate(source)
                payload = self.chat_payloads(client)[0]
                self.assertNotIn("vocabulary for this message", payload["messages"][0]["content"])
                self.assertEqual(payload["messages"][1]["content"], source)

    def test_fragment_recovery_uses_hints_only_from_unprotected_chat(self):
        client = self.client("Missing linked item", '{"part0":"Let us level up"}')
        result = client.translate("Bora upar [pj].", item_links=("[pj]",))
        self.assertEqual(result, "Let us level up [pj].")
        first, recovery = self.chat_payloads(client)
        for payload in (first, recovery):
            with self.subTest(format=payload.get("format")):
                self.assertIn("upar =", payload["messages"][0]["content"])
                self.assertNotIn("pj = player character", payload["messages"][0]["content"])
        self.assertEqual(json.loads(recovery["messages"][1]["content"]), {"part0": "Bora upar"})


if __name__ == "__main__":
    unittest.main()
