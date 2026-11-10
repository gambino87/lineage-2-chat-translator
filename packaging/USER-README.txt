LINEAGE 2 CHAT TRANSLATOR 1.7.16

Windows 10/11, 64-bit. Python is included; no separate Python installation is needed.
The supplied L2CT artwork is used for the program, shortcuts and window icons.

PT-BR STREAM PROMOTIONS

English translations include contextual help for stream promotions ending in
"so vem o pai ta on" ("come join me, I'm live"), including accented forms.
This improvement is limited to promotional endings; other mixed-language
messages can still translate incompletely.

MINIMIZE TO SYSTEM TRAY

Enable Minimize to system tray in the launcher's Settings, then use the normal
minimize button. The existing app icon appears in the Windows notification area
(it may be under the hidden-icons arrow). Translation and the overlay keep running.
Click the icon to reopen the same launcher, or right-click for Open translator
and Exit. Exit and the window's X close the translator and its owned local service.
The setting is off by default and saved immediately. If Windows cannot create
the icon, the launcher stays accessible instead of disappearing.

LOCAL SERVICE AND FILE ACCESS

Your Windows account keeps full access to application data. You can inspect,
change or remove it yourself after closing the translator. The app protects only
its own folders from other accounts; it does not change parent-folder permissions.
Normal temporary file locks remain while the service or an update is using files.
Older storage with unprotected ownership records requires one explicit review in
Configure. Confirm the displayed model folder only if it belongs to this app.
Other unconfirmed historical folders are preserved for manual review; an edited
old cleanup record is never treated as permission to delete unrelated files.
Ownership records remain readable JSON. If you manually edit them or move their
folder, Configure asks you to review that location again before automatic cleanup.
This does not prevent normal edits to your translation preferences.

The installer includes a protected Ollama frontend. Setup downloads the official
GPU libraries and your selected model separately. Existing verified runtimes are
upgraded without downloading models again. Unrecognized executable/library changes
are preserved and stop startup; use a clean data folder or restore official files.
Both local service connections require private launch credentials and verify the
connected process before sending chat. These protections do not defend against
malware running as your Windows account or an administrator.

FEEDBACK AND BUG REPORTS

Use the Discord link below the launcher's reply box, or visit
https://discord.gg/V4ph4Ww5m to post feedback and report bugs.

OVERLAY CHAT FILTERS

Click Channels on the overlay to slide open its filter tray. For only Say and
Party, click None, then select Say and Party. Changes apply immediately
and the channel option names use their chat colors in both the tray and Overlay
settings. Command channel and Other / notices use the normal chat fallback color.
Selections are saved for the next launch. All restores every channel. Other / notices
covers remaining channels; system notices also need Include notices in Settings.
The launcher transcript keeps all received chat. Hidden messages remain in the
recent in-memory history, so reselecting a channel restores its retained rows.
Click Channels again, the tray's close button, or Escape while using the tray
to collapse it. The same selections are available in Overlay settings.

Drag the overlay's top-right grip to resize it while keeping its bottom-left
corner fixed. Hiding or locking the overlay also closes the channel tray.

The launcher now includes nine DLLs instead of 52. Windows 10/11 supplies the
compatibility runtime, so 43 redundant copies are omitted. Updating removes
unchanged copies from earlier releases automatically. Python, interface,
encryption and Visual C++ support libraries remain bundled.

PARTY AND ITEM LINKS

Party recruitment links show their translated description while preserving
level ranges and player counts exactly, for example:
Looking for a healer [12~19] (8/9)
Linked items still use their English catalog names, even in the same message.
An unreadable party title appears as [Party link], not [Linked item]. Links are
displayed as text; the overlay does not join parties or interact with the game.

Recognized web addresses keep their original text in translations and replies.
This includes multiple links and spaced forms such as Kick. com /channel.
Item names, game abbreviations and prices within an address stay intact.

UPDATES

Open Settings in the installed launcher. The Updates row checks for a newer
version in the background and shows Install vX & restart when one is available.
Click it to download and verify the installer. After a successful download,
the translator saves your preferences, stops its Ollama service, closes,
installs the update, removes its temporary update files, and reopens itself.
Models, model storage, chat-log selection and overlay settings are kept.
Model setup is skipped during this automatic upgrade; no model is redownloaded.

Installers contain only the current program files. After either a manual or
automatic update, a separate Windows helper waits until the translator has
closed and cleans up before it reopens. This fixes a leftover ucrtbase.dll
from earlier updates. Obsolete libraries and assets are removed only when they
still match a previously shipped copy. Modified and unrecognized files are
preserved. No historical installers, development reports or chat logs are
included. If a locked old file prevents cleanup, close the translator and run
the installer again to retry. Downloaded models and saved settings are kept.

The normal Windows installer progress window appears during replacement.
Checks and downloads that fail can be retried from Settings. Installation or
restart errors are reported; a rare Windows restart requirement needs your
attention. Do not run another installer or uninstall while an update is active.
Version 1.7.12 and later require an independent release signature before an
update can run. Older versions cannot verify this signature: their first
upgrade needs an installer obtained through a trusted source. Installing a
signed-update-capable version adds protection for later updates; it cannot
retroactively protect that first download. Source/development copies can check
releases but must use the website installer to upgrade.

PRIVACY

Chat and replies are translated by the local service. Newly started services
disable request-body debug logging and inherited extra browser origins.
The app does not intentionally save chat translations; the game keeps its own
log, and recent translations can stay in memory until the app closes. Replies
are copied to the Windows clipboard, including any history/sync you enable.
Downloads and update checks contact external servers. They can see your IP
address; update requests also include the app version, never chat text.
Local diagnostics can contain account/folder names; redact them before sharing.
Updates require a release signature checked against a public key built into
the app, in addition to HTTPS and the installer checksum. The signing key is
kept separately from the download server. Windows publisher signing is not
included, so Windows may still show an unknown-publisher prompt. Update
signatures do not protect an already compromised computer.

INITIAL INSTALLATION

1. Install the translator for your Windows account. New installations default to
   %LOCALAPPDATA%\Programs\Lineage 2 Chat Translator. Program-folder permissions
   are restricted to your account, Windows and administrators. Existing installs
   keep their current folder, including the older C:\Lineage 2 Chat Translator
   location, and setup repairs their permissions before replacing program files.
   Setup stops if a folder cannot be protected safely. It does not change the
   permissions of parent folders or move saved settings and models.
2. The installer opens model setup before showing its completion page.
3. Choose a model, a writable model storage folder, and your L2 system folder
   or game root before downloading. Setup requires the game folder and saves
   its Network.log path. It never edits game files or creates the game log.
4. Click Download & configure. Setup downloads the portable Ollama runtime and
   only the selected model. Internet is required; downloads can be several GB.
5. Setup verifies a sample translation, removes its verified runtime download
   archive, saves your choices, and stops its temporary local service. The
   installer then offers to open the translator. Start menu shortcuts also work.

If model setup is canceled or fails, the installer offers Retry. Cancel leaves
the launcher installed and clearly reports that model setup is incomplete.
Use Configure Lineage 2 Chat Translator to finish later. Failed model downloads
keep their resumable layers until retried or uninstalled.

During setup, Cancel stops the current operation and keeps the window open for
retry. The window's close button cancels and then closes after its owned local
service stops. Wait for Cancelling to finish; if cleanup fails, the window stays
open with a retry message. Completed model layers and resumable model data are
kept. An incomplete runtime ZIP is removed and downloads again on retry; a fully
verified runtime ZIP is kept if extraction is cancelled.
Each model download attempt has a two-hour limit even while progress continues.
Retry reuses the model data already downloaded. The runtime download has a
one-hour limit; model verification has its own shorter limit.

After setup, the normal shortcut shows a small Starting translator window with
an animated progress bar while Ollama starts and the saved model is verified.
It opens the translator automatically when ready, without showing model setup.
Setup appears if configuration is incomplete or startup needs attention. Use
Configure Lineage 2 Chat Translator to open it manually for model changes.
The translator opens with the game folder already selected, without an automatic
folder popup. Change it later under Settings > System folder, or use Log file
for a custom log. Existing saved paths and custom log filenames are preserved.

Settings no longer includes the Try a message sample input or Translate button.
Click a message's language code in the overlay to see Reply in Russian (for
example). Click a player name to see Reply to ExamplePlayer in Russian. The heading
updates when the language is detected or changed and no longer repeats the
source message. Name replies still copy the game's whisper prefix automatically.
Both clicks restore the launcher and activate the English input after the
overlay click finishes, ready for immediate typing. Any previous draft is
selected so typing replaces it. Incoming chat does not activate the launcher.

Qwen3.5 9B is the current tested model (approximately 6.6 GB of model weights).
Qwen2.5 3B is the smaller previously tested model (approximately 1.9 GB), with
known translation weaknesses. Qwen3.5 4B is an experimental option.
The shared Ollama runtime takes additional storage, plus temporary setup space.
GPU memory is shared with the game; speed varies by hardware and other GPU use.

Use Configure Lineage 2 Chat Translator in the Start menu to change models or storage.
Download & configure is the main setup button. After setup, it becomes Open translator.
Configure releases any Ollama service it starts when setup or model removal
finishes, including failed operations. Open translator starts and checks the
service again. If cleanup fails, Close keeps the window open so you can retry;
starting another operation also retries cleanup before starting a new service.
Hardware information, download logs, removal and service controls are under
Details & model tools. Closing the main translator window saves your preferences,
requests that loaded models unload, then stops its verified dedicated Ollama
service. A Closing—please wait screen stays visible until shutdown finishes;
wait for that window to disappear before reopening the translator. If shutdown
cannot be confirmed, the translator shows a warning before closing.
Closing or hiding only the overlay keeps translation running. The next
launch starts the service again. Model files stay installed. Use Stop local
service in configuration to explicitly stop the configured service. Configure's
automatic cleanup leaves services it did not start alone. Unrelated Ollama
installations are left alone.
Shutdown diagnostics are saved in the selected data folder under logs/shutdown.log.
Switching storage folders does not move or erase previously downloaded models.
An existing custom chat-log path is preserved when no new L2 folder is selected.
Uninstall from Windows Installed apps or the Uninstall Lineage 2 Chat Translator
Start menu shortcut. After confirmation, it stops this application's service
and removes the launcher, downloaded models, local runtime, saved settings,
logs and owned setup leftovers. There is no keep-data option. App upgrades keep
existing downloads and preferences; full uninstall removes them.

Cleanup tracks data folders selected with this release and adopts the current
verified storage location from earlier releases. Older untracked storage folders
cannot be discovered automatically. Unrelated files and Lineage II game files
are not removed. If data cleanup cannot finish, uninstall keeps the launcher so
you can resolve the reported issue and retry.

Translation runs on this computer. The dedicated service listens on
127.0.0.1:11435 with cloud features disabled. The translator reads the selected
client's Network.log. It does not read game memory, inject code, send keystrokes,
or modify the game. Whisper replies are copied for you to paste yourself.
Chat history and translations are held in memory by the translator.

The L2 client must actually write chat to Network.log. Support was observed with
Reborn Legacy/Origins; other clients and future versions need verification.
This package does not establish a server operator's approval or guarantee
anti-cheat acceptance. Translation can misread slang, Greeklish and context.
Linked items use their ClassID to look up English names in an offline database
of 19,199 standard High Five item IDs before translation. Ollama cannot rename
those protected labels. Enchant levels and recognizable annotations remain.
Unknown or invalid IDs keep their original titles; conflicting variants keep
the original title too. A custom server reusing standard IDs may need different
reference data. This does not change ordinary bracketed text without an item ID.
The database can hold Chinese, Russian, Spanish and Portuguese names for future
localization. Only English coverage and two checked Chinese labels are populated;
missing translations fall back to English. No model or client files are changed.

Downloaded runtime and model licenses are linked in configuration. The runtime
is downloaded separately from the official Ollama release. See
THIRD-PARTY-NOTICES.txt and licenses/ for bundled software notices.

The package is unsigned. A public production release should be Authenticode
signed by the publisher before distribution; checksums identify exact files.
