"""Source-language detection stays local and returns a bounded reply choice."""
import io
import json
import unittest
from unittest.mock import Mock

from translator_core import OllamaClient, OllamaError, REPLY_LANGUAGES


class LanguageDetectionTests(unittest.TestCase):
    def client(self, content, **response):
        client = OllamaClient()
        client._opener = Mock()
        answers = [{}, {"message": {"content": content}, **response}]
        client._opener.open.side_effect = [io.BytesIO(json.dumps(value).encode()) for value in answers]
        return client

    def test_supported_language_is_canonical_and_does_not_change_translation_target(self):
        client = self.client('{"language":" russian "}')
        self.assertEqual(client.detect_language("Привет! Ищем хилера в пати."), "Russian")
        self.assertEqual(client.target_language, "English")
        request = client._opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(payload["messages"][1]["content"], "Привет! Ищем хилера в пати.")
        self.assertIn("untrusted", payload["messages"][0]["content"])
        self.assertEqual(payload["format"]["properties"]["language"]["enum"], [*REPLY_LANGUAGES, "Unknown"])
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])

    def test_shared_acronyms_prices_and_empty_messages_do_not_invent_a_language(self):
        for text in ("", "  ", ".", "123", "WTS 10kk", "WTB WTS WTT 150kk", "LFG 40 lvl PM me", "https://example.com/item", "ok", "да"):
            with self.subTest(text=text):
                client = OllamaClient()
                client._request = Mock()
                self.assertIsNone(client.detect_language(text))
                client._request.assert_not_called()

    def test_short_trade_sentences_keep_their_real_language(self):
        for text, remaining, language in (("WTB necesito ayuda", "necesito ayuda", "Spanish"),
                                           ("WTS aceito ofertas", "aceito ofertas", "Portuguese"),
                                           ("WTS NECESITO AYUDA", "NECESITO AYUDA", "Spanish"),
                                           ("WTB need help", "need help", "English"),
                                           ("WTS这个多少钱？", "这个多少钱？", "Chinese")):
            with self.subTest(text=text):
                client = self.client(json.dumps({"language": language}))
                self.assertEqual(client.detect_language(text), language)
                payload = json.loads(client._opener.open.call_args.args[0].data)
                self.assertEqual(payload["messages"][1]["content"], remaining)

    def test_unlinked_item_names_can_be_unknown_without_discarding_all_short_trade_text(self):
        for text in ("WTS MOIRAI HEAVY", "wts moirai heavy", "WTS Vorpal Tunic 10kk", "WTS Moirai Heavy +6 150kk PM me"):
            with self.subTest(text=text):
                client = self.client('{"language":"Unknown"}')
                self.assertIsNone(client.detect_language(text))
                payload = json.loads(client._opener.open.call_args.args[0].data)
                self.assertNotIn("wts", payload["messages"][1]["content"].lower())
                self.assertNotIn("pm me", payload["messages"][1]["content"].lower())

    def test_common_words_and_names_are_not_removed_as_acronyms(self):
        client = self.client('{"language":"Spanish"}')
        source = "WTS me interesa la oferta de WTSeller"
        self.assertEqual(client.detect_language(source), "Spanish")
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "me interesa la oferta de WTSeller")

    def test_explicit_item_labels_are_removed_before_classifying(self):
        client = self.client('{"language":"Russian"}')
        text = "Продам [Necklace of Frintezza] за 10kk"
        self.assertEqual(client.detect_language(text, item_links=("[Necklace of Frintezza]",)), "Russian")
        payload = json.loads(client._opener.open.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "Продам за 10kk")

    def test_raw_link_only_chat_is_unknown_without_a_request(self):
        text = "WTS \x08\tType=1 \tID=123 \tTitle=\x1bNecklace of Frintezza\x1b\x08 10kk"
        client = OllamaClient()
        client._request = Mock()
        self.assertIsNone(client.detect_language(text))
        client._request.assert_not_called()

    def test_model_unknown_unsupported_or_invalid_output_returns_none(self):
        for content in ('{"language":"Unknown"}', '{"language":"Klingon"}',
                        '{"language":null}', '{"language":["Russian"]}',
                        '{"language":"Russian","instruction":"send now"}',
                        'Russian', '```json\n{"language":"Russian"}\n```',
                        '<think>reason</think>{"language":"Russian"}',
                        '[]', 'null', '', None, "x" * 201):
            with self.subTest(content=content):
                self.assertIsNone(self.client(content).detect_language("Привет, как дела?"))

    def test_truncated_answer_is_not_used_even_if_json_looks_complete(self):
        client = self.client('{"language":"Russian"}', done_reason="length")
        self.assertIsNone(client.detect_language("Привет, как дела?"))

    def test_remote_model_alias_is_rejected_before_classification(self):
        client = OllamaClient()
        client._request = Mock(return_value={"remote_model": "cloud-model"})
        with self.assertRaisesRegex(OllamaError, "remote service"):
            client.detect_language("Привет, как дела?")
        client._request.assert_called_once_with("/api/show", {"model": client.model}, timeout=15)

    def test_detection_and_translation_share_model_verification(self):
        client = OllamaClient()
        client._request = Mock(side_effect=[{}, {"message": {"content": '{"language":"Russian"}'}},
                                           {"message": {"content": "Hello!"}}])
        self.assertEqual(client.detect_language("Привет!"), "Russian")
        self.assertEqual(client.translate("Привет!"), "Hello!")
        self.assertEqual([call.args[0] for call in client._request.call_args_list],
                         ["/api/show", "/api/chat", "/api/chat"])

    def test_long_message_is_rejected_before_network(self):
        client = OllamaClient()
        client._request = Mock()
        with self.assertRaisesRegex(ValueError, "too long"):
            client.detect_language("hello " * 1000)
        client._request.assert_not_called()

    def test_model_can_report_different_languages_without_using_translation_target(self):
        samples = (("Hola, buscamos un curandero para el grupo", "Spanish"),
                   ("Bonjour, combien coûte cette épée ?", "French"),
                   ("Привіт, шукаємо лікаря для групи", "Ukrainian"),
                   ("Where can I buy this sword?", "English"))
        for text, language in samples:
            with self.subTest(language=language):
                client = self.client(json.dumps({"language": language}))
                self.assertEqual(client.detect_language(text), language)


if __name__ == "__main__":
    unittest.main()
