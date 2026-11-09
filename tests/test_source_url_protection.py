"""Independent source-URL regressions; every model response is synthetic."""
import json
import re
import unittest
from unittest.mock import Mock

from translator_core import (
    OllamaClient, OllamaError, _language_text, _protect_item_links, _url_matches,
)


_MARKERS = re.compile(r"\[\[_*L2ITEM\d+\]\]")


def mutate_markers(text, mutation):
    first, second = _MARKERS.findall(text)
    if mutation == "missing":
        return text.replace(second, "", 1)
    if mutation == "duplicate":
        return text.replace(second, first, 1)
    if mutation == "swap":
        return text.replace(first, "<SWAP>", 1).replace(second, first, 1).replace("<SWAP>", second, 1)
    raise AssertionError("Unknown synthetic mutation")


class SourceUrlRecognitionTests(unittest.TestCase):
    def urls(self, source):
        matches = list(_url_matches(source))
        for match in matches:
            self.assertEqual(match.group(), source[match.start():match.end()])
        return [match.group() for match in matches]

    def test_reported_spaced_and_contiguous_links_are_both_recognized(self):
        source = "Kick. com /examplechannel - Canada - NA Stream- - Twitch.tv/examplechannel"
        self.assertEqual(self.urls(source), ["Kick. com /examplechannel", "Twitch.tv/examplechannel"])

    def test_horizontal_separator_spacing_is_preserved_exactly(self):
        for url in ("Kick. com /stream", "Kick .com/stream", "Kick . com /stream",
                    "kick.com /stream", "kick\t.\tcom\t/stream", "Sub . kick . com /stream"):
            with self.subTest(url=url):
                self.assertEqual(self.urls("Visit " + url + " now"), [url])

    def test_common_spaced_suffixes_are_supported(self):
        for suffix in ("com", "tv", "gg", "net", "org", "io", "live", "me", "co"):
            with self.subTest(suffix=suffix):
                url = "example. " + suffix + " /path"
                self.assertEqual(self.urls(url), [url])

    def test_spaced_prose_and_cross_line_addresses_are_not_links(self):
        for source in ("no. more /time", "kick.\ncom/path", "kick.com\n/path",
                       "kick. com / path"):
            with self.subTest(source=source):
                self.assertEqual(self.urls(source), [])

    def test_contiguous_root_url_does_not_absorb_following_word(self):
        self.assertEqual(self.urls("kick.com/ path"), ["kick.com/"])

    def test_unicode_case_port_query_and_fragment_are_not_normalized(self):
        for url in ("twіtch.tv/тест", "例子.公司/路径", "пример.рф/путь",
                    "Exemplo.com:8080/A%20B?q=Ol%C3%A1&n=2#Part_3"):
            with self.subTest(url=url):
                self.assertEqual(self.urls("See " + url + " now"), [url])

    def test_no_email_host_or_suffix_is_mistaken_for_a_link(self):
        for source in ("user@sub.example.com/path", "user@example.com/path",
                       "user@www.example.com/path", "user@sub. example.com/path",
                       "user @ sub.example.com/path", "user @ sub . example . com /path",
                       "@sub.example.com/path", ".sub.example.com/path",
                       "prefix/sub.example.com/path"):
            with self.subTest(source=source):
                self.assertEqual(self.urls(source), [])

    def test_prices_filenames_and_bare_domain_without_path_are_not_links(self):
        for source in ("1.5kk/each", "1,5kk/each", "2.5/each", "10kk/each",
                       "config.ini", "example.com", "example. com"):
            with self.subTest(source=source):
                self.assertEqual(self.urls(source), [])

    def test_each_repeated_occurrence_has_its_own_marker(self):
        source = "See example.com/path and example.com/path"
        masked, mapping = _protect_item_links(source, ())
        self.assertEqual(list(mapping.values()), ["example.com/path", "example.com/path"])
        self.assertEqual(len(set(mapping)), 2)
        for marker in mapping:
            self.assertEqual(masked.count(marker), 1)
        self.assertNotIn("example.com", masked)

    def test_whole_url_wins_over_embedded_acronyms_and_prices(self):
        for url in ("pvp.example.com/path", "example.com/WTB/10kk", "example.com/WTB"):
            with self.subTest(url=url):
                masked, mapping = _protect_item_links("See " + url, (), protect_prices=True)
                self.assertEqual(list(mapping.values()), [url])
                self.assertEqual(masked, "See " + next(iter(mapping)))

    def test_linked_item_title_wins_over_url_inside_its_label(self):
        label = "[Club example.com/10kk]"
        source = label + " see Kick. com /trade"
        masked, mapping = _protect_item_links(source, (label,))
        self.assertEqual(list(mapping.values()), [label, "Kick. com /trade"])
        self.assertNotIn("example.com", masked)
        self.assertNotIn("Kick", masked)

    def test_literal_marker_collision_does_not_replace_user_text(self):
        source = "Literal [[L2ITEM0]] see example.com/path"
        masked, mapping = _protect_item_links(source, ())
        self.assertIn("[[L2ITEM0]]", masked)
        self.assertNotIn("[[L2ITEM0]]", mapping)
        self.assertEqual(list(mapping.values()), ["example.com/path"])

    def test_language_detection_excludes_both_url_forms(self):
        self.assertEqual(_language_text("Привет Kick. com /english Twitch.tv/russian", ()), "Привет")
        self.assertEqual(_language_text("Kick. com /english Twitch.tv/russian", ()), "")


class SourceUrlPipelineTests(unittest.TestCase):
    def client(self, rewrite=lambda value: value, *, target="English", mutation=None,
               recovery_rewrite=None):
        client = OllamaClient(target_language=target)
        client._verify_local_model = Mock()

        def respond(_path, payload):
            value = payload["messages"][-1]["content"]
            if "format" in payload:
                rewrite_fragment = recovery_rewrite or rewrite
                output = json.dumps({key: rewrite_fragment(fragment)
                                     for key, fragment in json.loads(value).items()}, ensure_ascii=False)
            else:
                output = rewrite(value)
                if mutation:
                    output = mutate_markers(output, mutation)
            return {"message": {"content": output}, "done_reason": "stop"}

        client._request = Mock(side_effect=respond)
        return client

    def inputs(self, client):
        return [call.args[1]["messages"][-1]["content"] for call in client._request.call_args_list]

    def test_reported_message_hides_and_restores_both_addresses(self):
        source = "Kick. com /examplechannel - Canada - NA Stream- - Twitch.tv/examplechannel"
        client = self.client()
        self.assertEqual(client.translate(source), source)
        self.assertEqual(client._request.call_count, 1)
        sent = self.inputs(client)[0]
        self.assertEqual(len(_MARKERS.findall(sent)), 2)
        for hidden in ("Kick", "Twitch", "examplechannel"):
            self.assertNotIn(hidden, sent)
        self.assertIn("Canada", sent)

    def test_url_only_messages_bypass_inference_and_remain_exact(self):
        for source in ("pvp.example.com/path", "WTS pvp.example.com/path",
                       "Kick. com /stream Twitch.tv/channel"):
            with self.subTest(source=source):
                client = self.client()
                self.assertEqual(client.translate(source), source)
                client._request.assert_not_called()

    def test_leading_acronym_does_not_split_url_host(self):
        for prefix in ("", "WTS ", "WTB "):
            for url in ("pvp.example.com/path", "PVP . example.com /path"):
                with self.subTest(prefix=prefix, url=url):
                    source = prefix + url + " смотри"
                    client = self.client(lambda value: value.replace("смотри", "watch"))
                    self.assertEqual(client.translate(source), prefix + url + " watch")
                    self.assertEqual(client._request.call_count, 1)
                    self.assertNotIn("example.com", self.inputs(client)[0])

    def test_negation_direction_items_and_real_prices_survive_with_two_urls(self):
        source = "WTB Не продаю [Club] за 10kk, смотри Kick. com /a и Twitch.tv/b"
        expected = "WTB I am not selling [Club] for 10kk, see Kick. com /a and Twitch.tv/b"
        rewrite = lambda value: (value.replace("Не продаю", "I am not selling")
                                .replace("за", "for").replace("смотри", "see").replace(" и ", " and "))
        client = self.client(rewrite)
        self.assertEqual(client.translate(source, item_links=("[Club]",)), expected)
        self.assertEqual(client._request.call_count, 1)
        self.assertIn("10kk", self.inputs(client)[0])
        for hidden in ("[Club]", "Kick", "Twitch"):
            self.assertNotIn(hidden, self.inputs(client)[0])

    def test_numeric_url_path_is_not_treated_as_a_real_price(self):
        source = "Смотри pvp.example.com/WTB/10kk за 2kk"
        client = self.client(lambda value: value.replace("Смотри", "See").replace("за", "for"))
        self.assertEqual(client.translate(source), "See pvp.example.com/WTB/10kk for 2kk")
        self.assertEqual(client._request.call_count, 1)
        self.assertNotIn("10kk", self.inputs(client)[0])
        self.assertIn("2kk", self.inputs(client)[0])

    def test_punctuation_and_wrappers_round_trip_without_guessing(self):
        for url in ("example.com/path!", "example.com/path?", "example.com/path.",
                    "example.com/a(b)", "example.com/path]", '"example.com/path"',
                    "'example.com/path'", "`example.com/path`", "<example.com/path>"):
            with self.subTest(url=url):
                source = "Смотри " + url + " сейчас"
                client = self.client(lambda value: value.replace("Смотри", "See").replace("сейчас", "now"))
                self.assertEqual(client.translate(source), "See " + url + " now")
                self.assertEqual(client._request.call_count, 1)

    def test_all_outgoing_targets_preserve_spaced_and_bare_urls(self):
        for target, phrase in (("Russian", "Используй только"), ("Portuguese", "Use apenas"),
                               ("Spanish", "Usa solo"), ("Chinese", "只使用")):
            with self.subTest(target=target):
                client = self.client(lambda value: value.replace("Use only", phrase), target=target)
                self.assertEqual(client.translate("Use only Kick. com /a and Twitch.tv/b"),
                                 phrase + " Kick. com /a and Twitch.tv/b")
                self.assertEqual(client._request.call_count, 1)

    def test_missing_duplicate_and_swapped_urls_recover_once_in_source_order(self):
        rewrite = lambda value: value.replace("Não use", "Do not use").replace("use apenas", "use only")
        source = "Não use Kick. com /a; use apenas Twitch.tv/b"
        expected = "Do not use Kick. com /a; use only Twitch.tv/b"
        for mutation in ("missing", "duplicate", "swap"):
            with self.subTest(mutation=mutation):
                client = self.client(rewrite, mutation=mutation)
                self.assertEqual(client.translate(source), expected)
                self.assertEqual(client._request.call_count, 2)
                for sent in self.inputs(client):
                    self.assertNotIn("Kick", sent)
                    self.assertNotIn("Twitch", sent)

    def test_outgoing_missing_duplicate_and_swapped_urls_fail_without_recovery(self):
        for mutation in ("missing", "duplicate", "swap"):
            with self.subTest(mutation=mutation):
                client = self.client(target="Russian", mutation=mutation)
                with self.assertRaises(OllamaError):
                    client.translate("Do not use Kick. com /a; use only Twitch.tv/b")
                self.assertEqual(client._request.call_count, 1)

    def test_recovery_still_rejects_an_invented_real_price(self):
        client = self.client(mutation="missing", recovery_rewrite=lambda value: value + " 9kk")
        with self.assertRaisesRegex(OllamaError, "price"):
            client.translate("Trade at Kick. com /10kk or Twitch.tv/b for 2kk")
        self.assertEqual(client._request.call_count, 2)

    def test_party_numbers_inside_url_do_not_become_party_details(self):
        source = "Нужен хилер [12~19] (8/9), смотри example.com/path/(1/9)"
        client = self.client(lambda value: value.replace("Нужен хилер", "Need healer").replace("смотри", "see"))
        self.assertEqual(client.translate(source, party_details=("[12~19]", "(8/9)")),
                         "Need healer [12~19] (8/9), see example.com/path/(1/9)")
        self.assertEqual(client._request.call_count, 1)
        for hidden in ("[12~19]", "(8/9)", "(1/9)", "example.com"):
            self.assertNotIn(hidden, self.inputs(client)[0])

    def test_mixed_explicit_and_repeated_bare_links_keep_occurrences(self):
        source = "Смотри example.com/a и https://example.org/b и example.com/a"
        client = self.client(lambda value: value.replace("Смотри", "See").replace(" и ", " and "))
        self.assertEqual(client.translate(source), "See example.com/a and https://example.org/b and example.com/a")
        self.assertEqual(client._request.call_count, 1)
        self.assertEqual(len(set(_MARKERS.findall(self.inputs(client)[0]))), 3)


if __name__ == "__main__":
    unittest.main()
