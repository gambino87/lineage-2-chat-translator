# Lineage 2 Chat Translator

Source for **v{{VERSION}}**, published so users can inspect how the translator works
before installing it. This repository contains the application, installer and
build scripts, tests, item catalog, artwork, and website source.

[Download the Windows installer](https://40.160.140.227/) ·
[Feedback and bug reports on Discord](https://discord.gg/V4ph4Ww5m)

## What it does

- Reads chat from a supported Lineage II client's existing `Network.log`.
- Translates through a local Ollama model and displays an always-on-top overlay.
- Preserves item names, prices and common game abbreviations; offers channel filters.
- Translates replies into the selected language and copies them to the clipboard.
- Downloads the selected runtime/model during setup and manages its local service.

The translator reads the log file; it does not read game process memory, inject
into the client or send chat to the game. Supported log formats depend on the
client/server. Translation quality depends on the selected model and message.

## Source map

| Location | Purpose |
| --- | --- |
| `translator_core.py`, `game_slang.py`, `item_catalog.py` | Parsing, local-model requests, glossary and protected text |
| `translator_app.py`, `chat_overlay.py`, `overlay_tray.py`, `reply_composer.py` | Launcher, overlay, filters and replies |
| `installer_main.py`, `installer_runtime.py`, `app_paths.py` | Setup, model downloads and owned service lifecycle |
| `app_updater.py`, `update_controls.py`, `installation_cleanup.py`, `uninstall_data.py` | Updates, cleanup and uninstall |
| `assets/` | Icons, item catalog and trusted update/cleanup helpers |
| `tests/` | Automated regression checks using mocks and temporary files |
| `packaging/` | Build scripts, Inno Setup source, notices and deployment tooling |
| `sites/l2-chat-translator/` | Homepage template and release changelog |
| `tools/import_item_catalog.py` | Item-data import tool and provenance checks |
| `game_slang_research.json` | Glossary references with synthetic examples |

Other root modules and PowerShell/CMD scripts support icons, versioning, log
selection and development startup. `verify_local.py` is an optional diagnostic
that contacts an already configured local model and checks the selected log;
it is **not** part of the offline regression suite.

## Build and test

Use **Windows 10/11 x64**, **Python 3.12 x64 with Tcl/Tk**, and PowerShell 7.
Run these commands from the repository root:

```powershell
pwsh -NoProfile -File packaging/bootstrap-tools.ps1
.build-tools/venv/Scripts/python.exe -m unittest discover -s tests
.build-tools/venv/Scripts/python.exe packaging/build.py --skip-installer
```

Bootstrap downloads the pinned build dependencies and verified portable Inno
Setup compiler. The app-only build runs the regression suite, freezes the
application and runs a headless self-test. Official release builds omit
`--skip-installer` to compile the installer and prepare the signed website feed;
that requires the publisher's private Windows signing identity, which is not
included in this repository.
It does not install the result, open the interface, start Ollama or download models.
Generated files go into `.build/`, `release/` and the site's `dist/`, all ignored
by Git. See [the build documentation](packaging/README.md) for details.

For a separately published fork, provision your own signing identity and update
its public trust anchors and hosting configuration. See [update signing](packaging/UPDATE-SIGNING.md).
Tests skip checks that require an unavailable platform facility; a successful
test run does not establish coverage of every Windows or model configuration.

The included website project ID identifies the public mirror and is not a
credential. Local page preparation does not publish anything. Deploying a
separate instance requires your own hosting and credentials; read the deployment
scripts before using them. Do not deploy a modified build under an existing
official version or upload it to the official update feed.

## Relationship to the published installer

[SOURCE-MANIFEST.json](SOURCE-MANIFEST.json) records the v{{VERSION}} release installer
checksum and the SHA-256 hashes of its application modules and assets.
From v1.7.12, clients verify a separate release signature against the public key
bundled in the app. The signing key stays off the download server and is never
part of this repository. See [update signing](packaging/UPDATE-SIGNING.md) for
the protocol, bootstrap requirements and limits. Windows publisher signing is
separate and is not included.
Those files are byte-for-byte copies of the recorded release inputs. The
manifest also inventories the other files in this snapshot, excluding itself.
Git preserves their original line endings so those hashes remain checkable.

Documentation and test fixtures are adapted for publication: example player
names, a streamer URL path and a private operator key-file location are replaced
with generic examples, and the user guide's version heading is corrected to
{{VERSION}}. These changes are listed in the source manifest. The
private development diary and review tooling/data are omitted.

This is a source transparency snapshot, **not a claim of byte-for-byte binary
reproducibility**. In particular, the official obsolete-file cleanup plan is
generated from checksum-verified older installers and their manifests. These
historical binaries are not in Git. An app-only build can run without that
history, but its historical cleanup inventory differs. Official signed release
preparation also requires the private publisher identity. Toolchain, timestamps
and packaging metadata can also affect the resulting executable bytes.

## Privacy and network behavior

Chat and reply translation requests go to the configured loopback Ollama service.
The application retains recent messages in memory and saves user preferences
locally. It does not intentionally persist the displayed translations; the game
maintains its own log. Copied replies are subject to Windows clipboard history
and synchronization settings.

Setup downloads Ollama/model files from upstream. Update checks and downloads
contact the project's HTTPS host; those servers can see the connecting IP address.
The repository contains no model weights, Ollama binaries, personal settings,
chat logs, saved translation reviews, credentials or compiled installers.

Regression fixtures are synthetic or deidentified. Do not commit real logs,
tokens, settings or local diagnostics when reporting a problem.

## Licensing

The original application code is made available for transparency; no additional
open-source license has been selected for it. Existing third-party licenses,
including GPL-3.0 for the separate L2J-derived item dataset, remain in effect.
See [LICENSE-NOTICE.md](LICENSE-NOTICE.md) and the preserved upstream notices.

## Keeping source current

The release exporter publishes an allowlisted source snapshot and verifies its
public GitHub commit before exporting the website release. Version tags preserve
the matching application and installer identity; tooling-only updates can advance
`main` without moving an existing tag. See [the source publication workflow](packaging/PUBLIC-SOURCE.md).
