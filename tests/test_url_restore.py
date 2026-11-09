"""Pure URL-span protection checks; source bytes never pass through a model."""
import re
import unittest

from translator_core import _PRICE, _protect_item_links, _url_matches


class SourceUrlSpanTests(unittest.TestCase):
    def protected(self, source, labels=()):
        masked, mapping = _protect_item_links(source, labels)
        if mapping:
            pattern = re.compile("|".join(re.escape(marker) for marker in mapping))
            restored = pattern.sub(lambda match: mapping[match.group()], masked)
        else:
            restored = masked
        self.assertEqual(restored, source)
        return masked, mapping

    def test_unicode_host_is_preserved_without_normalization(self):
        url = "twіtch.tv/тест"
        masked, mapping = self.protected("Смотри " + url)
        self.assertEqual(list(mapping.values()), [url])
        self.assertNotIn("twіtch", masked)
        self.assertNotIn("twitch", masked)
        decomposed = "cafe\u0301.example/path"
        self.assertEqual(_protect_item_links(decomposed, ()), (decomposed, {}))

    def test_port_path_query_and_fragment_are_one_exact_span(self):
        url = "Exemplo.com:8080/A%20B?q=Ol%C3%A1&n=2#Part_3"
        masked, mapping = self.protected("Vá " + url + " agora")
        self.assertEqual(list(mapping.values()), [url])
        self.assertNotIn("8080", masked)
        self.assertNotIn("Part_3", masked)

    def test_explicit_and_bare_source_forms_are_preserved_as_written(self):
        for url in ("https://exemplo.com/original", "http://exemplo.com/original",
                    "www.exemplo.com/original", "exemplo.com/original", "Kick. com /original"):
            with self.subTest(url=url):
                masked, mapping = self.protected("See " + url)
                self.assertEqual(list(mapping.values()), [url])
                self.assertEqual(len(mapping), 1)
                self.assertNotIn(url, masked)

    def test_wrapped_urls_preserve_all_delimiters(self):
        for quote in ('"', "'", '`'):
            for url in ("source.com/path", "https://source.com/path"):
                with self.subTest(quote=quote, url=url):
                    masked, mapping = self.protected("See " + quote + url + quote)
                    self.assertEqual(len(mapping), 1)
                    self.assertNotIn("source.com", masked)

    def test_multiple_mixed_and_repeated_occurrences_are_all_protected(self):
        cases = (
            ("one.com/a two.com/b", ["one.com/a", "two.com/b"]),
            ("one.com/a https://two.com/b", ["one.com/a", "https://two.com/b"]),
            ("www.one.com/a two.com/b", ["www.one.com/a", "two.com/b"]),
            ("one.com/a one.com/a", ["one.com/a", "one.com/a"]),
            ("one.com/a http://one.com/a", ["one.com/a", "http://one.com/a"]),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                masked, mapping = self.protected(source)
                self.assertEqual(list(mapping.values()), expected)
                self.assertEqual(len(mapping), len(expected))
                for marker in mapping:
                    self.assertEqual(masked.count(marker), 1)

    def test_source_terminal_punctuation_is_never_trimmed_or_guessed(self):
        for terminal in ".,;:!?)]}":
            with self.subTest(terminal=terminal):
                url = "source.com/path" + terminal
                masked, mapping = self.protected("See " + url)
                self.assertEqual(list(mapping.values()), [url])
                self.assertNotIn("source.com", masked)

    def test_balanced_parentheses_round_trip_without_reinterpretation(self):
        for source in ("See source.com/a(b)", "See (source.com/path)", "See source.com/a(b)/c"):
            with self.subTest(source=source):
                masked, mapping = self.protected(source)
                self.assertEqual(len(mapping), 1)
                self.assertNotIn("source.com", masked)

    def test_email_and_url_suffixes_are_not_partial_candidates(self):
        for source in ("user@sub.example.com/path", "user@example.com/path",
                       "@sub.example.com/path", "prefix/sub.example.com/path",
                       ".sub.example.com/path", "user@sub. example.com/path",
                       "user @ sub.example.com/path"):
            with self.subTest(source=source):
                self.assertEqual(list(_url_matches(source)), [])
                self.assertEqual(self.protected(source), (source, {}))

    def test_linked_labels_are_atomic_and_do_not_expose_nested_urls(self):
        labels = ("[See item.example.com/path]", "[https://item.example.com/path]")
        source = labels[0] + " " + labels[1] + " source.com/original"
        masked, mapping = self.protected(source, labels)
        self.assertEqual(list(mapping.values()), [*labels, "source.com/original"])
        self.assertNotIn("item.example.com", masked)

    def test_repeated_and_overlapping_labels_retain_all_occurrences(self):
        labels = ("[item.com/a]", "[item.com/a] extra", "")
        source = "[item.com/a] extra [item.com/a] source.com/path"
        masked, mapping = self.protected(source, labels)
        self.assertEqual(list(mapping.values()), ["[item.com/a] extra", "[item.com/a]", "source.com/path"])
        self.assertEqual(len(mapping), 3)

    def test_prices_filenames_and_domains_without_slash_are_unchanged(self):
        for source in ("1.5kk/each", "10kk/each", "1,5kk/each", "config.ini",
                       "exemplo.com", "2.5/each", "No link here"):
            with self.subTest(source=source):
                self.assertEqual(self.protected(source), (source, {}))

    def test_numeric_url_path_is_excluded_from_price_comparison(self):
        masked, mapping = self.protected("See source.com/10kk for 2kk each")
        self.assertEqual(list(mapping.values()), ["source.com/10kk"])
        self.assertEqual(_PRICE.findall(masked), ["2kk"])

    def test_surrounding_negation_prices_and_spacing_are_not_rewritten(self):
        source = "\tWTB  [Club] for 1.5kk; no discounts\nsource.com/path  !\n"
        masked, mapping = self.protected(source, ("[Club]",))
        self.assertEqual(list(mapping.values()), ["WTB", "[Club]", "source.com/path"])
        self.assertIn("for 1.5kk; no discounts\n", masked)
        self.assertTrue(masked.startswith("\t"))
        self.assertTrue(masked.endswith("  !\n"))


if __name__ == "__main__":
    unittest.main()
