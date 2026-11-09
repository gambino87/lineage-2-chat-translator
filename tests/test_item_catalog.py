import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from item_catalog import ItemCatalog, get_catalog, get_item_name, resolve_item_title


class ItemCatalogTests(unittest.TestCase):
    def setUp(self):
        self.document = {
            'schema_version': 1, 'catalog_version': 'synthetic-test', 'namespace': 'synthetic',
            'source': {'project': 'synthetic controls'},
            'locales': {'en': {}, 'zh-Hans': {}, 'ru': {}, 'es': {}, 'pt-BR': {}},
            'items': {'217': {'names': {'en': 'Shining Knife', 'zh-Hans': '闪耀刀'},
                              'aliases': {'ru': ['Сияющий Нож']}},
                      '738': {'names': {'en': 'Sword of Solidarity'}},
                      '6581': {'names': {'en': 'Forgotten Blade - Haste'}}},
        }
        self.catalog = ItemCatalog(self.document)

    def test_explicit_locales_with_honest_english_fallback(self):
        self.assertEqual(self.catalog.name(217), 'Shining Knife')
        self.assertEqual(self.catalog.name(217, 'Chinese'), '闪耀刀')
        self.assertEqual(self.catalog.name(217, 'CN'), '闪耀刀')
        for language in ['Russian', 'Spanish', 'Portuguese', 'pt-PT', 'unknown']:
            self.assertEqual(self.catalog.name(217, language), 'Shining Knife')
            self.assertIsNone(self.catalog.name(217, language, fallback=False))
        self.assertEqual(self.catalog.coverage(), {'en': 3, 'zh-Hans': 1, 'ru': 0, 'es': 0, 'pt-BR': 0})

    def test_unknown_or_invalid_ids_do_not_guess_names(self):
        for identity in [None, 0, -1, True, '217', 217.0, 2**31, 999999]:
            self.assertIsNone(self.catalog.name(identity))
            self.assertEqual(self.catalog.resolve_title(identity, 'Custom меч +8'), 'Custom меч +8')

    def test_known_id_is_authoritative_for_localized_title(self):
        for title in ['闪耀刀', 'Сияющий Нож', 'Different localized title', '']:
            self.assertEqual(self.catalog.resolve_title(217, title), 'Shining Knife')

    def test_preserves_enchant_levels_and_instance_annotations(self):
        cases = {
            '+6 闪耀刀': '+6 Shining Knife',
            '+12闪耀刀': '+12 Shining Knife',
            '闪耀刀 +8': 'Shining Knife +8',
            '闪耀刀 - Focus': 'Shining Knife - Focus',
            '+6 闪耀刀 (Event)': '+6 Shining Knife (Event)',
            '闪耀刀 [PvP]': 'Shining Knife [PvP]',
            '闪耀刀 (Event) [PvP]': 'Shining Knife (Event) [PvP]',
            '+7 Shining Knife 150 Fire': '+7 Shining Knife 150 Fire',
            '闪耀刀 火150': 'Shining Knife 火150',
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(self.catalog.resolve_title(217, title), expected)
        self.assertEqual(self.catalog.resolve_title(6581, '+7 Forgotten Blade - Haste'),
                         '+7 Forgotten Blade - Haste')
        self.assertEqual(self.catalog.resolve_title(6581, '+7 Forgotten Blade - Haste (Event)'),
                         '+7 Forgotten Blade - Haste (Event)')
        self.assertEqual(self.catalog.resolve_title(6581, 'Forgotten Blade - Focus'),
                         'Forgotten Blade - Focus')

    def test_malformed_enchant_numbers_are_not_partially_consumed(self):
        for title in ['+12345 Shining Knife', '+6.5 Shining Knife', '+6,5 闪耀刀',
                      '闪耀刀 +12345', '闪耀刀 +6.5']:
            self.assertEqual(self.catalog.resolve_title(217, title), title)

    def test_bad_catalog_data_is_rejected(self):
        candidates = []
        bad = copy.deepcopy(self.document)
        bad['items']['217']['names'].pop('en')
        candidates.append(bad)
        bad = copy.deepcopy(self.document)
        bad['items']['217']['names']['en'] = 'Bad\nname'
        candidates.append(bad)
        bad = copy.deepcopy(self.document)
        bad['items']['0'] = bad['items']['217']
        candidates.append(bad)
        bad = copy.deepcopy(self.document)
        bad['items']['217']['names']['invented'] = 'Name'
        candidates.append(bad)
        for document in candidates:
            with self.assertRaises(ValueError):
                ItemCatalog(document)

    def test_duplicate_json_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog.json'
            value = json.dumps(self.document)
            value = value.replace('"items": {', '"items": {"217": {},')
            path.write_text(value, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                ItemCatalog.load(path)

    def test_missing_catalog_keeps_original_title(self):
        with patch('item_catalog.get_catalog', side_effect=FileNotFoundError):
            self.assertEqual(resolve_item_title(217, '闪耀刀'), '闪耀刀')
            self.assertEqual(resolve_item_title(217, ''), '')

    def test_bundled_reference_includes_reported_items_and_locale_coverage(self):
        catalog = get_catalog()
        self.assertEqual(catalog.namespace, 'l2j-high-five')
        for identity, english in {217: 'Shining Knife', 738: 'Sword of Solidarity',
                                  8191: 'Necklace of Frintezza', 16025: 'Necklace of Freya'}.items():
            self.assertEqual(get_item_name(identity), english)
        self.assertEqual(get_item_name(217, 'zh-Hans'), '闪耀刀')
        self.assertEqual(get_item_name(738, 'zh-Hans'), '团结之剑')
        self.assertGreater(catalog.coverage()['en'], 10000)
        self.assertEqual(catalog.coverage()['zh-Hans'], 2)


if __name__ == '__main__':
    unittest.main()
