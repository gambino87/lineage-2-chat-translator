"""Exact link provenance across mocked translation, normalization and recovery."""
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

from chat_links import (ChatLinkSpan, LinkedText, mark_link_boundaries,
                        normalize_display, restore_link_boundaries)
from translator_core import (ChatRecord, MAX_CHAT_CHARACTERS, OllamaClient, OllamaError,
                             TranslationInvariantError, decode_chat_links,
                             decode_chat_links_with_spans, parse_chat_line)


def raw_link(title, kind=10):
    return f"\x08\tType={kind} \tClassID=217 \tTitle=\x1b{title}\x1b\x08"


def record(raw):
    return parse_chat_line(f"(12:34:56) Log: (Receive)[S_SAY2] SayPacket2ID : 1 Name : Synthetic Say Msg:{raw} Type:1")


def colored(text):
    return [(span.kind, text[span.start:span.end]) for span in text.link_spans]


class SourceLinkTests(unittest.TestCase):
    def test_plain_duplicate_of_real_item_is_not_linked(self):
        value = record('[Shining Knife] / ' + raw_link('ignored', 1) + ' / [notice]')
        self.assertEqual(value.message, '[Shining Knife] / [Shining Knife] / [notice]')
        self.assertEqual(value.link_spans, (ChatLinkSpan('item', 18, 33),))

    def test_mixed_shared_delimiters_and_removed_controls_have_exact_offsets(self):
        party = 'Ищем хилера [12~19] (8/9)'
        source = '\x01prefix ' + raw_link(party)[:-1] + raw_link('ignored', 1) + '\x02 end'
        text, items, details, spans = decode_chat_links_with_spans(source)
        self.assertEqual(text, 'prefix ' + party + '[Shining Knife] end')
        self.assertEqual(colored(LinkedText(text, spans)), [('party', party), ('item', '[Shining Knife]')])
        self.assertEqual(decode_chat_links(source), (text, items, details))
        self.assertEqual(details, ('[12~19]', '(8/9)'))

    def test_plain_brackets_and_unknown_link_types_do_not_gain_known_styles(self):
        self.assertEqual(record('[Sword] [12~19] (8/9)').link_spans, ())
        self.assertEqual(record(raw_link('unknown', 2)).link_spans, ())
        malformed = record('\x08\tType=10 \tClassID=217\x08')
        self.assertEqual(colored(LinkedText(malformed.message, malformed.link_spans)), [('party', '[Party link]')])

    def test_party_without_details_and_repeated_links_remain_separate(self):
        value = record(raw_link('Нужен хилер') + ' / ' + raw_link('Нужен хилер'))
        self.assertEqual(value.party_details, ())
        self.assertEqual(colored(LinkedText(value.message, value.link_spans)),
                         [('party', 'Нужен хилер'), ('party', 'Нужен хилер')])


class RichTranslationTests(unittest.TestCase):
    def client(self):
        client = OllamaClient()
        client._verify_local_model = Mock()
        def respond(path, payload):
            body = payload['messages'][1]['content']
            if payload.get('format', {}).get('type') == 'object':
                content = json.dumps({key: value.replace('Ищем хилера', 'Looking for a healer')
                                      for key, value in json.loads(body).items()})
            else:
                content = body.replace('Ищем хилера', 'Looking for a healer').replace('Продам', 'Selling')
            return {'message': {'content': content}}
        client._request = Mock(side_effect=respond)
        return client

    def test_full_party_description_translates_inside_blue_boundaries(self):
        value = record('Продам ' + raw_link('ignored', 1) + ' / ' + raw_link('Ищем хилера [12~19] (8/9)'))
        client = self.client()
        result = client.translate_record(value)
        self.assertEqual(result, 'Selling [Shining Knife] / Looking for a healer [12~19] (8/9)')
        self.assertEqual(colored(result), [('item', '[Shining Knife]'),
                                          ('party', 'Looking for a healer [12~19] (8/9)')])
        body = client._request.call_args.args[1]['messages'][1]['content']
        self.assertFalse(any(0xe000 <= ord(char) <= 0xf8ff for char in body))
        self.assertNotIn('Type=', body)
        self.assertNotIn('ClassID=', body)

    def test_identical_plain_and_linked_items_keep_occurrence_provenance(self):
        value = record('[Shining Knife] / ' + raw_link('ignored', 1))
        result = self.client().translate_record(value)
        self.assertEqual(result, value.message)
        self.assertEqual(result.link_spans, value.link_spans)
        self.assertEqual(len(result.link_spans), 1)

    def test_display_boundaries_do_not_hide_standalone_language_hints(self):
        for source, target, expected in (('bien ahi', 'English', 'Nice!'),
                                         ('Farming before work starts', 'Spanish', 'farmeando')):
            with self.subTest(source=source):
                value = record(raw_link(source))
                client = self.client()
                client.target_language = target
                client.translate_record(value)
                messages = client._request.call_args.args[1]['messages']
                self.assertIn(expected, messages[0]['content'])
                self.assertIn('[[L2ITEM', messages[1]['content'])

    def test_item_words_remain_opaque_when_building_display_free_hints(self):
        value = record('Продам ' + raw_link('bien ahi', 1).replace('ClassID=217', 'ClassID=2147483647'))
        client = self.client()
        client.translate_record(value)
        system = client._request.call_args.args[1]['messages'][0]['content']
        self.assertNotIn('Nice!', system)

    def test_fragment_recovery_keeps_full_party_boundary_and_fixed_numbers(self):
        value = record(raw_link('Ищем хилера [12~19] (8/9)'))
        client = self.client()
        good_response = client._request.side_effect
        def respond(path, payload):
            if 'format' not in payload:
                return {'message': {'content': 'bad response without fixed tokens'}}
            return good_response(path, payload)
        client._request.side_effect = respond
        result = client.translate_record(value)
        self.assertEqual(result, 'Looking for a healer [12~19] (8/9)')
        self.assertEqual(colored(result), [('party', str(result))])
        self.assertEqual(client._request.call_count, 2)

    def test_integrity_failure_retries_plain_translation_without_guessing_styles(self):
        value = record(raw_link('Ищем хилера'))
        client = OllamaClient()
        client.translate = Mock(side_effect=[TranslationInvariantError('fixed tokens changed'), 'Looking for a healer'])
        result = client.translate_record(value)
        self.assertEqual(result, 'Looking for a healer')
        self.assertEqual(result.link_spans, ())
        self.assertEqual(client.translate.call_count, 2)
        self.assertEqual(client.translate.call_args.args, (value.message,))

    def test_network_failure_is_not_retried_for_cosmetic_boundaries(self):
        client = OllamaClient()
        client.translate = Mock(side_effect=OllamaError('connection unavailable'))
        with self.assertRaisesRegex(OllamaError, 'connection unavailable'):
            client.translate_record(record(raw_link('Ищем хилера')))
        client.translate.assert_called_once()

    def test_display_tokens_cannot_turn_a_complete_plain_translation_into_an_output_error(self):
        value = record(raw_link('Ищем хилера'))
        client = self.client()
        good_response = client._request.side_effect
        def respond(path, payload):
            if client._request.call_count == 1:
                return {'done_reason': 'length'}
            return good_response(path, payload)
        client._request.side_effect = respond
        result = client.translate_record(value)
        self.assertEqual(result, 'Looking for a healer')
        self.assertEqual(result.link_spans, ())
        self.assertEqual(client._request.call_count, 2)
        self.assertEqual(client._request.call_args.args[1]['messages'][1]['content'], value.message)

    def test_party_details_moved_outside_the_boundary_do_not_color_neighboring_prose(self):
        value = record('Outside ' + raw_link('Ищем хилера [12~19] (8/9)'))
        client = self.client()
        client._request.return_value = {'message': {'content':
            '[[L2ITEM0]]Outside Looking for a healer[[L2ITEM3]] [[L2ITEM1]] [[L2ITEM2]]'}}
        client._request.side_effect = None
        result = client.translate_record(value)
        self.assertEqual(result, 'Outside Looking for a healer [12~19] (8/9)')
        self.assertEqual(result.link_spans, ())

    def test_full_length_message_keeps_existing_translation_allowance(self):
        value = record(raw_link('Ищем хилера'))
        value = replace(value, message=value.message + 'x' * (MAX_CHAT_CHARACTERS - len(value.message)))
        client = OllamaClient()
        client.translate = Mock(return_value=value.message)
        result = client.translate_record(value)
        client.translate.assert_called_once_with(value.message)
        self.assertEqual(result.link_spans, value.link_spans)


class BoundarySafetyTests(unittest.TestCase):
    def test_collisions_and_crossed_boundaries_never_leak_markers_or_invent_styles(self):
        source = '\ue000[Item] / Party'
        spans = (ChatLinkSpan('item', 1, 7), ChatLinkSpan('party', 10, 15))
        marked, boundaries = mark_link_boundaries(source, spans)
        self.assertNotEqual(boundaries[0].opening, '\ue000')
        self.assertEqual(restore_link_boundaries(marked, boundaries), source)
        crossed = (boundaries[0].opening + '[Item]' + boundaries[1].opening + 'Party'
                   + boundaries[0].closing + boundaries[1].closing)
        result = restore_link_boundaries(crossed, boundaries)
        self.assertEqual(result, '[Item]Party')
        self.assertEqual(result.link_spans, ())

    def test_duplicate_missing_and_changed_item_boundaries_are_plain(self):
        marked, boundaries = mark_link_boundaries('[Item]', (ChatLinkSpan('item', 0, 6),))
        for bad in (marked + boundaries[0].closing, marked.replace(boundaries[0].opening, ''),
                    marked.replace('[Item]', 'unrelated words')):
            result = restore_link_boundaries(bad, boundaries)
            self.assertEqual(result.link_spans, ())
            self.assertFalse(any(marker in result for marker in (boundaries[0].opening, boundaries[0].closing)))

    def test_invalid_source_metadata_and_marker_exhaustion_are_plain(self):
        self.assertEqual(LinkedText('text', (ChatLinkSpan('item', -1, 3),)).link_spans, ())
        text = ''.join(chr(code) for code in range(0xe000, 0xf900))
        self.assertEqual(mark_link_boundaries(text, (ChatLinkSpan('party', 0, 1),)), (text, ()))

    def test_normalization_maps_unicode_whitespace_and_partial_truncation(self):
        source = '  😀  [Item]\t /\nParty title  '
        value = LinkedText(source, (ChatLinkSpan('item', 5, 11), ChatLinkSpan('party', 15, 26)))
        result = normalize_display(value, limit=18)
        self.assertEqual(result, '😀 [Item] / Party t')
        self.assertEqual(colored(result), [('item', '[Item]'), ('party', 'Party t')])
