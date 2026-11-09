# Source availability and third-party licenses

The original Lineage 2 Chat Translator application source is published for
transparency. No additional open-source license is granted for the original
project code by this publication. Public repository access remains subject to
GitHub's terms, including its viewing and forking features.

This statement does not replace or restrict existing third-party licenses.

- `assets/item_catalog.json` contains item data derived from the L2J Server
  DataPack, licensed under GPL-3.0. Its pinned source revision, provenance and
  attribution are in [the item catalog notice](assets/ITEM-CATALOG-NOTICE.txt),
  and the full license is in [L2J-ITEM-DATA.txt](packaging/licenses/L2J-ITEM-DATA.txt).
  [The importer](tools/import_item_catalog.py) documents how the data is generated.
- Notices for Python, Tcl/Tk, PyInstaller and Inno Setup are preserved in
  [packaging/licenses](packaging/licenses/) and
  [THIRD-PARTY-NOTICES.txt](packaging/THIRD-PARTY-NOTICES.txt).
- Ollama and LLM model weights are separate upstream downloads. They are not
  included in this repository and retain their own licenses.

This repository is described as **source available for transparency**, not as
a project with a newly selected open-source license.
