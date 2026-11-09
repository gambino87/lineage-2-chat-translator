"""Offline item names keyed by template (ClassID), independent of the LLM.

The bundled reference uses standard L2J High Five IDs. Locale entries are data,
not inferred translations; a missing localized name falls back to English.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

CATALOG_PATH = Path(__file__).resolve().parent / 'assets' / 'item_catalog.json'
_LOCALES = {
    'english': 'en', 'en': 'en', 'chinese': 'zh-Hans', 'cn': 'zh-Hans',
    'zh': 'zh-Hans', 'zh-cn': 'zh-Hans', 'zh-hans': 'zh-Hans',
    'zh-tw': 'zh-Hant', 'zh-hant': 'zh-Hant', 'russian': 'ru', 'ru': 'ru',
    'spanish': 'es', 'es': 'es', 'portuguese': 'pt-BR', 'pt': 'pt-BR',
    'pt-br': 'pt-BR', 'pt-pt': 'pt-PT',
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate item catalog key: ' + key)
        result[key] = value
    return result


def _valid_id(value):
    return type(value) is int and 0 < value <= 2**31 - 1


def _valid_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 512 and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _normalized(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


class ItemCatalog:
    def __init__(self, document):
        if not isinstance(document, dict) or document.get('schema_version') != 1:
            raise ValueError('Unsupported item catalog schema')
        self.version = document['catalog_version']
        self.namespace = document['namespace']
        self.source = document['source']
        self.locales = document['locales']
        self.items = document['items']
        if (not isinstance(self.version, str) or not self.version
                or not isinstance(self.namespace, str) or not self.namespace
                or not isinstance(self.source, dict) or not isinstance(self.locales, dict)
                or 'en' not in self.locales or not isinstance(self.items, dict) or not self.items):
            raise ValueError('Incomplete item catalog')
        for key, item in self.items.items():
            if not re.fullmatch(r'[1-9][0-9]{0,9}', key) or not _valid_id(int(key)):
                raise ValueError('Invalid item template ID')
            if not isinstance(item, dict) or not isinstance(item.get('names'), dict):
                raise ValueError('Invalid item names')
            names = item['names']
            if 'en' not in names or not set(names).issubset(self.locales) or not all(map(_valid_name, names.values())):
                raise ValueError('Invalid or missing canonical item name')
            aliases = item.get('aliases', {})
            if (not isinstance(aliases, dict) or not set(aliases).issubset(self.locales)
                    or any(not isinstance(values, list) or not all(map(_valid_name, values))
                           for values in aliases.values())):
                raise ValueError('Invalid localized item aliases')

    @classmethod
    def load(cls, path=CATALOG_PATH):
        return cls(json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_object))

    def name(self, class_id, locale='en', *, fallback=True):
        if not _valid_id(class_id):
            return None
        item = self.items.get(str(class_id))
        if item is None:
            return None
        locale = _LOCALES.get(locale.casefold(), locale) if isinstance(locale, str) else 'en'
        return item['names'].get(locale) or (item['names']['en'] if fallback else None)

    def coverage(self):
        return {locale: sum(locale in item['names'] for item in self.items.values())
                for locale in self.locales}

    def resolve_title(self, class_id, title, locale='en'):
        canonical = self.name(class_id, locale)
        if canonical is None:
            return title
        item = self.items[str(class_id)]
        known = {_normalized(name) for name in item['names'].values()}
        known.update(_normalized(name) for names in item.get('aliases', {}).values() for name in names)
        if not title or _normalized(title) in known:
            return canonical
        # Enchant level and separate annotations are properties of this instance,
        # not part of the template ID. Preserve them outside model translation.
        prefix = ''
        suffix = ''
        body = title.strip()
        enchant = re.match(r'^(\+[0-9]{1,4})(?![0-9.,])\s*(.+)$', body, re.DOTALL)
        if enchant:
            prefix, body = enchant[1] + ' ', enchant[2]
        elif body.startswith('+'):
            return title  # Do not partially consume malformed/overlong numbers.
        trailing_enchant = re.fullmatch(r'(.*?)(\s*\+[0-9]{1,4})', body)
        if trailing_enchant and trailing_enchant[1]:
            body, suffix = trailing_enchant[1], trailing_enchant[2]
        elif re.search(r'\+[0-9.,]+$', body):
            return title
        if _normalized(body) in known:
            return prefix + canonical + suffix
        literal_names = list(item['names'].values())
        literal_names += [name for values in item.get('aliases', {}).values() for name in values]
        for name in sorted(literal_names, key=len, reverse=True):
            start = re.match(re.escape(name) + r'(?=$|[\s(\[+])', body, re.IGNORECASE)
            if start:
                # Preserve even unfamiliar instance details when the base name
                # is verified; never silently drop elemental stats or labels.
                return prefix + canonical + body[start.end():] + suffix
        # Peel complete annotations one at a time so (Event) [PvP] survives.
        annotations = []
        while decoration := re.fullmatch(r'(.+?)(\s*(?:\([^()]*\)|\[[^\[\]]*\]))', body):
            body = decoration[1]
            annotations.insert(0, decoration[2])
            if _normalized(body) in known:
                break
        if _normalized(body) not in known:
            dash = re.fullmatch(r'(.+?)(\s+[-–—]\s+.+)', body)
            if dash:
                canonical_dash = re.fullmatch(r'(.+?)(\s+[-–—]\s+.+)', canonical)
                if canonical_dash:
                    if _normalized(dash[2]) != _normalized(canonical_dash[2]):
                        # A conflicting variant cannot be resolved honestly from
                        # these two inputs. Retain the protected source title.
                        return title
                else:
                    annotations.insert(0, dash[2])
        result = canonical
        for annotation in annotations:
            if not _normalized(result).endswith(_normalized(annotation)):
                result += annotation if annotation[0].isspace() else ' ' + annotation
        return prefix + result + suffix


@lru_cache(maxsize=1)
def get_catalog():
    return ItemCatalog.load()


def get_item_name(class_id, locale='en', *, fallback=True):
    return get_catalog().name(class_id, locale, fallback=fallback)


def resolve_item_title(class_id, title, locale='en'):
    if not _valid_id(class_id):
        return title
    try:
        catalog = get_catalog()
    except (OSError, ValueError, KeyError, TypeError):
        # An absent/corrupt optional data file must not erase an original title.
        # Release self-tests require a healthy, populated bundled catalog.
        return title
    return catalog.resolve_title(class_id, title, locale)
