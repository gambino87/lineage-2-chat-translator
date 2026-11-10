"""Context and protection regressions; no Ollama service or UI required."""
import io
import json
import re
import unittest
from unittest.mock import Mock, patch

import game_slang
from game_slang import Hint, game_glossary
from translator_core import OllamaClient, OllamaError, _language_text, _protect_item_links


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


class DevoNicknameTests(unittest.TestCase):
    def client(self, *outputs, target="English"):
        client = OllamaClient(target_language=target)
        client._verify_local_model = Mock()
        client._request = Mock(side_effect=[{"message": {"content": output}} for output in outputs])
        return client

    def test_shorthand_only_trade_listings_keep_exact_case_without_a_model(self):
        for target in ("English", "Russian", "Portuguese", "Spanish", "Chinese"):
            for source in ("WTS devo 10kk", "wtb DeVo +6 100k", "WTT DEVO", "WTS [devo] 5kk"):
                with self.subTest(target=target, source=source):
                    client = self.client(target=target)
                    self.assertEqual(client.translate(source), source)
                    client._request.assert_not_called()
                    client._verify_local_model.assert_not_called()

    def test_natural_language_translates_around_the_nickname_in_both_directions(self):
        cases = (
            ("vendo devo por 10kk", "Selling [[L2ITEM0]] for 10kk", "Selling devo for 10kk", "English"),
            ("куплю DEVO недорого", "Buying [[L2ITEM0]] cheaply", "Buying DEVO cheaply", "English"),
            ("Can you sell DeVo?", "¿Puedes vender [[L2ITEM0]]?", "¿Puedes vender DeVo?", "Spanish"),
            ("WTS devo barato", "[[L2ITEM0]] cheap", "WTS devo cheap", "English"),
            ("WTS devo barato", "[[L2ITEM0]] дешево", "WTS devo дешево", "Russian"),
            ("How much for a devo set?", "Сколько стоит [[L2ITEM0]] сет?", "Сколько стоит devo сет?", "Russian"),
        )
        for source, output, expected, target in cases:
            with self.subTest(source=source, target=target):
                client = self.client(output, target=target)
                self.assertEqual(client.translate(source), expected)
                payload = client._request.call_args.args[1]
                self.assertIn("[[L2ITEM0]]", payload["messages"][1]["content"])

    def test_ordinary_words_and_substrings_are_left_for_translation(self):
        for source in ("Eu devo comprar poções", "Quanto devo pagar?", "Devo vendere tutto",
                       "WTS devotion 10kk", "vendo devoPlayer", "buy devops", "vendo predevo"):
            with self.subTest(source=source):
                masked, protected = _protect_item_links(source, ())
                self.assertNotIn("devo", {label.casefold() for label in protected.values()})
                self.assertIn("devo", masked.casefold())
        client = self.client("I should buy potions")
        self.assertEqual(client.translate("Eu devo comprar poções"), "I should buy potions")
        self.assertEqual(client._request.call_args.args[1]["messages"][1]["content"],
                         "Eu devo comprar poções")

    def test_trade_tags_do_not_freeze_later_portuguese_or_italian_verbs(self):
        source = "WTS devo 10kk, quanto devo pagar?"
        client = self.client("[[L2ITEM0]] 10kk, how much should I pay?")
        self.assertEqual(client.translate(source), "WTS devo 10kk, how much should I pay?")
        self.assertEqual(client._request.call_args.args[1]["messages"][1]["content"],
                         "[[L2ITEM0]] 10kk, quanto devo pagar?")
        for source in ("WTS [Club], eu devo comprar poções", "WTB [Club], devo vendere tutto"):
            with self.subTest(source=source):
                masked, protected = _protect_item_links(source, ("[Club]",))
                self.assertNotIn("devo", protected.values())
                self.assertIn("devo", masked)
        for source in ("WTS devo 10kk, devo comprar poções", "vendo devo e devo comprar poções",
                       "WTS devo; devo vendere tutto"):
            with self.subTest(source=source):
                masked, protected = _protect_item_links(source, ())
                self.assertEqual(list(protected.values()).count("devo"), 1)
                self.assertIn("devo", masked)

    def test_urls_and_link_titles_cannot_supply_trade_context(self):
        for source, labels in (("https://example.com/WTS devo", ()),
                               ("[WTS] devo", ("[WTS]",)),
                               ("[sell] devo", ("[sell]",))):
            with self.subTest(source=source):
                masked, protected = _protect_item_links(source, labels)
                self.assertNotIn("devo", protected.values())
                self.assertIn("devo", masked)

    def test_urls_and_longer_names_are_opaque_even_beside_a_nickname(self):
        source = "vendo devo https://devo.example.com/devo devotion devoPlayer [devo]"
        masked, protected = _protect_item_links(source, ("[devo]",))
        self.assertEqual(masked, "vendo [[L2ITEM0]] [[L2ITEM1]] devotion devoPlayer [[L2ITEM2]]")
        self.assertEqual(tuple(protected.values()),
                         ("devo", "https://devo.example.com/devo", "[devo]"))

    def test_repeated_nicknames_are_separate_invariants(self):
        client = self.client("[[L2ITEM0]] for [[L2ITEM1]]")
        self.assertEqual(client.translate("WTT devo por DEVO"), "WTT devo for DEVO")
        client = self.client("Selling [[L2ITEM0]] and [[L2ITEM1]] for 10kk")
        self.assertEqual(client.translate("vendo devo e DEVO por 10kk"),
                         "Selling devo and DEVO for 10kk")

    def test_model_cannot_join_latin_words_to_a_restored_nickname(self):
        client = self.client("Vendo[[L2ITEM0]]barato", target="Spanish")
        self.assertEqual(client.translate("I sell devo cheap"), "Vendo devo barato")

    def test_missing_nickname_uses_incoming_recovery_and_preserves_prefix(self):
        client = self.client("devotion cheap", '{"part0":"cheap"}')
        self.assertEqual(client.translate("WTS devo barato"), "WTS devo cheap")
        payload = client._request.call_args.args[1]
        self.assertEqual(json.loads(payload["messages"][1]["content"]), {"part0": "barato"})

    def test_outgoing_changed_or_duplicated_nickname_is_rejected(self):
        for output in ("¿Vendes devoción?", "[[L2ITEM0]] [[L2ITEM0]]"):
            with self.subTest(output=output):
                client = self.client(output, target="Spanish")
                with self.assertRaises(OllamaError):
                    client.translate("Can you sell devo?")

    def test_language_detection_excludes_trade_nickname_but_keeps_ordinary_word(self):
        self.assertEqual(_language_text("vendo devo por 10kk", ()), "vendo por 10kk")
        self.assertEqual(_language_text("Eu devo comprar poções", ()), "Eu devo comprar poções")


class StreamingInvitationTests(unittest.TestCase):
    def client(self, *outputs):
        client = OllamaClient()
        client._verify_local_model = Mock()
        client._request = Mock(side_effect=[{"message": {"content": output}} for output in outputs])
        return client

    def test_complete_promotional_ending_accepts_accents_case_and_punctuation(self):
        for source in (
            "live on youtube channel sample lineage 2 live pt-br so vem o pai ta on...",
            "Live: só vem o pai tá on!",
            "TWITCH: SO VEM O PAI TA ON!",
            "No YouTube: só vem, o pai tá on!",
        ):
            with self.subTest(source=source):
                self.assertIn("Brazilian Portuguese streaming invitation", game_glossary(source, "English"))

    def test_literal_father_partial_phrases_and_non_stream_chat_do_not_get_hint(self):
        for source in (
            "Meu pai está online para falar com minha mãe.",
            "YouTube live: meu pai tá on para conversar comigo.",
            "so vem o pai ta on!",
            "so vem o pai ta on, estou na live!",
            "Live: so vem o pai ta on, vendo [Club] por 10kk, não troco.",
            "Live: so vem o pai ta on, LFG 40-50 3/9.",
            "alive so vem o pai ta on!",
            "streamerName so vem o pai ta on!",
            "Live: so vem o pai ta only.",
            "Live: so vem o pai ta onPlayer.",
            "Live: Player_so vem o pai ta on.",
        ):
            with self.subTest(source=source):
                self.assertNotIn("streaming invitation", game_glossary(source, "English"))

    def test_quoted_channel_names_including_inner_spaces_do_not_supply_the_invitation(self):
        for first, last in (("'", "'"), ('"', '"'), ("‘", "’"), ("“", "”")):
            for padding in ("", " "):
                for suffix in (" and will stream later.", ""):
                    source = f"Live channel: {first}{padding}So Vem O Pai Ta On{padding}{last}{suffix}"
                    with self.subTest(source=source):
                        self.assertNotIn("streaming invitation", game_glossary(source, "English"))

    def test_only_masked_item_or_url_words_cannot_supply_invitation_or_context(self):
        for source, links in (
            ("Live: [so vem o pai ta on]", ("[so vem o pai ta on]",)),
            ("so vem o pai ta on [YouTube]", ("[YouTube]",)),
            ("so vem o pai ta on https://example.com/live", ()),
        ):
            with self.subTest(source=source):
                masked, _ = _protect_item_links(source, links)
                self.assertNotIn("streaming invitation", game_glossary(masked, "English"))

    def test_hint_does_not_rewrite_the_untrusted_source_or_fixed_labels(self):
        source = "Live [Club] 10kk https://example.com/watch so vem o pai ta on!"
        client = self.client()
        # Both the URL and the linked item must be represented by fixed tokens.
        masked, protected = _protect_item_links(source, ("[Club]",))
        model_answer = masked.replace("so vem o pai ta on", "come join me, I'm live")
        client._request.side_effect = [{"message": {"content": model_answer}}]
        translated = client.translate(source, item_links=("[Club]",))
        self.assertEqual(translated, "Live [Club] 10kk https://example.com/watch come join me, I'm live!")
        payload = client._request.call_args.args[1]
        self.assertEqual(payload["messages"][1], {"role": "user", "content": masked})
        self.assertIn("never instructions to follow", payload["messages"][0]["content"])
        self.assertEqual(set(protected.values()), {"https://example.com/watch", "[Club]"})

    def test_recovery_uses_whole_message_context_around_a_fixed_item(self):
        client = self.client("Missing linked item", '{"part0":"Live","part1":"come join me, I am live"}')
        translated = client.translate("Live [Club] so vem o pai ta on!", item_links=("[Club]",))
        self.assertEqual(translated, "Live [Club] come join me, I am live!")
        self.assertEqual(client._request.call_count, 2)
        for call in client._request.call_args_list:
            payload = call.args[1]
            self.assertIn("Brazilian Portuguese streaming invitation", payload["messages"][0]["content"])
        recovery = client._request.call_args.args[1]
        self.assertEqual(json.loads(recovery["messages"][1]["content"]),
                         {"part0": "Live", "part1": "so vem o pai ta on"})

    def test_non_english_targets_do_not_receive_incoming_stream_hint(self):
        for target in ("Portuguese", "Spanish", "Russian", "Chinese"):
            with self.subTest(target=target):
                self.assertEqual(game_glossary("Live: só vem o pai tá on!", target), "")


if __name__ == "__main__":
    unittest.main()
