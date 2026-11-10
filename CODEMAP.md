# Code reading guide

This guide follows the v1.7.14 application in execution order. It names the
functions to inspect, their inputs and side effects, and the checks protecting
each boundary. Read [SECURITY.md](SECURITY.md) for the threat model and limits.
The public README is generated from
[its template](packaging/public-source/README.template.md); the private
development diary is not the public application's specification.

## Start here

The installed entrypoint is [installer_main.py](installer_main.py). Source
launchers also delegate to it. Translation does not begin in a hidden service
or website script.

    installer_main.main
      -> SingleInstance + update/uninstall guards
      -> SetupWindow: validate storage/runtime/model, start owned backend
      -> translator_app.main -> TranslatorApp
           -> LogTail -> parse_chat_line -> translation queue
           -> OllamaClient -> authenticated local backend -> local model
           -> result queue -> launcher + overlay
           -> ReplyComposer -> local translation -> Windows clipboard
      -> close: finish owned-service shutdown before exit/update handoff

Worker threads perform model/download work and return results through queues.
Tk callbacks poll the log in bounded batches and render/update controls on the
UI thread; recent-chat reads also occur there.
Generation and revision checks discard work that belongs to a cleared history,
old reply draft, cancelled operation or closed window.

## 1. Entry, configuration and setup

| File and symbols | Responsibility and side effects |
| --- | --- |
| [installer_main.py](installer_main.py): main, SingleInstance | Routes command-line modes before creating windows. Normal setup/launch holds an instance mutex. Update/uninstall guards prevent conflicting work. |
| installer_main: self_test, diagnose | Self-test checks imports, Tcl, native libraries, synthetic parsing, item data and bundled helper/backend integrity without network or windows. Diagnose reads configuration and hardware metadata. Output can contain local paths. |
| installer_main: validate_selection, save_configuration | Restricts model choices, validates the dedicated data folder and resolves an existing game log. Saves preferences/runtime location with rollback if the second write fails. |
| installer_main: SetupWindow.install, launch_existing, _start_work, _drain | Owns setup workers, cancellation tokens, progress and successful handoff to the translator. A cancelled setup waits for owned-service cleanup. |
| installer_main: SetupWindow.finish, close | Retains exact-child ownership through setup failures and the translator's lifetime. Shutdown precedes release of the instance mutex. |
| [app_paths.py](app_paths.py): preferences_directory, settings_file, runtime_file, default_data_directory | Keeps preferences separate from installed code. Reuses recognized legacy preferences. Source-mode settings_file differs from frozen mode; the managed launcher supplies its preferences to the UI. |
| [game_log_setup.py](game_log_setup.py): resolve_system_log | Resolves an existing system folder/log; does not enable logging, modify game configuration or create a game log. |
| [app_version.py](app_version.py), [app_icon.py](app_icon.py) | Shared release version and bundled window artwork. |

Read the modes in main before running a helper: --self-test is passive;
--uninstall-cleanup removes managed data. --install-model opens setup and can
download/start the local backend. These are materially different operations.

## 2. Runtime installation and ownership

[installer_runtime.py](installer_runtime.py) contains RuntimeManager and the
catalog of supported models. Read its service-environment builder, runtime
installation, model installation and start/stop methods together rather than
treating a mutable installation receipt as a trust anchor.

1. Guard the dedicated storage tree and establish or verify its ownership.
2. Verify the official native-runtime archive against the pinned SHA-256.
   Extraction validates member names, sizes and layout.
3. Replace the archive's frontend with the separately verified protected
   frontend bundled in the app. Check the complete trusted runtime inventory.
4. Start the exact verified executable with a fresh session key and a controlled
   environment. Register the retained frontend process identity in this Python
   process. The service's stdout/stderr are local operational logs.
5. Pull the selected supported model through the authenticated local API when
   needed. Model tags are catalog choices, not publisher-pinned model digests.
6. Stop only the owned process tree; do not kill every process named Ollama or
   adopt an unrelated listener because it answers on the expected port.

Supporting modules:

| File and symbols | Why the check exists |
| --- | --- |
| [runtime_integrity.py](runtime_integrity.py): load_manifest, pin_runtime, verified_runtime, verify_bundled_frontend | Trusted file hashes come from the bundled manifest. Verify all runtime members, reject unexpected loadable files, and retain checked handles so paths cannot simply be replaced after hashing. |
| [secure_storage.py](secure_storage.py): guard_private_tree | Inspect owners, ACLs, links, hard links and ancestor permissions. Hold the relevant handles while using protected storage. allow_locked_ancestors permits broader ancestor ACLs while handles pin ancestry; the owned root remains private. An arbitrary chosen path does not authorize recursive repair or deletion. |
| secure_storage: seal_record, verify_record, path_identity | Bind managed-data ownership records to the Windows account and exact directory identity. This is distinct from ordinary user preferences. |
| secure_storage: locked_file, locked_directory, safe_remove_tree | Coordinate path validation with file use/removal. Guarded removal rejects redirected trees. Callers must first establish which tree they own. |
| [ollama_service.ps1](ollama_service.ps1) | Compatibility environment/owner-inspection helpers. Source launchers no longer use it to start a second unauthenticated backend. |

## 3. Local request authentication

[local_service.py](local_service.py) is the Python-side transport boundary.

    register_session(endpoint, owned_process, verified_executable, random_key)
      -> ProcessIdentity: retain a live process handle and inspect its account
      -> ServiceSession: retain this launcher's credential, no disk sharing
      -> PinnedHTTPHandler -> PinnedHTTPConnection.connect
           -> establish TCP connection
           -> verify_socket: reverse address/port tuple belongs to owned PID
           -> only then allow HTTP headers and body to be sent

The order matters. Testing a listening port earlier would not establish who
owns a later connection. get_session rejects a caller without the registered
session; it does not discover a secret from settings or borrow another
launcher's credentials. forget_session removes the registration on cleanup.

The corresponding Go implementation is public under
[packaging/secure-ollama](packaging/secure-ollama/README.md):

- overlay/l2ctsecurity/security.go authenticates frontend requests before
  upstream route handlers and supplies authenticated runner transports.
- overlay/l2ctsecurity/process_windows.go checks Windows socket/process identity
  for the actual runner connection before sending HTTP bytes.
- patch_source.py applies exact-context changes to the pinned upstream source:
  frontend guard, distinct runner keys, process retention, authenticated
  discovery and Windows process-tree cleanup.
- build_secure_ollama.py verifies source/toolchain/archive pins and Go module
  checksums, runs focused security tests and produces the frontend/manifest.

This is ordinary HTTP over IPv4 loopback. Authentication and process identity
checks protect a specific local trust boundary; they do not create a sandbox
against malware already running as the installing Windows account.

## 4. Incoming chat: file to displayed translation

The critical pipeline is in [translator_core.py](translator_core.py) and
[translator_app.py](translator_app.py).

1. _open_shared opens the selected log for reading with Windows sharing flags
   compatible with the game's writer. LogTail handles encoding, incomplete
   records, truncation and replacement. It normally starts at the end.
2. parse_chat_line recognizes incoming S_SAY2 records and validates their size
   and channel fields. ChatRecord carries timestamp, sender, channel, text and
   protected link metadata. It does not represent an instruction to execute.
3. decode_chat_links resolves item ClassID values through the offline catalog.
   Party-link descriptions remain translatable while level ranges/player
   counts are kept separately. Per-instance item IDs are not catalog lookups.
4. TranslatorApp._poll_log and enqueue submit bounded work. _worker calls the
   model with message text. It does not add record sender/timestamp/channel
   fields to the incoming translation prompt. It also identifies source
   language for reply selection.
5. OllamaClient validates a loopback endpoint and local model choice.
   _request uses the pinned local transport, bypasses proxies, refuses
   redirects and caps the response read. Remote-backed model aliases are
   refused.
6. OllamaClient.translate protects recognized URLs, item labels, relevant
   abbreviations and party details before requesting translation. It requires
   each expected marker exactly once; URL and party-detail markers also retain
   source order. Item markers can move for grammar. Protected source spans are
   restored outside the model. Protected-only/item-price-acronym listings can
   return unchanged; ordinary English text still goes through inference.
7. If English output fails validation, bounded recovery, including
   _translate_fragments, may retry smaller text while retaining protected
   pieces. Price protection is added for eligible English recovery. It cannot
   retry indefinitely. Replies to other target languages
   fail instead of using recovery that could produce a misleading mixture.
8. The worker returns a result event. _drain_events ignores stale generations;
   _append renders the launcher and passes the same record to the overlay.
   Text remains Tk text, not HTML, script, a shell command or an automatic link.

[game_slang.py](game_slang.py): game_glossary supplies context hints rather than
executing player text. [item_catalog.py](item_catalog.py): ItemCatalog validates
the attributed bundled dataset; resolve_title/get_item_name perform offline
lookups and preserve unknown titles instead of inventing canonical names.
[tools/import_item_catalog.py](tools/import_item_catalog.py) is a development
import/provenance tool, not a runtime web lookup.

The principal bounds are named near their owning code: 80 pending translations,
250 launcher rows, 250 overlay messages, 128 cached translations, bounded log
reads and 1 MiB model responses. Clear invalidates visible and queued results;
the worker cache can remain until eviction or exit. It is not memory erasure.

## 5. Replies, overlay and window APIs

[reply_composer.py](reply_composer.py):

    select -> draft/revision tracking -> translate -> ReplyJob
      -> _worker -> _execute_job -> local model
      -> _drain_events -> accept only current selection/draft -> copy

select can choose a language or exact whisper recipient from a displayed
record. A submitted reply is a fresh message; it does not inherit an earlier
party advertisement's numbers. _drain_events rejects editing controls and
flattens whitespace; copy checks the result still matches the current context
before replacing Windows clipboard contents. A successful submitted translation
copies automatically. The app does not paste it into the game or send keys.

[chat_overlay.py](chat_overlay.py): OverlayHistory stores bounded messages;
channel_is_visible uses numeric channel IDs. Filtering affects display, not
which enabled messages the launcher translates. _WindowsOverlay checks its
window belongs to this process before applying native styles/placement.
ChatOverlay renders text, handles resize/move/click-through settings and routes
reply selections back to the launcher.

[overlay_tray.py](overlay_tray.py): tray_geometry and choice_columns calculate
layout; ChannelTray manages the attached filter window, animation and selection
callback. Neither module hooks the game or reads its memory/screen.

TranslatorApp.save_settings saves known preferences rather than transcripts.
_open_discord opens a fixed invite on a click without appending diagnostics.
close signals workers to stop; it does not join every translation thread.
_finish_shutdown waits for owned-backend shutdown before committing an update
handoff.

### Optional system tray

[tray_controls.py](tray_controls.py): TrayControls listens only to the
launcher's own minimize/map/configure/destroy events. The saved
minimize_to_tray flag defaults to false. A deferred iconic-state check starts
the native icon; only a successful ready event permits withdrawing the root.
Callbacks carry instance generations through a queue, so a late event cannot
hide a restored/closing window. Overlay replies reuse restore. Hiding never
stops the log reader, model session or overlay.

[windows_tray.py](windows_tray.py): NativeTray owns a hidden top-level Windows
message window, existing L2CT.ico handle, notification icon and fixed popup menu
on one native thread. It handles click/Open/Exit and Explorer's TaskbarCreated
message. No game window, global hook, network or model data is involved.
Construction/import creates no UI. Native callbacks enqueue plain events;
Tk consumes them. Failure returns the launcher to ordinary window/taskbar access.
Closing requests native teardown, then the shutdown worker waits for cleanup
before update handoff. The X and tray Exit use the real close path.

Headless tests mock Windows APIs; they do not demonstrate actual tray appearance,
click behavior or Explorer recovery on every supported desktop configuration.

## 6. Updates: check, prepare, commit, execute

Read these in order:

1. [update_controls.py](update_controls.py): UpdateControls.settings_shown/check
   runs a background metadata check. Installation requires the install control.
   _prepare/_publish/_drain_events coordinate cancellation and UI generations.
2. [app_updater.py](app_updater.py): check_for_update downloads metadata and its
   detached signature from fixed HTTPS paths. _signed_release authenticates
   exact bytes before parsing, rejects duplicate fields and validates product,
   platform, version and exact installer identity. A check never runs an
   installer.
3. [update_trust.py](update_trust.py): verify_release_signature/_verify_with_key
   uses the bundled RSA public key and native Windows cryptography. A key
   supplied by the download host is not accepted.
4. [update_network.py](update_network.py): NetworkOperation applies deadlines
   across connection, TLS, headers and body, including cancellation of blocked
   sockets. This updater's numeric host avoids a DNS stage. Read caps supplement
   size headers; requests refuse proxies and redirects.
5. app_updater.prepare_update validates the registered installation and creates
   an owned private stage. It downloads/verifies installer size and hash without
   executing it. It copies the installed helper rather than fetching a script.
6. After preference saving and clean backend shutdown, launch_update locks
   helper/path identities and negotiates readiness and commit. Failure before
   commit cancels the handoff.
7. [assets/apply-update.ps1](assets/apply-update.ps1) waits for the retained
   original process to exit, verifies the version and signed metadata again,
   and holds the checked installer against write/delete through execution.
8. Inno installs the files under its permission/cleanup gates. A successful
   headless self-test precedes cleanup of owned temporary files and one launcher
   restart. Installation failure or a required reboot does not blindly reopen
   installed code.

The helper is separate so the translator can release the files being replaced.
Read [UPDATE-SIGNING.md](packaging/UPDATE-SIGNING.md) for bootstrap, key custody,
replay/freshness and recovery limits.

## 7. Installation, obsolete files and uninstall

[packaging/installer.iss](packaging/installer.iss) uses a per-user, non-elevating
install. PrepareToInstall protects the application tree before replacement;
CurStepChanged verifies it afterward, runs external retired-file cleanup, then
permits model setup/launch. Failed gates return failure rather than report a
completed setup. Uninstall runs managed-data cleanup before removing the app.

| Implementation | Authority and scope |
| --- | --- |
| [assets/protect-install-directory.ps1](assets/protect-install-directory.ps1): Invoke-InstallDirectoryProtection | Preflight the owned application tree and its ancestors. Restrict that tree to the installing account, SYSTEM and Administrators; reject unsafe paths/links/owners. Never repair parent/profile/Temp permissions. |
| [installation_cleanup.py](installation_cleanup.py) | Validate safe Windows-relative paths, current/retired overlaps and historical hash plans. Python cleanup is useful for isolated checks; it is not the frozen installer's DLL-removal entrypoint. |
| [assets/cleanup-program-files.ps1](assets/cleanup-program-files.ps1): Invoke-RegisteredCleanup | Actual installer cleanup, outside frozen Python so it cannot load the retired DLL. Bind to the registered install, hold guards and remove only retired matching bytes. Modified retired loadable binaries block completion. |
| [uninstall_data.py](uninstall_data.py): register_data_root, storage_confirmation_identity, uninstall_data | Record sealed directory ownership; require exact-root confirmation before legacy adoption. Preflight registered roots, stop only the owned backend and remove managed children/preferences. Settings alone do not grant deletion authority; unknown siblings/unconfirmed history are retained. |

Do not conflate three deletion operations: replacing obsolete program files,
removing owned temporary update downloads, and uninstalling registered model
data. Each has its own ownership rules and a different target.

## 8. Build and public release

These tools operate on a publisher's development workspace, not in the installed
translator's normal execution path.

| Tool | What to inspect |
| --- | --- |
| [packaging/bootstrap-tools.ps1](packaging/bootstrap-tools.ps1) | Versioned Python build dependencies; pinned compiler archive and upstream signatures. Python requirements are version pinned without a hash-locked pip file. |
| [packaging/build.py](packaging/build.py) | SOURCES/ASSETS explicitly select shipped inputs. Run regressions, stage fresh source, freeze, verify native origins/layout, run packaged self-test, compile installer and prepare site from one manifest. |
| [packaging/build_environment.py](packaging/build_environment.py) | Isolated Python/PATH, approved dependency roots and complete DLL/PYD provenance. Windows UCRT is supplied by Windows; the expected nine application DLLs are explicit. |
| [packaging/payload_layout.py](packaging/payload_layout.py) | Exact current payload and license list, private/generated-file rejection, verified earlier-release hashes for retirement metadata. No historical executable payload is copied forward. |
| [packaging/sign_release.py](packaging/sign_release.py), [sign-release.ps1](packaging/sign-release.ps1) | Sign exact public metadata locally with the dedicated Windows key. Provision is explicit; ordinary signing never replaces the key. |
| [packaging/release_site.py](packaging/release_site.py) | Match source/assets/installer to the successful manifest. Render release tokens/changelog and retain immutable versioned downloads. Check mode does not sign or publish. |
| [packaging/source_release.py](packaging/source_release.py) | snapshot constructs an allowlisted, scanned, deidentified candidate before editing the source-only clone. publish_source verifies existing history/tag identity, pushes atomically without force and checks public GitHub blobs. |
| [packaging/export_main_server.py](packaging/export_main_server.py) | Production export requires source publication first. Emit only the verified public static tree and sidecar checksum inventory. |
| [packaging/main-server/deploy_release.py](packaging/main-server/deploy_release.py) | Server verifies bundle and exact file set, preserves immutable downloads and switches the static release atomically. It does not change game/database/web service configuration. |
| [packaging/main-server/discord_release.py](packaging/main-server/discord_release.py) | Publisher-side release announcement after public verification, using private server configuration. Not installed bot code or client telemetry. |
| [packaging/complete_release.py](packaging/complete_release.py), [release_cleanup.py](packaging/release_cleanup.py), [release_lock.py](packaging/release_lock.py) | Fresh verification of both public hosts and source/tag precedes narrowly scoped old build-cache pruning. Shared lock excludes active build/export work. |

The website is static source under [sites/l2-chat-translator](sites/l2-chat-translator/README.md).
Hosting identity is public configuration, not a credential. Hosting configuration
files describe the main server; they do not prove its current deployed state.

## 9. How to check the explanations

Run the headless suite from the repository root on Windows:

    .build-tools/venv/Scripts/python.exe -B -m unittest discover -s tests
    .build-tools/venv/Scripts/python.exe -B verify_local.py

The first command includes mocks, synthetic files, isolated local HTTP fixtures
and platform-specific Windows checks. The second reuses the offline self-test;
it cannot attach to another launcher's process-local authenticated session.
It requires the built protected frontend to be present in a source checkout.
Neither is a live game, real model or interactive upgrade test.

| Review question | Main regression files under tests/ |
| --- | --- |
| Does malformed chat stay data? | test_translator_core, test_source_url_protection, test_url_restore_pipeline, test_party_links, test_item_link_catalog |
| Can stale replies copy text? | test_reply_composer, test_whisper_integration |
| Are histories/filtering and window behavior bounded? | test_chat_overlay, test_overlay_channel_filters, test_overlay_tray, test_overlay_integration |
| Can minimizing strand the launcher or interrupt shutdown? | test_tray_controls, test_windows_tray, test_app_update_integration |
| Can another listener obtain credentials? | test_local_service, test_ollama_service; Go overlay security/process_windows tests |
| Can runtime files or storage redirect execution/deletion? | test_runtime_integrity, test_secure_storage, test_installer_runtime, test_install_directory_security |
| Can host metadata replace trusted update code? | test_update_trust, test_app_updater, test_update_runner, test_update_network, test_update_controls |
| Can cleanup remove unrelated/modified files? | test_installation_cleanup, test_external_cleanup, test_locked_dll_cleanup, test_uninstall_data |
| Can private files leak into public releases? | test_payload_layout, test_build_environment, test_source_release, test_release_site, test_main_server_export |
| Can release failure cause premature cleanup/announcements? | test_complete_release, test_release_cleanup, test_release_lock, test_discord_release, test_discord_deploy_hook |

Tests establish their asserted cases only. Comments, this guide and a successful
test run are evidence to inspect alongside the code, not a security certificate.
