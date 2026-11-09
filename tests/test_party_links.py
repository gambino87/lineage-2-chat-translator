"""Synthetic party/item link checks; model responses are mocked, never inferred."""
import json
import re
import unittest
from unittest.mock import Mock, patch

from translator_core import ChatRecord, OllamaClient, OllamaError, decode_chat_links, decode_item_links, parse_chat_line


def link(title, kind=10, class_id=217):
    # Deliberately share an item ClassID: Type, not the numeric ID, is authoritative.
    return (f"\x08\tType={kind} \tID=12345 \tColor=0 \tUnderline=0 "
            f"\tClassID={class_id} \tTitle=\x1b{title}\x1b\x08")


def record(message):
    return parse_chat_line(
        "(12:34:56) Log: (Receive)[S_SAY2] SayPacket2ID : 123 Name : Synthetic "
        f"Say Msg:{message} Type:25\r\n")


def translated(content):
    return {"message": {"content": content}, "done_reason": "stop"}


class PartyLinkParsingTests(unittest.TestCase):
    def test_party_title_is_readable_and_only_its_details_are_immutable(self):
        title = "Ищем хилера [12~19] (8/9)"
        with patch("translator_core.resolve_item_title", side_effect=AssertionError("Party is not an item")):
            text, items, details = decode_chat_links(link(title))
        self.assertEqual(text, title)
        self.assertEqual(items, ())
        self.assertEqual(details, ("[12~19]", "(8/9)"))
        self.assertNotIn(title, details)

    def test_range_and_count_punctuation_and_spacing_are_kept_exactly(self):
        for level_range in ("[12~19]", "[12-19]", "[ 12 ~ 19 ]", "[ 12 - 19 ]"):
            with self.subTest(level_range=level_range):
                title = f"Looking for a healer {level_range} ( 8 / 9 )"
                text, items, details = decode_chat_links(link(title))
                self.assertEqual(text, title)
                self.assertEqual(items, ())
                self.assertEqual(details, (level_range, "( 8 / 9 )"))

    def test_party_description_without_details_is_still_translatable(self):
        self.assertEqual(decode_chat_links(link("Ищем хилера")), ("Ищем хилера", (), ()))

    def test_item_and_party_in_one_message_remain_different_kinds(self):
        title = "Ищем хилера [12~19] (8/9)"
        parsed = record("WTS " + link("闪耀刀", kind=1) + " / " + link(title))
        self.assertEqual(parsed.message, "WTS [Shining Knife] / " + title)
        self.assertEqual(parsed.item_links, ("[Shining Knife]",))
        self.assertEqual(parsed.party_details, ("[12~19]", "(8/9)"))
        self.assertNotIn("ClassID", parsed.message)

    def test_adjacent_and_shared_delimiter_links_keep_order_and_metadata(self):
        party_title = "Ищем хилера [12~19] (8/9)"
        party, item = link(party_title), link("团结之剑", kind=1, class_id=738)
        for first, second, expected in ((party, item, party_title + "[Sword of Solidarity]"),
                                        (item, party, "[Sword of Solidarity]" + party_title)):
            for shared in (False, True):
                with self.subTest(first_party=first == party, shared=shared):
                    raw = (first[:-1] if shared else first) + second
                    text, items, details = decode_chat_links(raw)
                    self.assertEqual(text, expected)
                    self.assertEqual(items, ("[Sword of Solidarity]",))
                    self.assertEqual(details, ("[12~19]", "(8/9)"))
                    for metadata in ("Type=", "ClassID=", "Title=", "\x08", "\x1b"):
                        self.assertNotIn(metadata, text)

    def test_multiple_party_links_do_not_combine_or_drop_descriptions(self):
        first = "Нужен хилер [12~19] (8/9)"
        second = "Ищем лучников [14~21] (1/12)"
        text, items, details = decode_chat_links(link(first) + " / " + link(second))
        self.assertEqual(text, first + " / " + second)
        self.assertEqual(items, ())
        self.assertEqual(details, ("[12~19]", "(8/9)", "[14~21]", "(1/12)"))

    def test_repeated_details_are_metadata_once_but_text_occurrences_survive(self):
        title = "Ищем хилера [12~19] (8/9)"
        text, items, details = decode_chat_links(link(title) + " / " + link(title))
        self.assertEqual(text.count("[12~19]"), 2)
        self.assertEqual(text.count("(8/9)"), 2)
        self.assertEqual(details, ("[12~19]", "(8/9)"))
        self.assertEqual(items, ())

    def test_missing_terminal_backspace_keeps_following_chat(self):
        title = "Ищем хилера [12~19] (8/9)"
        self.assertEqual(decode_chat_links(link(title)[:-1] + " PM me"),
                         (title + " PM me", (), ("[12~19]", "(8/9)")))

    def test_malformed_party_link_is_not_an_item_and_cannot_use_item_catalog(self):
        cases = ["\x08\tType=10 \tClassID=217\x08", link(""),
                 "\x08\tType=10 \tClassID=217 \tTitle=\x1bunfinished [12~19] (8/9)\x08"]
        with patch("translator_core.resolve_item_title", side_effect=AssertionError("Party is not an item")):
            for raw in cases:
                with self.subTest(raw=raw):
                    text, items, details = decode_chat_links(raw)
                    self.assertEqual(text, "[Party link]")
                    self.assertNotIn("[Linked item]", text)
                    self.assertNotIn("[Shining Knife]", text)
                    self.assertEqual(details, ())

    def test_plain_text_that_looks_like_details_does_not_gain_link_metadata(self):
        text = "Ищем хилера [12~19] (8/9) Type=10 ClassID=217"
        self.assertEqual(decode_chat_links(text), (text, (), ()))

    def test_legacy_decoder_and_positional_chat_record_remain_compatible(self):
        title = "Ищем хилера [12~19] (8/9)"
        self.assertEqual(decode_item_links(link(title)), (title, ()))
        self.assertEqual(decode_item_links(link("闪耀刀", kind=1)),
                         ("[Shining Knife]", ("[Shining Knife]",)))
        parsed = ChatRecord("12:34:56", "Synthetic", "text", 25, (), "Russian")
        self.assertEqual(parsed.source_language, "Russian")
        self.assertEqual(parsed.party_details, ())


class PartyLinkTranslationTests(unittest.TestCase):
    def client(self, rewrite=None, *, target="English"):
        client = OllamaClient(target_language=target)
        rewrite = rewrite or (lambda text: text.replace("Ищем хилера", "Looking for a healer"))

        def respond(path, payload=None, **_kwargs):
            if path == "/api/show":
                return {}
            self.assertEqual(path, "/api/chat")
            text = payload["messages"][-1]["content"]
            return translated(rewrite(text))

        client._request = Mock(side_effect=respond)
        return client

    def model_inputs(self, client):
        return [call.args[1]["messages"][-1]["content"] for call in client._request.call_args_list
                if call.args[0] == "/api/chat"]

    def test_raw_party_link_translates_words_without_exposing_fixed_details_to_model(self):
        client = self.client()
        title = "Ищем хилера [12~19] (8/9)"
        self.assertEqual(client.translate(link(title)), "Looking for a healer [12~19] (8/9)")
        inputs = self.model_inputs(client)
        self.assertEqual(len(inputs), 1)
        self.assertIn("Ищем хилера", inputs[0])
        for hidden in ("[12~19]", "(8/9)", "ClassID", "Type=", "\x08", "\x1b"):
            self.assertNotIn(hidden, inputs[0])

    def test_parsed_party_metadata_is_preserved_through_leading_tag_recursion(self):
        parsed = record("LFG " + link("Ищем хилера [12~19] (8/9)"))
        client = self.client()
        result = client.translate(parsed.message, item_links=parsed.item_links, party_details=parsed.party_details)
        self.assertEqual(result, "LFG Looking for a healer [12~19] (8/9)")

    def test_mixed_party_item_translation_keeps_english_catalog_names(self):
        source = link("Ищем хилера [12~19] (8/9)") + " / " + link("闪耀刀", kind=1)
        for raw in (True, False):
            with self.subTest(raw=raw):
                client = self.client()
                parsed = record(source)
                result = (client.translate(source) if raw else
                          client.translate(parsed.message, item_links=parsed.item_links, party_details=parsed.party_details))
                self.assertEqual(result, "Looking for a healer [12~19] (8/9) / [Shining Knife]")
                for value in self.model_inputs(client):
                    self.assertNotIn("Shining Knife", value)
                    self.assertNotIn("闪耀刀", value)

    def test_repeated_party_details_are_restored_per_occurrence(self):
        client = self.client()
        party = link("Ищем хилера [ 12 - 19 ] ( 8 / 9 )")
        result = client.translate(party + " / " + party)
        self.assertEqual(result, "Looking for a healer [ 12 - 19 ] ( 8 / 9 ) / Looking for a healer [ 12 - 19 ] ( 8 / 9 )")

    def test_non_english_target_translates_description_but_keeps_english_item_name(self):
        client = self.client(lambda text: text.replace("Looking for healer", "Ищем хилера"), target="Russian")
        source = link("Looking for healer [12~19] (8/9)") + " / " + link("团结之剑", kind=1, class_id=738)
        self.assertEqual(client.translate(source), "Ищем хилера [12~19] (8/9) / [Sword of Solidarity]")

    def test_party_description_remains_language_evidence_but_items_and_details_do_not(self):
        source = link("Ищем хилера [12~19] (8/9)") + " " + link("闪耀刀", kind=1)
        for raw in (True, False):
            with self.subTest(raw=raw):
                client = OllamaClient()
                client._request = Mock(side_effect=[{}, translated('{"language":"Russian"}')])
                parsed = record(source)
                answer = (client.detect_language(source) if raw else
                          client.detect_language(parsed.message, item_links=parsed.item_links, party_details=parsed.party_details))
                self.assertEqual(answer, "Russian")
                text = self.model_inputs(client)[0]
                self.assertIn("Ищем хилера", text)
                for hidden in ("[12~19]", "(8/9)", "Shining Knife", "闪耀刀"):
                    self.assertNotIn(hidden, text)

    def test_changed_dropped_and_duplicated_party_tokens_recover_from_original_details(self):
        title = "Ищем хилера [12~19] (8/9)"
        for damaged in ("Looking for a healer [99~100] (1/1)",
                        "Looking for a healer", "Looking for a healer [[L2ITEM0]] [[L2ITEM0]]"):
            with self.subTest(damaged=damaged):
                client = OllamaClient()
                client._request = Mock(side_effect=[{}, translated(damaged),
                                                    translated('{"part0":"Looking for a healer"}')])
                self.assertEqual(client.translate(link(title)), "Looking for a healer [12~19] (8/9)")
                inputs = self.model_inputs(client)
                self.assertEqual(len(inputs), 2)
                fragments = json.loads(inputs[-1])
                self.assertIn("Ищем хилера", " ".join(fragments.values()))
                self.assertNotIn("99", inputs[-1])
                self.assertNotIn("12~19", inputs[-1])
                self.assertNotIn("8/9", inputs[-1])

    def test_non_english_target_rejects_lost_details_instead_of_returning_changed_numbers(self):
        client = OllamaClient(target_language="Russian")
        client._request = Mock(side_effect=[{}, translated("Ищем хилера [99~100] (1/1)")])
        with self.assertRaises(OllamaError):
            client.translate(link("Looking for a healer [12~19] (8/9)"))
        self.assertEqual(len(self.model_inputs(client)), 1)

    def test_multiple_parties_recover_if_model_moves_details_to_different_description(self):
        client = OllamaClient()
        source = link("Нужен хилер [12~19] (8/9)") + " / " + link("Нужен танк [14~21] (1/12)")

        def respond(path, payload=None, **_kwargs):
            if path == "/api/show":
                return {}
            text = payload["messages"][-1]["content"]
            if "format" in payload:
                fragments = json.loads(text)
                return translated(json.dumps({key: value.replace("Нужен хилер", "Need healer").replace("Нужен танк", "Need tank")
                                               for key, value in fragments.items()}))
            tokens = re.findall(r"\[\[[^\[\]]+\]\]", text)
            self.assertEqual(len(tokens), 4)
            return translated(f"Need healer {tokens[2]} {tokens[3]} / Need tank {tokens[0]} {tokens[1]}")

        client._request = Mock(side_effect=respond)
        self.assertEqual(client.translate(source), "Need healer [12~19] (8/9) / Need tank [14~21] (1/12)")
        self.assertEqual(len(self.model_inputs(client)), 2)

    def test_extra_literal_count_next_to_valid_tokens_is_removed_by_recovery(self):
        client = OllamaClient()

        def respond(path, payload=None, **_kwargs):
            if path == "/api/show":
                return {}
            text = payload["messages"][-1]["content"]
            if "format" in payload:
                fragments = json.loads(text)
                return translated(json.dumps({key: "Looking for a healer" for key in fragments}))
            return translated(text.replace("Ищем хилера", "Looking for a healer") + " (1/1)")

        client._request = Mock(side_effect=respond)
        self.assertEqual(client.translate(link("Ищем хилера [12~19] (8/9)")),
                         "Looking for a healer [12~19] (8/9)")
        self.assertEqual(len(self.model_inputs(client)), 2)

    def test_recovery_that_invents_an_extra_count_fails_without_more_retries(self):
        client = OllamaClient()
        client._request = Mock(side_effect=[{}, translated("Looking for a healer"),
                                            translated('{"part0":"Looking for a healer (1/1)"}')])
        with self.assertRaises(OllamaError):
            client.translate(link("Ищем хилера [12~19] (8/9)"))
        self.assertEqual(len(self.model_inputs(client)), 2)


if __name__ == "__main__":
    unittest.main()
