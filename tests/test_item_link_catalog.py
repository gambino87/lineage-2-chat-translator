"""Catalog lookup precedes model protection; all chat fixtures are synthetic."""
import unittest
from unittest.mock import Mock, patch

from translator_core import OllamaClient, decode_item_links, parse_chat_line


def link(title="闪耀刀", class_field="ClassID=217", object_id=1078202847):
    return (f"\x08\tType=1 \tID={object_id} \tColor=0 \tUnderline=0 "
            f"\t{class_field} \tTitle=\x1b{title}\x1b\x08")


def record(message):
    return parse_chat_line(
        "(16:23:42) Log: (Receive)[S_SAY2] SayPacket2ID : 123 Name : Player "
        f"Say Msg:{message} Type:25\r\n")


class ItemLinkCatalogTests(unittest.TestCase):
    def test_reported_chinese_links_use_english_without_model_inference(self):
        cases = [
            ("WTS" + link() + "!!+", "WTS[Shining Knife]!!+"),
            ("WTS " + link("团结之剑", "ClassID=738"), "WTS [Sword of Solidarity]"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                parsed = record(source)
                self.assertEqual(parsed.message, expected)
                for target in ("English", "Chinese", "Russian", "Portuguese"):
                    client = OllamaClient(target_language=target)
                    client._request = Mock(side_effect=AssertionError("No model needed for item listing"))
                    self.assertEqual(client.translate(parsed.message, item_links=parsed.item_links), expected)
                    self.assertEqual(client.translate(source), expected)
                    client._request.assert_not_called()

    def test_object_id_never_selects_the_catalog_entry(self):
        self.assertEqual(decode_item_links(link("团结之剑", "ClassID=738", object_id=217))[0],
                         "[Sword of Solidarity]")
        for fields in ("", "OtherClassID=217", "ClassID=2147483647"):
            with self.subTest(fields=fields):
                self.assertEqual(decode_item_links(link("Unknown custom title", fields, object_id=217)),
                                 ("[Unknown custom title]", ("[Unknown custom title]",)))

    def test_invalid_or_duplicate_class_ids_do_not_guess(self):
        for fields in ("ClassID=-217", "ClassID=0", "ClassID=2147483648", "ClassID=999999999999999",
                       "ClassID=217.0", "ClassID=0xD9", "ClassID=+217", "ClassID=217x",
                       "ClassID=", "ClassID= 217", "ClassID =217", "ClassID=２１７",
                       "ClassID=217 ClassID=738", "ClassID=217 ClassID=217",
                       "ClassID=217 classid=738", "ClassID=217 ClassID=bad"):
            with self.subTest(fields=fields):
                self.assertEqual(decode_item_links(link("Original title", fields)),
                                 ("[Original title]", ("[Original title]",)))

    def test_class_id_inside_title_is_not_metadata(self):
        source = link("Custom ClassID=217", "")
        self.assertEqual(decode_item_links(source),
                         ("[Custom ClassID=217]", ("[Custom ClassID=217]",)))
        for malformed in ("title=\x1bCustom ClassID=217", "Title = Custom ClassID=217",
                          "Title=\x1bCustom ClassID=217"):
            self.assertEqual(decode_item_links("\x08 Type=1 " + malformed + "\x08"),
                             ("[Linked item]", ("[Linked item]",)))

    def test_repeated_and_adjacent_items_keep_occurrence_order(self):
        first, second = link(), link("团结之剑", "ClassID=738")
        for separator in ("", " / "):
            with self.subTest(separator=separator):
                message, labels = decode_item_links(first + separator + second + first)
                self.assertEqual(message,
                                 "[Shining Knife]" + separator + "[Sword of Solidarity][Shining Knife]")
                self.assertEqual(labels, ("[Shining Knife]", "[Sword of Solidarity]"))

    def test_same_source_title_with_different_ids_resolves_each_occurrence(self):
        with patch("translator_core.resolve_item_title", side_effect=lambda item_id, title: f"item {item_id}") as resolver:
            message, labels = decode_item_links(link("same", "ClassID=217") + link("same", "ClassID=738"))
        self.assertEqual(message, "[item 217][item 738]")
        self.assertEqual(labels, ("[item 217]", "[item 738]"))
        self.assertEqual([call.args for call in resolver.call_args_list], [(217, "same"), (738, "same")])

    def test_shared_backspace_does_not_consume_the_next_link(self):
        for first in (link(), "\x08\tType=1 \tClassID=217\x08",
                      "\x08\tType=1 \tClassID=2147483647\x08"):
            with self.subTest(first=first):
                expected_first = "[Linked item]" if "2147483647" in first else "[Shining Knife]"
                source = first[:-1] + link("团结之剑", "ClassID=738") + " 10kk"
                message, labels = decode_item_links(source)
                self.assertEqual(message, expected_first + "[Sword of Solidarity] 10kk")
                self.assertEqual(labels, (expected_first, "[Sword of Solidarity]"))
                for field in ("Type=", "ID=", "ClassID=", "Title=", "\x08", "\x1b"):
                    self.assertNotIn(field, message)

    def test_missing_final_backspace_keeps_following_chat(self):
        self.assertEqual(decode_item_links(link()[:-1] + " for 10kk"),
                         ("[Shining Knife] for 10kk", ("[Shining Knife]",)))

    def test_safe_class_id_can_recover_a_damaged_or_empty_title(self):
        for source in ("\x08\tType=1 \tClassID=217\x08", link(""),
                       "\x08\tType=1 \tClassID=217 \tTitle=\x1bunfinished\x08"):
            with self.subTest(source=source):
                self.assertEqual(decode_item_links(source), ("[Shining Knife]", ("[Shining Knife]",)))

    def test_non_item_link_does_not_use_item_class_id(self):
        message, labels = decode_item_links(link().replace("Type=1", "Type=2"))
        self.assertEqual((message, labels), ("[Linked item]", ("[Linked item]",)))

    def test_unknown_source_title_is_protected_unchanged(self):
        source = "WTB " + link("自定义武器", "ClassID=2147483647") + " +6 10kk"
        parsed = record(source)
        client = OllamaClient()
        client._request = Mock(side_effect=AssertionError("Unknown title must remain protected"))
        self.assertEqual(client.translate(parsed.message, item_links=parsed.item_links),
                         "WTB [自定义武器] +6 10kk")
        client._request.assert_not_called()

    def test_ordinary_brackets_and_plain_metadata_are_not_catalog_links(self):
        for text in ("WTS [闪耀刀]", "ClassID=217 Title=闪耀刀", "[ClassID=217]",
                     "Type=1 ID=738 ClassID=217 Title=闪耀刀"):
            with self.subTest(text=text):
                self.assertEqual(decode_item_links(text), (text, ()))

    def test_known_modifiers_and_external_prices_are_preserved(self):
        self.assertEqual(decode_item_links("WTS " + link("+6 闪耀刀") + " 10kk!!")[0],
                         "WTS [+6 Shining Knife] 10kk!!")

    def test_item_only_listing_does_not_infer_language_from_localized_title(self):
        for source in ("WTS " + link() + " 10kk", "WTS " + link("团结之剑", "ClassID=738")):
            with self.subTest(source=source):
                client = OllamaClient()
                client._request = Mock(side_effect=AssertionError("No language evidence"))
                self.assertIsNone(client.detect_language(source))
                parsed = record(source)
                self.assertIsNone(client.detect_language(parsed.message, item_links=parsed.item_links))
                client._request.assert_not_called()

    def test_catalog_title_never_reaches_translation_or_detection_model(self):
        source = "Продам " + link() + " за 10kk"
        client = OllamaClient()
        client._request = Mock(side_effect=[
            {}, {"message": {"content": "Selling [[L2ITEM0]] for 10kk"}},
            {"message": {"content": '{"language":"Russian"}'}},
        ])
        self.assertEqual(client.translate(source), "Selling [Shining Knife] for 10kk")
        self.assertEqual(client.detect_language(source), "Russian")
        payloads = [call.args[1] for call in client._request.call_args_list if call.args[0] == "/api/chat"]
        self.assertEqual(payloads[0]["messages"][-1]["content"], "Продам [[L2ITEM0]] за 10kk")
        self.assertEqual(payloads[1]["messages"][-1]["content"], "Продам за 10kk")

    def test_model_mutation_recovers_canonical_name_from_source(self):
        client = OllamaClient()
        client._request = Mock(side_effect=[
            {}, {"message": {"content": "Selling [错误名称] for 10kk."}},
            {"message": {"content": '{"part0":"Selling","part1":"for"}'}},
        ])
        self.assertEqual(client.translate("Продам " + link() + " за 10kk."),
                         "Selling [Shining Knife] for 10kk.")
        recovery = client._request.call_args.args[1]["messages"][-1]["content"]
        self.assertNotIn("Shining Knife", recovery)
        self.assertNotIn("闪耀刀", recovery)
        self.assertNotIn("错误名称", recovery)


if __name__ == "__main__":
    unittest.main()
