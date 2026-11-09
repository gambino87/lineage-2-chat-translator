# Build the Windows installer

Use Windows 10/11 x64 and Python 3.12 x64 with Tcl/Tk. From the project root:

```powershell
pwsh -NoProfile -File packaging/bootstrap-tools.ps1
.build-tools/venv/Scripts/python.exe packaging/secure-ollama/build_secure_ollama.py
.build-tools/venv/Scripts/python.exe packaging/build.py
```

The bootstrap installs pinned Python build dependencies into a project-local
virtual environment. It extracts a portable Inno compiler package, verifies its
digest and upstream Authenticode/ECDSA signatures, and collects upstream license
notices. It does not install the Inno application or register file associations.

The protected Ollama frontend has a separate pinned source/toolchain build; see
[its build and security notes](secure-ollama/README.md). Its generated executable
is bundled under `_internal/secure-runtime/ollama.exe`, verified against the
public `assets/secure-ollama-manifest.json`, and covered by the signed installer.
The source repository contains its patch, build recipe, hashes and licenses, not
the compiled executable. GPU libraries and model weights remain separate setup
downloads. Building the backend does not start it or download models.

Runtime files are validated against that bundled inventory, never against an
editable receipt beside the executable. File handles and dedicated-folder ACLs
remain held while the service runs. Owner access is retained; ordinary in-use
file locks end on shutdown. Cleanup deletes checked Windows file objects through
handles, refusing junctions and hard links instead of following them.

Cleanup ownership is separate from executable authenticity. New records bind the
chosen folder's Windows file identity to a current-user DPAPI integrity seal.
Their ordinary JSON paths remain readable; owners can edit or delete the files,
but changed ownership records must be reviewed again in Configure before they
authorize deletion. Mutable legacy custom paths are never promoted just because
a genuine runtime is present. One explicit confirmation adopts only the displayed
dedicated folder; unconfirmed historical paths are archived for manual review.

The launcher and patched backend use ephemeral credentials and verify each
actual connected socket's process before transmitting requests. The main API,
model runners and temporary discovery runners are authenticated. This separates
other Windows accounts; it cannot protect against owner-account malware or an
administrator, and does not promise immunity to local resource-exhaustion attacks.

New installations use Inno's `{autopf}` with `PrivilegesRequired=lowest`, resolving
to the current user's Programs folder. Upgrades retain their registered location:
older updater helpers explicitly pass `/DIR` and reopen the original executable,
so silently moving an existing installation would break their handoff.

Before file replacement, setup extracts `protect-install-directory.ps1` into its
private temporary directory and runs native Windows PowerShell there. The helper
restricts the owned application tree to the current user, SYSTEM and Administrators,
including legacy installs at `C:\Lineage 2 Chat Translator`, and checks the tree
again before launching installed code. Unsafe paths, ownership, links or ancestor
permissions stop setup; it does not broaden permissions, elevate, or change a
parent folder's ACL. This closes the program-folder write-permission exposure;
it does not attest that a previously writable installation was never modified
or revoke another process's already-open security-management handles.

Like other per-user installers, setup assumes the Windows profile and temporary
directory are private to the account. Inno's non-admin temporary directory can
inherit its parent's permissions. This app-folder repair does not secure an
already-shared profile or Temp folder; those permissions need separate repair.
It never rewrites profile, Temp or other parent ACLs.

Each build uses a fresh `.build/<version>-<UTC timestamp>/source` folder containing
only explicitly listed application modules, icon assets, the item catalog with
its attribution notice, and the trusted update and cleanup helpers. Personal settings, logs,
review data, development scripts, tests and model weights cannot enter this
source staging directory. PyInstaller discovers Python/Tcl/Tk dependencies.

Setup uses a fresh cancellation token per worker operation. Controlled HTTP
connections register their sockets before proxy negotiation, TLS and response
headers, so a watchdog can interrupt blocked reads without closing a buffered
response on the UI thread. Runtime transfer and model pull budgets are one and
two hours respectively; setup's sample verification shares a 180-second budget
across requests. Windows service-owner checks may still take up to 20 seconds;
native DNS resolution follows the OS resolver timeout. Cancellation waits for
owned-service cleanup, which deliberately ignores the cancelled token. Model
layers and verified runtime archives are retained; incomplete runtime ZIPs are
not resumable and are removed. Headless tests exercise stalled and trickling
local HTTP fixtures, cancellation races, retry and exact-child cleanup without
using a real Ollama service or model.

Native dependency discovery uses isolated Python (`-I`), an explicit PATH limited
to the pinned interpreter/venv and Windows directories, and a separate cache for
each build. `build_environment.py` validates binary origins against those roots
and excludes only Windows-provided `api-ms-win-core-*`, `api-ms-win-crt-*` and
`ucrtbase.dll` before collection. The nine required application/VC runtime DLLs
remain bundled. Every collected DLL/PYD must match the complete audited inventory
and its source checksum. Unexpected native files or changed origins fail the build.

The frozen self-test uses an empty working directory and OS-only PATH, with no
Python, venv or development-tool fallback. It exercises Tcl, TLS, hashing and
compression, verifies that UCRT loaded from Windows System32, and rejects an
obsolete app-local ucrtbase.dll even if the system copy is also loaded. The Windows
10/11 minimum is unchanged. See Microsoft's [UCRT deployment guidance](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment?view=msvc-170).

`verify_dll_upgrade.py` additionally validates the 52-to-nine transition using
isolated copies under `.build`: overlay the new payload, run the real cleanup,
check all 43 retired DLLs, verify current/preserved bytes, and run the upgraded
frozen self-test. It never modifies the registered installation. This headless
check does not claim interactive testing across every supported Windows version.

The frozen payload is checked against its expected root, assets and license
layout. License staging copies only the six required notices. Inno receives an
exact generated file list rather than a recursive wildcard, and the payload is
checked again after compilation. Historical installers and development data
cannot enter through an old build folder or an extra license/asset backup.

The build verifies earlier release installers against their manifests, then
generates `_internal/assets/obsolete-program-files.json`: current file paths and
hashes of retired program files, not old binaries or user data. Windows path
comparisons are case-insensitive, including ancestor conflicts. The generated
plan is included in the payload manifest and the packaged self-test validates it.

After current files are installed, both manual and automatic installs extract
`assets/cleanup-program-files.ps1` from the trusted installer into Inno's private
temporary directory. The installer first clears any app-specific DLL search
directory inherited from an older updater. Native Windows PowerShell runs the
helper hidden from the neutral temporary directory; the installer waits for it before model setup or app restart. The
frozen translator never performs this cleanup: loading it can pin the same old
DLL it needs to remove. Its deprecated cleanup switch now returns a retry message.

The helper holds the app instance guard, binds its target to the registered
installation, validates the complete retirement plan and removes only unchanged
retired files whose hashes match an earlier release. It rejects links, junctions,
path escapes and current-file overlaps, and only removes empty nested folders.
Models, settings, logs, uninstaller files and changed/unrecognized files are
preserved. A failure returns a nonzero installer result with a retry instruction;
rerunning safely retries missing or previously locked files. This is independent
of the updater's cleanup of its owned temporary download and helper files.

`verify_locked_dll_cleanup.py` tests the actual Windows DLL-lock case in isolated
copies under `.build`: a child process maps the old runtime, deletion is refused
while loaded, and cleanup succeeds after the child exits. It checks both a full
old payload and a partial upgrade with only ucrtbase.dll left, verifies current
and preserved bytes, and runs the new frozen headless self-test. It does not
close or replace the user's running translator or modify its registry entries.

`assets/L2CT.png` is the supplied artwork. `assets/L2CT.ico` contains Windows icon
sizes from 16 through 256 pixels; regenerate it with `packaging/make-icon.ps1`
after replacing the PNG. Its native-size frame preserves the original PNG bytes.
Both assets are explicitly staged, bundled and recorded with hashes. The ICO is
embedded in the program and installer and referenced by the installed shortcuts.
Tk roots use the bundled icon as the default for their child windows.

`assets/item_catalog.json` is a separate attributed item-name dataset; its source
revision and XML checksums are embedded. `tools/import_item_catalog.py` reproduces
the catalog from its pinned public source. The bundled self-test checks item
counts and resolves the reported Chinese links without a model or network.
The item-data license is included as `licenses/L2J-ITEM-DATA.txt`.

`assets/apply-update.ps1` is copied from the installed application for update
handoff; it is never fetched as executable script from the website. The packaged
self-test checks that this helper is included. From v1.7.12, independent release
signatures are required in both the application and helper before installation.
Windows cryptography performs RSA-PSS verification without adding a crypto
package or bundled DLL. See [UPDATE-SIGNING.md](UPDATE-SIGNING.md) for the
signing protocol, local key custody, first trusted upgrade and recovery limits.
Update tests use fake downloads,
processes and windows; no installed application is replaced by the build. A live
upgrade test requires explicit permission and a controlled installed copy.

The script invokes the frozen app with `--self-test --output <file>` and compiles
the installer only after it exits successfully. It does not create windows,
download a runtime/model, start a service, or install the resulting application.
Outputs are written to `release/<version>/`. `.build/latest-build.json` points
to the exact frozen app and build folder for further headless validation.

## Release installer and homepage together

For a new app release, increment `app_version.py` and add an entry to
`sites/l2-chat-translator/changelog.json`. Use a list of objects with `version`
and `changes` (a list of plain-text notes), newest first, keeping earlier entries.
The newest changelog version must match the app version. The build checks this
before compiling; Site preparation also checks and renders the release history.
The normal build first runs the regression suite, then builds and checks the
installer and prepares `sites/l2-chat-translator/dist/` from the same release
manifest. Homepage version, all installer links, size, checksum, signing status
and default model details are generated together. Checksums remain in the download
metadata, without a technical verification section on the homepage. Edit feature descriptions in
`sites/l2-chat-translator/index.template.html`, not the generated `dist/index.html`.

To prepare/check an existing unchanged release without rebuilding it:

```powershell
.build-tools/venv/Scripts/python.exe packaging/release_site.py
.build-tools/venv/Scripts/python.exe packaging/release_site.py --check
```

Preparation rejects stale source/icon hashes, failed packaged self-tests,
changed installer bytes and unexpected public files. Older versioned downloads
are retained. A prepared/published version cannot be rebuilt under the same name.
The existing release manifests are required to verify retained downloads.

The build **prepares** files; it does not publish them. Export the verified bundle
with `packaging/export_main_server.py`. That production command first updates
and verifies the public GitHub source snapshot and immutable release tag; source
publication failure blocks export. See [PUBLIC-SOURCE.md](PUBLIC-SOURCE.md).
Then publish atomically to the main server
following `HOSTING.md`. Run the check above immediately before export. Deploy the
homepage and installer together, then verify the public installer checksum.
The original Sites mirror is separate and retains its existing identity/audience.

Interactive installation waits for `--install-model` before its completion page.
That mode returns only after successful model verification, owned archive cleanup,
preference saving and service shutdown, or returns nonzero if setup is canceled.
Uninstall invokes `--uninstall-cleanup` after the standard removal confirmation
and before program files are removed. The helper holds the app instance lock;
failure aborts program removal so cleanup can be retried. Data deletion is scoped
by per-user registries and ownership records, with path/link checks.

Inno event ordering and failure handling follow the official
[event documentation](https://jrsoftware.org/ishelp/topic_scriptevents.htm),
[Exec](https://jrsoftware.org/ishelp/topic_isxfunc_exec.htm) and
[Abort](https://jrsoftware.org/ishelp/topic_isxfunc_abort.htm) references.

An existing official compiler can be supplied with `--iscc C:/path/ISCC.exe`.
`--skip-installer` builds and checks only the frozen application.

Primary references: [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html),
[Inno compiler CLI](https://jrsoftware.org/ishelp/topic_compilercmdline.htm),
[per-user installation](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm),
[download verification](https://jrsoftware.org/isdl-verify.php).
