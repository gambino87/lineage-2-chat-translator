# Security and privacy overview

Reviewed application: **1.7.14**. Security overview updated: **2026-11-10**.

The reviewed source implements a local chat-log reader and translator. It does
not contain a game-memory reader, injected game component, screen capture,
keylogger, automatic game input, or a chat-upload/telemetry feature. Translation
requests go to the launcher's authenticated local backend. Installing it still
means trusting a Windows executable, its publisher, its build inputs, the model
provider and the installing Windows account.

This review found no confirmed new exploitable defect in the inspected paths.
It found and corrected an obsolete development diagnostic that could no longer
authenticate to the protected backend. The correction uses the offline
self-test; it does not relax backend authentication or alter the installer.
This is a source review with regression checks, not an independent security
certification, full dependency audit or live penetration test.

For the functions behind each statement, use [CODEMAP.md](CODEMAP.md). Release
input hashes are recorded in SOURCE-MANIFEST.json in the public repository.

## What runs, and with whose permissions?

The application is a Python/Tk Windows program packaged with PyInstaller. Its
Inno Setup installer requests a per-user installation without administrator
elevation. Setup checks and restricts the owned program directory before
replacing files and checks it again before running installed code.

The launcher starts a verified protected Ollama frontend. Ollama can start
native CPU/GPU model runners; those processes run as the same Windows user.
The model backend is **not sandboxed**. Local authentication protects access
to that backend; it does not remove the backend's ability to read or write
files accessible to its Windows account.

There is no separately installed always-on Windows service required by the
translator. The launcher retains ownership of its child backend and shuts down
its process tree. It does not terminate every process named Ollama. Interactive
launch, model setup, update installation and uninstall are different actions;
the offline self-test does not perform those actions.

The optional Minimize to system tray setting hides only the launcher; translation,
the overlay and the owned backend continue running. The notification icon uses
the existing app artwork and an owned hidden native message window. It installs
no global input hook and handles no chat data. Click/Open restores the same
session; tray Exit or the window's X invokes the normal shutdown path. Native
events reach Tk through a queue, and the launcher is hidden only after icon
registration succeeds. Native cleanup must finish before update handoff.
See [tray_controls.py](tray_controls.py) and [windows_tray.py](windows_tray.py).

Evidence: [installer_main.py](installer_main.py),
[installer_runtime.py](installer_runtime.py),
[installer.iss](packaging/installer.iss),
[installation permission helper](assets/protect-install-directory.ps1).

## What can read or receive chat?

    Selected existing Network.log
      -> read-only file handle -> bounded parser/queue
      -> local-model translation and language identification
      -> launcher/overlay history in process memory

    Explicitly submitted reply
      -> local-model translation -> Windows clipboard
      -> user chooses where to paste

The parser accepts the supported incoming chat record format. The app opens
the selected file for reading, while allowing the game to continue writing or
rotating it. It does not attach to the game process, read its memory, change
game files, enable game logging, or send chat/key presses to the game.

Incoming translation requests include message text and translation instructions;
the worker does not add the record's sender, timestamp or channel fields to the
model prompt. Language identification can make an additional local-model
request before a reply is selected. A name written inside a message is still
part of that message. Reply text can include a deliberately selected whisper
recipient in the clipboard output.

The log selector is not a filesystem sandbox: the user can choose another
readable file. A network/UNC path can engage Windows network-file access.
The privacy description assumes a local game log and the unmodified managed
launcher/backend configuration.

Evidence: [game_log_setup.py](game_log_setup.py),
[translator_core.py](translator_core.py): _open_shared, LogTail, parse_chat_line,
OllamaClient; [translator_app.py](translator_app.py): enqueue, _worker;
[reply_composer.py](reply_composer.py): translate, copy.

## Internet and local-network activity

| Action | Destination and information involved |
| --- | --- |
| Chat/reply translation and language detection | Authenticated HTTP on numeric IPv4 loopback, normally 127.0.0.1:11435. Contains the relevant message/draft and model instructions. No cloud translation endpoint is configured. |
| Runtime setup | Official GitHub release/download infrastructure over HTTPS. Fetches native-runtime files; the archive and installed file inventory are checksum pinned. Remote download hosts can see connection IP and request metadata. |
| Model setup | Upstream Ollama model registry/download infrastructure, through the local backend. Fetches the chosen model; it does not upload game chat as part of the pull. This depends on upstream model/registry trust and may use inherited network proxy settings. |
| Opening Settings update controls | Fixed project HTTPS host, 40.160.140.227. Release requests identify the application version; the host sees connection IP and ordinary HTTP metadata. No chat, prompts, settings or game-log paths are included. Checking does not download/run an installer. |
| Explicit update install | Uses the already authenticated metadata and fetches/verifies its installer from the same fixed HTTPS origin. Proxies and redirects are refused by the updater. |
| Clicking a model license or Discord feedback link | Opens the chosen fixed URL in the default browser. No diagnostic bundle or chat text is attached automatically. Browser/site privacy behavior then applies. |

Normal translation requests disable proxies and redirects, reject non-loopback
endpoints and reject models reporting remote/cloud aliases. The backend
environment disables Ollama cloud features. These are source-level controls,
not a promise that a compromised dependency or operating system cannot use
the network.

The installed program has no automatic bug-report uploader. Release publishing
and Discord announcements are publisher-side tooling, not background client
telemetry. The static homepage source has no analytics script. The main host
configuration keeps ordinary web access/error logs; hosting providers and the
separate mirror can have their own infrastructure behavior.

Evidence: [translator_core.py](translator_core.py): OllamaClient,
[installer_runtime.py](installer_runtime.py): _service_environment,
_download_runtime, _install_model;
[app_updater.py](app_updater.py): _open, check_for_update;
[update_network.py](update_network.py),
[website source](sites/l2-chat-translator/index.template.html),
[host configuration](packaging/main-server/https.conf).

## What remains on the PC?

| Data | Retention and access |
| --- | --- |
| Original game log | Created/maintained by the game. The translator does not delete it. Other software with permission to read that file can read it. |
| Launcher and overlay text | Bounded history, 250 messages in each, in ordinary process memory. |
| Translation cache | Up to 128 worker entries in memory. Clear removes visible/pending results and invalidates stale work; cache entries can remain until eviction or process exit. |
| Copied replies | Windows clipboard, intentionally replaced after a successful submitted reply. Other software and enabled clipboard history/sync can access it. The app does not erase clipboard history. |
| Preferences | Local settings including chosen log path, model, target and overlay options. Managed-launcher preferences normally live under the Windows roaming application-data folder, with legacy-location support. Source-mode settings can differ; see app_paths.py. |
| Runtime/models | Dedicated chosen local data directory. Default is a Lineage2ChatTranslator folder under the user's home. Weights, manifests and verified runtime files persist for later use. |
| Operational logs and ownership records | Backend stdout/stderr logs under the managed data directory and shutdown/update diagnostics. Records bind cleanup authority to the account and directory identity. Paths, usernames, hardware details and exception text can appear in diagnostics. |
| Setup/update files | Inno/owned updater temporary files and local status records. Successful cleanup is scoped to recognized owned files; interrupted/unknown files may remain for review or retry. |

The app does not intentionally serialize displayed chat/reply transcripts.
That is not a guarantee that no text can ever reach disk: Windows paging,
crash dumps, clipboard history and upstream error paths are outside that claim.
The original game log already contains the source chat. Clear and process exit
are not secure erasure.

The launcher disables request-body logging and inherited debug options for its
backend. The pinned native runner still emits operational TRACE-level logs;
reviewed routine traces contain counters/cache metadata. Native startup logs
include the last four characters of a runner credential. That startup message
does not print the complete credential, but blanket claims that logs contain no sensitive
information would be inaccurate. Do not post raw diagnostics or model data in
public bug reports; use a synthetic example and redact local identifying data.

Evidence: [translator_app.py](translator_app.py): _worker, clear, save_settings,
service_shutdown_callback; [app_paths.py](app_paths.py);
[installer_runtime.py](installer_runtime.py): ensure_service;
[pinned runner launch code](https://github.com/ollama/ollama/blob/9a3e8177767245acc62b735eba7d5d8a553606d0/llm/llama_server.go)
and [native HTTP implementation](https://github.com/ggml-org/llama.cpp/blob/b11963/tools/server/server-http.cpp).

## Why loopback alone is insufficient

Other local programs can contact loopback ports. The current backend adds
controls beyond a local address:

1. A fresh random 32-byte session key is supplied through the owned frontend's
   environment, rather than command arguments or settings.
2. Every frontend route is authenticated before upstream route/body handling.
3. The Python caller checks the actual established connection's reverse TCP
   tuple against a retained owned process handle before sending credentials
   or message bytes. An earlier successful listener probe is insufficient.
4. Native model and discovery runners use separate random keys. Requests to
   model runners likewise check the actual connected process identity before
   sending bytes. Discovery consumes the owned process's output.
5. Trusted runtime hashes, private Windows permissions and held file/path
   handles make executable replacement a separate checked boundary.

Frontend traffic uses plaintext loopback HTTP, not TLS. Native /health,
/v1/health and empty OPTIONS responses remain public upstream; these do not
authorize inference or model management. The native web UI is disabled.

These protections assume a normally private Windows account/profile/Temp and
an uncompromised OS. They do not protect against malware already running as
the installing user, administrators, kernel compromise or every local denial
of service. Installation-folder repair does not repair an already shared
profile/Temp directory, prove an older writable install was never changed,
or revoke another process's already-open security-management handles.

Evidence: [local_service.py](local_service.py): ProcessIdentity, verify_socket,
PinnedHTTPConnection; [secure_storage.py](secure_storage.py);
[runtime_integrity.py](runtime_integrity.py);
[protected backend source and contract](packaging/secure-ollama/README.md).

## Can chat or model output execute commands?

Player text and model output are untrusted data. The code displays them as Tk
text and has no model-tool bridge, shell-evaluation path, automatic URL opener
for message content, or game-input path. The overlay styles its own windows
after checking their ownership; it does not install game hooks.

The parser, queues and model-response reads have explicit limits. Recognized
URLs/item labels and other protected spans are restored outside the model;
missing/duplicated markers cause bounded recovery or failure. URL and party
detail markers also retain source order; item markers can move for grammar. Reply copying
rejects hidden editing controls and validates a whisper recipient.

Prompt manipulation can still change translation quality. Protected markers do
not prove that all meaning, tone, negation or amounts are correct. Copying a
reply is an intentional information transfer to a shared Windows facility;
the user should review it before pasting.

Evidence: [translator_core.py](translator_core.py): parse_chat_line, translate,
_translate_fragments; [reply_composer.py](reply_composer.py): copy;
[chat_overlay.py](chat_overlay.py): _WindowsOverlay, _write_message.

## Updates and the first trusted download

From **1.7.12**, clients verify an independent RSA-3072 PSS/SHA-256 signature
over the exact release metadata bytes with a public key bundled in the app.
The metadata binds product, platform, version and installer name/size/SHA-256.
The application verifies before parsing. The bundled update helper verifies
again and holds the verified installer against replacement through execution.
HTTPS validation, fixed-origin/path checks and bounded downloads still apply.

A download-host-only compromise cannot sign a replacement installer for a
client that already trusts the authentic app/key. It can withhold updates,
make checks fail, or serve an older signed release that remains newer than the
installed version. There is no cryptographic guarantee of release freshness.

The first installation still requires a trusted installer/key. Versions before
1.7.12 cannot obtain independent first-upgrade protection merely because a
signature now exists on the same host. A checksum on the same compromised page
would not independently establish publisher identity.

**The installer has no Windows Authenticode publisher signature.** Independent
updater signatures are a different mechanism; Windows can still show an unknown
publisher/SmartScreen warning. No antivirus scan or absence of warnings is
claimed by this review.

The dedicated signing key stays in the publisher account's Windows key store
and is not deployed to either download host. The build machine/signing account
remain trusted. Non-exportable storage does not stop same-account malware
requesting signatures. Automatic key rotation and a portable recovery backup
are not implemented.

Evidence: [update_trust.py](update_trust.py),
[app_updater.py](app_updater.py),
[assets/apply-update.ps1](assets/apply-update.ps1),
[signing protocol and recovery limits](packaging/UPDATE-SIGNING.md).

## Deletion and permission changes

Updates remove obsolete program files only when paths and hashes match a
validated prior-release inventory. Unknown/modified ordinary files are kept;
modified retired loadable binaries block successful cleanup. The cleanup helper
runs outside the frozen application so it cannot load the DLL being removed.

Uninstall deliberately removes the application's registered downloaded models,
runtime, owned logs and preferences after confirmation. Sealed ownership records
and exact directory identities establish authority; settings or a familiar
folder name alone do not. Linked/redirected paths are refused. Unknown sibling
files and unconfirmed historical storage are retained. Uninstall cleanup failure
aborts program removal so it can be retried. Failed installer cleanup leaves the
translator closed for repair rather than silently reporting success.

The bundled PowerShell helpers run with ExecutionPolicy Bypass so a verified
local helper can execute. The updater does not download an arbitrary PowerShell
script. These helpers and native Windows APIs still deserve manual review
because their authorized job includes changing permissions or deleting files.

Evidence: [installation permission helper](assets/protect-install-directory.ps1),
[retired-file helper](assets/cleanup-program-files.ps1),
[installation_cleanup.py](installation_cleanup.py),
[uninstall_data.py](uninstall_data.py).

## Dependencies, models and source transparency

The public repository includes application/build/installer source, the maintained
backend patch, pinned source/toolchain records, tests, item-data provenance and
third-party notices. The runtime inventory covers all expected native members;
only the Ollama frontend is replaced by this project's protected build.

Model weights are pulled using supported catalog tags. They are **not pinned
to a publisher-reviewed model digest**. Registry-provided integrity checks do
not establish that the publisher independently reviewed the weights. Native
model parsers/GPU libraries and the Python/Tk/Windows stack remain trusted
dependencies and can have vulnerabilities.

Pinned archives, go.sum, native provenance checks and security tests establish
particular integrity/provenance properties. They do not establish absence of
known or unknown vulnerabilities. The build does not include a comprehensive
dependency vulnerability scan or Go govulncheck; Python build requirements are
version pinned without pip --require-hashes. See
[Go's separate vulnerability tooling](https://go.dev/doc/security/vuln/).
No claim of a clean CVE inventory is made here.

SOURCE-MANIFEST.json identifies the published installer and hashes the matching
application inputs. Documentation/tooling may advance on main while immutable
version tags preserve application/installer identity. These records make
comparison possible; they do not independently prove that published executable
bytes were produced from those inputs. A byte-for-byte reproducible build or
independent binary/source attestation has not been established. Official
retirement metadata also depends on historical verified installers, and official
release preparation requires a private signing identity absent from GitHub.

The source publication tool uses an explicit allowlist and deidentifies known
fixtures. Settings, logs, model weights, credentials, private review reports and
compiled packages are excluded. Automated privacy scans have limits; a manual
review is still required when adding public files. Existing third-party licenses
are preserved; this overview does not add a license to the original app code.

Evidence: [build.py](packaging/build.py),
[build_environment.py](packaging/build_environment.py),
[runtime manifest](assets/secure-ollama-manifest.json),
[backend build](packaging/secure-ollama/build_secure_ollama.py),
[model catalog/install code](installer_runtime.py),
[source publication](packaging/PUBLIC-SOURCE.md),
[third-party notices](packaging/THIRD-PARTY-NOTICES.txt).

## What this review verified

The review traced application, installer, source-startup, local transport,
protected-backend patch, updater, storage/deletion and public-release code.
Independent parallel reviews checked the translation/privacy, backend/storage
and update/cleanup boundaries. Important functions already have trust-boundary
docstrings and named limits; CODEMAP.md explains their order and relationships
without changing the verified application inputs for cosmetic comments.

The v1.7.13 security-review regression run completed **982 Python tests, with 10 skipped**. Platform
skips and mocks limit the evidence. The offline diagnostic also exercises the
existing synthetic self-test rather than authenticating to another running
launcher's session. Live game behavior, new model inference, interactive install
or upgrade testing, adversarial fuzzing, broad native/dependency analysis,
antivirus scanning and deployed-server penetration testing were not repeated
for this review. The separate backend build's focused Go tests were inspected,
not rerun or included in that Python test count.

Reviewers can run the commands and follow the code/test map in
[CODEMAP.md](CODEMAP.md). A successful test run supports those tested cases;
it is not a guarantee that the program is risk free.
