import codecs
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import request

import translator_core
from chat_links import ChatLinkSpan
from translator_core import ChatRecord, LogTail, OllamaClient, OllamaError, parse_chat_line, read_recent_chat


def line(message="Привет, мир!", sender="Player", channel=25):
    return f"(16:23:42) Log: (Receive)[S_SAY2] SayPacket2ID : 123 Name : {sender} Say Msg:{message} Type:{channel}\r\n"


def item_link(title="Necklace of Frintezza", class_id=2_147_483_647):
    # Observed Origins link encoding; these are actual control characters.
    # Arbitrary synthetic titles use an unknown ID, not a real necklace ID.
    return ("\x08\tType=1 \tID=1078202847 \tColor=0 \tUnderline=0 "
            f"\tClassID={class_id} \tTitle=\x1b{title}\x1b\x08")


class ParserTests(unittest.TestCase):
    def test_oversized_and_out_of_range_protocol_records_are_ignored(self):
        self.assertIsNone(parse_chat_line(line("x" * translator_core.MAX_CHAT_RECORD_CHARACTERS)))
        for channel in ("9" * 5000, str(translator_core.MAX_SIGNED_INT32 + 1),
                        str(-translator_core.MAX_SIGNED_INT32 - 2)):
            with self.subTest(channel_size=len(channel)):
                self.assertIsNone(parse_chat_line(line(channel=channel)))
        self.assertEqual(parse_chat_line(line(channel=-1)).channel, -1)

    def test_multilingual_and_last_type_delimiter(self):
        self.assertEqual(parse_chat_line(line("Привет 世界 😀 Type:99", "Two Names", 1)),
                         ChatRecord("16:23:42", "Two Names", "Привет 世界 😀 Type:99", 1))

    def test_announcements_have_empty_sender(self):
        self.assertEqual(parse_chat_line(line("Welcome", "", 10)).sender, "")

    def test_ignores_nonchat_and_malformed_records(self):
        for value in ("Log: AuthLogin", line().replace("(Receive)", "(Send)"), line().replace("Type:25", "Type:no")):
            self.assertIsNone(parse_chat_line(value))

    def test_linked_item_uses_visible_title_instead_of_attributes(self):
        record = parse_chat_line(line("wts " + item_link(), "ExampleTrader", 25))
        self.assertEqual(record, ChatRecord(
            "16:23:42", "ExampleTrader", "wts [Necklace of Frintezza]", 25,
            ("[Necklace of Frintezza]",),
            link_spans=(ChatLinkSpan("item", 4, 4 + len("[Necklace of Frintezza]")),)))

    def test_multiple_and_repeated_links_preserve_surrounding_chat(self):
        first = item_link("Doom Plate Armor")
        second = item_link("Dynasty Halberd")
        record = parse_chat_line(line(f" WTS {first} set {second}; WTB {first} 10kk"))
        self.assertEqual(record.message,
                         " WTS [Doom Plate Armor] set [Dynasty Halberd]; WTB [Doom Plate Armor] 10kk")
        self.assertEqual(record.item_links, ("[Doom Plate Armor]", "[Dynasty Halberd]"))

    def test_link_title_survives_missing_final_backspace(self):
        record = parse_chat_line(line("Продам " + item_link("+6 Меч 世界")[:-1] + " за 10kk"))
        self.assertEqual(record.message, "Продам [+6 Меч 世界] за 10kk")
        self.assertEqual(record.item_links, ("[+6 Меч 世界]",))

    def test_damaged_links_show_placeholder_without_inventing_item_names(self):
        for broken in ("\x08\tType=1 \tID=123 \tClassID=2147483647\x08",
                       "\x08\tType=1 \tID=123 \tTitle=\x1bUnfinished title\x08",
                       item_link("")):
            with self.subTest(broken=repr(broken)):
                record = parse_chat_line(line("WTS " + broken + " 10kk"))
                self.assertEqual(record.message, "WTS [Linked item] 10kk")
                self.assertEqual(record.item_links, ("[Linked item]",))

    def test_plain_attributes_and_bracketed_text_are_not_item_links(self):
        text = "Type=1 ID=123 Title=hello [LFG] Type:99"
        record = parse_chat_line(line(text))
        self.assertEqual(record.message, text)
        self.assertEqual(record.item_links, ())


class TailTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "Network.log"
        self.tail = None

    def tearDown(self):
        if self.tail:
            self.tail.close()
        self.folder.cleanup()

    def write(self, text):
        self.path.write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))

    def append(self, data):
        with self.path.open("ab") as stream:
            stream.write(data)

    def test_initial_skip_appends_and_no_duplicates(self):
        self.write(line("old"))
        self.tail = LogTail(self.path)
        self.assertEqual(self.tail.poll(), [])
        self.append(line("new").encode("utf-16-le"))
        self.assertEqual([x.message for x in self.tail.poll()], ["new"])
        self.assertEqual(self.tail.poll(), [])

    def test_partial_codeunit_surrogate_and_line(self):
        self.write("")
        self.tail = LogTail(self.path, start_at_end=False)
        raw = line("😀 Привет").encode("utf-16-le")
        split = raw.index("😀".encode("utf-16-le")) + 3
        self.append(raw[:split])
        self.assertEqual(self.tail.poll(), [])
        self.append(raw[split:-2])
        self.assertEqual(self.tail.poll(), [])
        self.append(raw[-2:])
        self.assertEqual(self.tail.poll()[0].message, "😀 Привет")

    def test_initial_partial_record_is_skipped(self):
        raw = line("old 😀").encode("utf-16-le")
        self.path.write_bytes(codecs.BOM_UTF16_LE + raw[:51])
        self.tail = LogTail(self.path)
        self.assertEqual(self.tail.poll(), [])
        self.append(raw[51:] + line("new").encode("utf-16-le"))
        self.assertEqual([x.message for x in self.tail.poll()], ["new"])

    def test_truncation_restarts(self):
        self.write(line("old long message"))
        self.tail = LogTail(self.path)
        self.tail.poll()
        self.write(line("new"))
        self.assertEqual([x.message for x in self.tail.poll()], ["new"])

    def test_truncate_and_regrow_between_polls(self):
        self.write(line("old"))
        self.tail = LogTail(self.path)
        self.tail.poll()
        self.write(line("new much longer record"))
        self.assertEqual([x.message for x in self.tail.poll()], ["new much longer record"])

    def test_replacement_allows_rename_with_open_reader(self):
        self.write(line("old"))
        self.tail = LogTail(self.path)
        self.tail.poll()
        self.path.rename(self.path.with_suffix(".old"))
        self.write(line("replacement"))
        self.assertEqual([x.message for x in self.tail.poll()], ["replacement"])

    def test_missing_then_created(self):
        self.tail = LogTail(self.path)
        self.assertEqual(self.tail.poll(), [])
        self.write(line("created"))
        self.assertEqual([x.message for x in self.tail.poll()], ["created"])

    def test_recent_limit_and_partial_line(self):
        self.write(line("one") + line("two") + line("three") + line("partial").rstrip())
        self.assertEqual([x.message for x in read_recent_chat(self.path, 2)], ["two", "three"])
        self.assertEqual(read_recent_chat(self.path, 0), [])

    def test_link_split_during_utf16_write_is_decoded_once_for_live_and_recent(self):
        self.write("")
        self.tail = LogTail(self.path, start_at_end=False)
        raw = line("Продам " + item_link("Necklace of Freya") + " 10kk").encode("utf-16-le")
        split = raw.index("Freya".encode("utf-16-le")) + 3
        self.append(raw[:split])
        self.assertEqual(self.tail.poll(), [])
        self.assertEqual(read_recent_chat(self.path), [])
        self.append(raw[split:])
        live = self.tail.poll()
        self.assertEqual(live, [ChatRecord(
            "16:23:42", "Player", "Продам [Necklace of Freya] 10kk", 25,
            ("[Necklace of Freya]",),
            link_spans=(ChatLinkSpan("item", 7, 7 + len("[Necklace of Freya]")),))])
        self.assertEqual(read_recent_chat(self.path), live)
        self.assertEqual(self.tail.poll(), [])


class OllamaTests(unittest.TestCase):
    def client(self, responses):
        client = OllamaClient()
        client._opener = Mock()
        client._opener.open.side_effect = [io.BytesIO(json.dumps(value).encode()) for value in responses]
        return client

    def test_loopback_only_and_local_models(self):
        for url in ("https://ollama.com", "http://192.168.1.2:11434", "http://127.0.0.1.evil.test", "http://user@localhost", "http://localhost/api", "http://localhost?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaClient(base_url=url)
        for model in ("qwen2.5:cloud", "something-cloud", "https://host/model", "hf.co/model/name"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                OllamaClient(model=model)
        OllamaClient(base_url="http://[::1]:11434")

    def test_localhost_is_pinned_to_numeric_loopback_before_requesting_chat(self):
        for url, expected in (("http://localhost:11435/", "http://127.0.0.1:11435"),
                              ("http://LOCALHOST", "http://127.0.0.1")):
            with self.subTest(url=url):
                client = OllamaClient(base_url=url)
                self.assertEqual(client.base_url, expected)
                client._opener = Mock()
                client._opener.open.return_value = io.BytesIO(b'{"version":"test"}')
                self.assertTrue(client.health())
                self.assertEqual(client._opener.open.call_args.args[0].full_url, expected + "/api/version")

    def test_oversized_local_response_is_rejected_before_json_parsing(self):
        client = self.client([])
        client._opener.open.side_effect = None
        response = io.BytesIO(b'{"private":"' + b'x' * 200 + b'"}')
        client._opener.open.return_value = response
        with patch.object(translator_core, "MAX_OLLAMA_RESPONSE_BYTES", 64), \
             patch.object(translator_core.json, "loads") as parse_response:
            with self.assertRaisesRegex(OllamaError, "too large") as caught:
                client.health()
        parse_response.assert_not_called()
        self.assertNotIn("private", str(caught.exception))

    def test_response_at_the_limit_is_accepted(self):
        body = b'{"version":"test"}'
        client = self.client([])
        client._opener.open.side_effect = None
        client._opener.open.return_value = io.BytesIO(body)
        with patch.object(translator_core, "MAX_OLLAMA_RESPONSE_BYTES", len(body)):
            self.assertTrue(client.health())

    def test_oversized_visible_and_raw_messages_never_reach_the_local_model(self):
        client = self.client([])
        for text in ("x" * (translator_core.MAX_CHAT_CHARACTERS + 1),
                     "x" * (translator_core.MAX_CHAT_RECORD_CHARACTERS + 1)):
            for method in (client.translate, client.detect_language):
                with self.subTest(size=len(text), method=method.__name__), self.assertRaises(ValueError):
                    method(text)
        client._opener.open.assert_not_called()

    def test_long_link_metadata_does_not_count_as_visible_chat(self):
        client = self.client([])
        link = item_link("Sword").replace("\tColor=0", "\tExtra=" + "x" * 4200 + " \tColor=0")
        self.assertEqual(client.translate("WTS " + link), "WTS [Sword]")
        client._opener.open.assert_not_called()

    def test_payload_uses_only_user_text_and_no_thinking(self):
        client = self.client([{}, {"message": {"content": "Hello!"}}])
        self.assertEqual(client.translate("Привет!"), "Hello!")
        req = client._opener.open.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(payload["model"], "qwen3.5:9b-q4_K_M")
        self.assertEqual(client._opener.open.call_args.kwargs["timeout"], 180)
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "Привет!"})
        self.assertIn("untrusted", payload["messages"][0]["content"])
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], 2048)

    def test_cloud_backed_alias_rejected(self):
        client = self.client([{"remote_host": "https://ollama.com"}])
        with self.assertRaises(OllamaError):
            client.translate("Привет")
        self.assertEqual(client._opener.open.call_count, 1)

    def test_game_vocabulary_helps_without_rewriting_chat_or_prices(self):
        client = self.client([{}, {"message": {"content": "Buying B-grade shots for 15kk."}}])
        source = "Куплю соски Б грейда за 15kk."
        client.translate(source)
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], source)
        self.assertIn("соски = shots", payload["messages"][0]["content"])
        self.assertIn("куплю = buying", payload["messages"][0]["content"])
        self.assertNotIn("catacombs", payload["messages"][0]["content"])

    def test_game_vocabulary_does_not_leak_into_unrelated_chat_or_other_targets(self):
        for source, target in (("Do not attack yet.", "English"),
                               ("Купаться и качели", "English"),
                               ("Куплю соски", "Spanish")):
            with self.subTest(source=source, target=target):
                client = self.client([{}, {"message": {"content": "Translation"}}])
                client.target_language = target
                client.translate(source)
                payload = json.loads(client._opener.open.call_args.args[0].data)
                self.assertNotIn("vocabulary for this message", payload["messages"][0]["content"])

    def test_linked_item_names_do_not_trigger_game_vocabulary(self):
        client = self.client([{}, {"message": {"content": "Selling [[L2ITEM0]] 10kk"}}])
        self.assertEqual(client.translate("Selling [Соски] 10kk", item_links=("[Соски]",)), "Selling [Соски] 10kk")
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertNotIn("vocabulary for this message", payload["messages"][0]["content"])

    def test_leading_trade_abbreviation_never_reaches_the_model(self):
        for prefix in ("WTB ", "wts: ", "WTS/WTB "):
            with self.subTest(prefix=prefix):
                client = self.client([{}, {"message": {"content": "B-grade shots, price?"}}])
                self.assertEqual(client.translate(prefix + "соски Б грейда, цена?"), prefix + "B-grade shots, price?")
                payload = json.loads(client._opener.open.call_args.args[0].data)
                self.assertEqual(payload["messages"][1]["content"], "соски Б грейда, цена?")

    def test_acronym_adjacent_to_chinese_is_preserved(self):
        client = self.client([{}, {"message": {"content": "How much is this?"}}])
        self.assertEqual(client.translate("WTS这个多少钱？"), "WTS How much is this?")

    def test_item_price_and_acronym_only_chat_needs_no_translation_in_any_language(self):
        for target in ("English", "Russian", "Spanish", "Chinese"):
            client = OllamaClient(target_language=target)
            client._request = Mock()
            source = "WTS [Sword] +6 10kk; WTB [Shield] 5kk"
            self.assertEqual(client.translate(source, item_links=("[Sword]", "[Shield]")), source)
            self.assertEqual(client.translate("WTB WTS WTT 10kk"), "WTB WTS WTT 10kk")
            for price in ("WTS -10kk", "WTB-10kk", "WTS:10kk", "WTS[Sword]+6 10kk"):
                self.assertEqual(client.translate(price, item_links=("[Sword]",)), price)
            client._request.assert_not_called()

    def test_inline_acronyms_preserve_exact_case_and_item_names_remain_whole(self):
        client = self.client([{}, {"message": {"content": "Send [[L2ITEM0]] to WTSeller about [[L2ITEM1]]."}}])
        source = "Send pM to WTSeller about [WTS Sword]."
        self.assertEqual(client.translate(source, item_links=("[WTS Sword]",)), source)
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "Send [[L2ITEM0]] to WTSeller about [[L2ITEM1]].")

    def test_changed_or_missing_inline_acronym_uses_source_for_recovery(self):
        for output in ("Can you WTS a sword?", "Can you buy a sword?", "Can you [[L2ITEM0]] [[L2ITEM0]] a sword?"):
            client = self.client([{}, {"message": {"content": output}},
                                  {"message": {"content": '{"part0":"Can you","part1":"a sword"}'}}])
            self.assertEqual(client.translate("Can you WTB a sword?"), "Can you WTB a sword?")
            self.assertEqual(client._opener.open.call_count, 3)

    def test_acronym_placeholder_does_not_collide_with_literal_chat_text(self):
        client = self.client([{}, {"message": {"content": "Keep L2ITEM0; send [[_L2ITEM0]] later."}}])
        self.assertEqual(client.translate("Keep L2ITEM0; send PM later."), "Keep L2ITEM0; send PM later.")

    def test_restored_acronym_does_not_join_a_neighboring_word(self):
        client = self.client([{}, {"message": {"content": "Please [[L2ITEM0]]me later."}}])
        self.assertEqual(client.translate("Please PM me later."), "Please PM me later.")

    def test_urls_with_acronym_paths_are_protected_as_whole_urls(self):
        client = self.client([{}, {"message": {"content": "See [[L2ITEM0]] for details."}}])
        source = "See https://example.com/WTB for details."
        self.assertEqual(client.translate(source), source)
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "See [[L2ITEM0]] for details.")

    def test_models_and_health(self):
        client = self.client([{"models": [{"name": "qwen2.5:3b"}, {"name": "x:cloud"}, {"name": "alias", "remote_model": "remote"}]}, {"version": "0.1"}])
        self.assertEqual(client.list_models(), ["qwen2.5:3b"])
        self.assertTrue(client.health())

    def test_empty_translation_is_error(self):
        client = self.client([{}, {"message": {"content": ""}}])
        with self.assertRaises(OllamaError):
            client.translate("Привет")

    def test_truncated_response_is_rejected_even_if_text_looks_valid(self):
        client = self.client([{}, {"message": {"content": "Hello"}, "done_reason": "length"}])
        with self.assertRaisesRegex(OllamaError, "incomplete translation"):
            client.translate("Привет, как дела?")

    def test_reasoning_tags_are_never_displayed(self):
        for content in ("<think>I should translate this", "reasoning</think>Hello", "<THINK>reason</THINK>Hello"):
            with self.subTest(content=content):
                client = self.client([{}, {"message": {"content": content}, "done_reason": "stop"}])
                with self.assertRaisesRegex(OllamaError, "reasoning"):
                    client.translate("Привет")

    def test_complete_expanded_translation_is_allowed(self):
        translated = "Looking for a healer to join our party."
        client = self.client([{}, {"message": {"content": translated}, "done_reason": "stop"}])
        self.assertEqual(client.translate("Ищу хила"), translated)

    def test_raw_link_attributes_never_reach_model_and_title_is_restored(self):
        client = self.client([{}, {"message": {"content": "Selling [[L2ITEM0]] for 10kk."}}])
        translated = client.translate("Продам " + item_link() + " за 10kk.")
        self.assertEqual(translated, "Selling [Necklace of Frintezza] for 10kk.")
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "Продам [[L2ITEM0]] за 10kk.")
        self.assertNotIn("ClassID", json.dumps(payload))

    def test_each_link_occurrence_is_restored_even_when_model_reorders_it(self):
        client = self.client([{}, {"message": {
            "content": "Trade [[L2ITEM1]] for [[L2ITEM0]] or [[L2ITEM2]]."}}])
        translated = client.translate(
            "Обмен [Doom Plate Armor] на [Dynasty Halberd] или [Doom Plate Armor].",
            item_links=("[Doom Plate Armor]", "[Dynasty Halberd]"))
        self.assertEqual(translated,
                         "Trade [Dynasty Halberd] for [Doom Plate Armor] or [Doom Plate Armor].")
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"],
                         "Обмен [[L2ITEM0]] на [[L2ITEM1]] или [[L2ITEM2]].")

    def test_literal_token_in_chat_does_not_collide_with_protected_item(self):
        for literal in ("[[L2ITEM0]]", "l2item0"):
            with self.subTest(literal=literal):
                client = self.client([{}, {"message": {
                    "content": f"Keep {literal}, selling [[_L2ITEM0]]."}}])
                translated = client.translate(
                    f"Оставь {literal}, продам [Sword].", item_links=("[Sword]",))
                self.assertEqual(translated, f"Keep {literal}, selling [Sword].")
                payload = json.loads(client._opener.open.call_args.args[0].data)
                self.assertEqual(payload["messages"][1]["content"],
                                 f"Оставь {literal}, продам [[_L2ITEM0]].")

    def test_model_changes_to_token_case_or_brackets_still_restore_exact_title(self):
        for content in ("Selling l2item0", "Selling [L2ITEM0]", "Selling [[l2item0]]"):
            with self.subTest(content=content):
                client = self.client([{}, {"message": {"content": content}}])
                self.assertEqual(client.translate(
                    "Продам [Necklace of Frintezza]", item_links=("[Necklace of Frintezza]",)),
                    "Selling [Necklace of Frintezza]")
                self.assertEqual(client._opener.open.call_count, 2)

    def test_model_omission_duplication_or_unknown_item_token_is_recovered_from_source(self):
        for content in ("Selling a necklace.", "Selling [[L2ITEM0]] and [[L2ITEM0]].",
                        "Selling l2item0 and [L2ITEM0].", "Selling [[L2ITEM1]].",
                        "Selling [[L2ITEM0]] and [[L2ITEM1]]."):
            with self.subTest(content=content):
                client = self.client([{}, {"message": {"content": content}},
                                      {"message": {"content": '{"part0":"Selling"}'}}])
                self.assertEqual(client.translate("Продам [Necklace of Freya].",
                                                 item_links=("[Necklace of Freya]",)),
                                 "Selling [Necklace of Freya].")
                self.assertEqual(client._opener.open.call_count, 3)

    def test_redirects_are_disabled(self):
        client = OllamaClient()
        handler = next(h for h in client._opener.handlers if isinstance(h, request.HTTPRedirectHandler))
        self.assertIsNone(handler.redirect_request(None, None, 302, "Moved", {}, "https://external.example/"))


if __name__ == "__main__":
    unittest.main()
