"""Rebuild the shipped item-name catalog from a pinned public L2J datapack.

Reads only public XML, README and license text from upstream. Does not read a
client, game log, database or process, and never executes downloaded content.
Run deliberately when updating the checked-in catalog; the app works offline.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib import error, request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "L2J Server DataPack"
PROJECT_URL = "https://bitbucket.org/l2jserver/l2j-server-datapack"
REVISION = "b319c9a4a0a2ce53808306b113e3ee6c834a7f94"
ARCHIVE_URL = PROJECT_URL + "/get/" + REVISION + ".zip"
ITEM_FOLDER = "src/main/resources/data/stats/items/"
CATALOG_VERSION = "2026-11-08.1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
FILENAME = re.compile(r"([0-9]{5})-([0-9]{5})\.xml")
KNOWN_ITEMS = {
    "217": "Shining Knife", "738": "Sword of Solidarity",
    "8191": "Necklace of Frintezza", "16025": "Necklace of Freya",
}
OBSERVED_ALIASES = {"217": "闪耀刀", "738": "团结之剑"}
LOCALES = {
    "en": {"label": "English", "source": "upstream"},
    "zh-Hans": {
        "label": "Chinese (Simplified)", "source": "observed-link-titles",
        "notes": "Two linked titles checked against ClassID; not a complete official translation",
    },
    "zh-Hant": {"label": "Chinese (Traditional)"},
    "pt-BR": {"label": "Portuguese (Brazil)"},
    "pt-PT": {"label": "Portuguese (Portugal)"},
    "es": {"label": "Spanish"},
    "ru": {"label": "Russian"},
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def fetch_archive(url: str) -> bytes:
    parsed = urlsplit(url)
    require(parsed.scheme == "https" and parsed.netloc == "bitbucket.org"
            and url == ARCHIVE_URL, "Unexpected source URL")
    req = request.Request(url, headers={"User-Agent": "L2ChatTranslator-item-catalog-import/1"})
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=45) as response:
                content = response.read(MAX_ARCHIVE_BYTES + 1)
            break
        except error.HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Source download failed ({exc.code}): {url}") from exc
            retry_after = exc.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdecimal() else 5 * (attempt + 1)
            if delay > 60:
                raise RuntimeError(f"Upstream asks to retry after {delay}s; try the import later") from exc
            time.sleep(max(1, delay))
    require(len(content) <= MAX_ARCHIVE_BYTES, "Oversized source archive")
    return content


def parse_item_file(path: str, content: bytes) -> tuple[dict, list[tuple[str, str]]]:
    require(b"<!DOCTYPE" not in content.upper() and b"<!ENTITY" not in content.upper(),
            "XML entities are not accepted")
    tree = ET.fromstring(content)
    require(tree.tag == "list", "Unexpected item XML format")
    match = FILENAME.fullmatch(path.rsplit("/", 1)[1])
    require(match is not None, "Invalid item filename")
    lower, upper = int(match[1]), int(match[2])
    require(lower % 100 == 0 and upper == lower + 99, "Unexpected source item range")
    rows = []
    for item in tree.findall("item"):
        raw_id, name = item.attrib["id"], item.attrib["name"]
        require(re.fullmatch(r"[1-9][0-9]*", raw_id) is not None, "Invalid item ID")
        require(lower <= int(raw_id) <= upper, "Item outside source filename range: " + raw_id)
        require(name.strip() == name and name and all(ord(c) >= 32 for c in name),
                "Invalid item name for " + raw_id)
        rows.append((raw_id, name))
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest()}, rows


def make_catalog() -> tuple[dict, str, str]:
    # One ~22 MiB upstream archive is more reliable than hundreds of API calls.
    # Inspect only selected data members; never extract or execute the archive.
    archive = fetch_archive(ARCHIVE_URL)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
        require(len(names) == len(set(names)), "Duplicate archive members")
        license_paths = [name for name in names if name.endswith('/LICENSE.md') and name.count('/') == 1]
        require(len(license_paths) == 1, "Missing root source license")
        prefix = license_paths[0].split('/')[0] + '/'
        paths = sorted(name[len(prefix):] for name in names if name.startswith(prefix + ITEM_FOLDER)
                       and FILENAME.fullmatch(name[len(prefix + ITEM_FOLDER):]))
        require(0 < len(paths) <= 300, "Missing or excessive source item files")
        selected = paths + ['LICENSE.md', 'README.md']
        require(sum(bundle.getinfo(prefix + path).file_size for path in selected) <= MAX_ARCHIVE_BYTES,
                "Oversized source data")

        def read_selected(path):
            member = bundle.getinfo(prefix + path)
            require(member.file_size <= MAX_RESPONSE_BYTES, "Oversized source member")
            return bundle.read(member)

        sources = [parse_item_file(path, read_selected(path)) for path in paths]
        license_bytes = read_selected('LICENSE.md')
        readme_bytes = read_selected('README.md')
    items = {}
    for source, rows in sources:
        for item_id, name in rows:
            require(item_id not in items, "Duplicate or conflicting item ID: " + item_id)
            items[item_id] = {"names": {"en": name}}
    for item_id, name in KNOWN_ITEMS.items():
        require(items.get(item_id, {}).get("names", {}).get("en") == name,
                "Known item changed or missing: " + item_id)
    for item_id, name in OBSERVED_ALIASES.items():
        items[item_id]["names"]["zh-Hans"] = name
    require(b"GNU GENERAL PUBLIC LICENSE" in license_bytes and b"Version 3, 29 June 2007" in license_bytes,
            "Unexpected upstream license")
    require(b"master branch is the latest High Five release" in readme_bytes,
            "Upstream chronicle declaration changed")
    files = [source for source, _ in sources]
    files.extend({"path": path, "sha256": hashlib.sha256(content).hexdigest()}
                 for path, content in (("LICENSE.md", license_bytes), ("README.md", readme_bytes)))
    catalog = {
        "schema_version": 1, "catalog_version": CATALOG_VERSION, "namespace": "l2j-high-five",
        "source": {"project": PROJECT, "url": PROJECT_URL, "revision": REVISION,
                   "license": "GPL-3.0", "files": files},
        "locales": LOCALES,
        "items": {item_id: items[item_id] for item_id in sorted(items, key=int)},
    }
    license_text = license_bytes.decode("utf-8").replace("\r\r\n", "\n").replace("\r\n", "\n")
    notice = f"""L2 Chat Translator item-name reference catalog

Catalog version: {CATALOG_VERSION}
Namespace: l2j-high-five (High Five reference; not a universal server registry)
English source: {PROJECT}
Project: {PROJECT_URL}
Pinned source revision: {REVISION}
Source directory: {ITEM_FOLDER}
Data license: GNU General Public License version 3 (GPL-3.0)
Full license: packaging/licenses/L2J-ITEM-DATA.txt in source distributions;
licenses/L2J-ITEM-DATA.txt in installed distributions.

This separate data catalog contains numeric item template IDs and English names
extracted from the public L2J Server DataPack. It does not contain client assets,
descriptions, icons, stats, game logs or player information. Source XML and the
upstream README/license hashes are recorded in item_catalog.json. The JSON is
the editable source form of the catalog. A reproducible importer is provided at
tools/import_item_catalog.py in the translator source.

Changes from upstream: retained only ID/name fields; indexed names by locale;
added two manually checked Chinese link-title aliases for IDs 217 and 738.
Those two aliases are observed labels, not a complete official translation.
All other non-English locale slots deliberately have no unverified names.
No item names are generated by a language model or learned from incoming chat.

Custom servers and other chronicles can reuse IDs or alter names. This data is
a reference, not a guarantee about any particular server. Preserve unknown
titles and item modifiers rather than guessing an item identity.

The catalog is provided without warranty; see the accompanying GPLv3 license.
Upstream authors retain their notices and rights. Game/item names belong to
their respective owners; this reference does not imply their endorsement.
"""
    return catalog, license_text, notice


def write_artifact(path: Path, text: str) -> None:
    require(not path.is_symlink(), "Refusing linked output: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, prefix="." + path.name, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(text)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    args = parser.parse_args()
    catalog, license_text, notice = make_catalog()
    destination = args.output_root / "assets/item_catalog.json"
    write_artifact(destination, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    write_artifact(args.output_root / "packaging/licenses/L2J-ITEM-DATA.txt", license_text)
    write_artifact(args.output_root / "assets/ITEM-CATALOG-NOTICE.txt", notice)
    print(json.dumps({"catalog": str(destination), "items": len(catalog["items"]),
                      "xml_files": len(catalog["source"]["files"]) - 2,
                      "source_revision": REVISION, "bytes": destination.stat().st_size,
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                      "localized_names": {"en": len(catalog["items"]), "zh-Hans": 2}}, indent=2))


if __name__ == "__main__":
    main()
