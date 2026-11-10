"""Self-contained Windows entrypoint and first-run local-model setup."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from app_paths import default_data_directory, preferences_directory, runtime_file
from app_version import VERSION
from ui_i18n import (tr, set_language, get_language, normalize_language, LANGUAGE_NAMES,
                     language_choices, language_code, localize_tree, LocalizedStringVar,
                     set_message, catalog_issues)
from app_icon import ICON, PNG, apply_window_icon
from game_log_setup import resolve_system_log
from installer_runtime import DEFAULT_MODEL, ENDPOINT, MODEL_CATALOG, RuntimeManager, SetupCancelled, SetupError, required_disk_bytes
from translator_core import OllamaClient, parse_chat_line
from uninstall_data import (register_data_root, uninstall_data, storage_requires_confirmation,
                            storage_confirmation_identity)
from app_updater import cleanup_update_cache, consume_update_result, update_in_progress
from app_updater import _powershell as _system_powershell
from installation_cleanup import CLEANUP_PLAN_NAME, read_cleanup_plan

SETTINGS_PATH = preferences_directory() / "settings.json"
RUNTIME_PATH = runtime_file()
UNINSTALL_MUTEX = "Local\\Lineage2ChatTranslatorUninstall"
# Win32 values are protocol constants, not timings or application settings.
MUTEX_SYNCHRONIZE_ACCESS = 0x00100000
ERROR_FILE_NOT_FOUND = 2
ERROR_ALREADY_EXISTS = 183
POPUP_INFORMATION_ICON = 64

LAUNCH_NOTICE_SECONDS = 30  # Rejected copies exit while Windows owns the notice.
HARDWARE_QUERY_TIMEOUT_SECONDS = 15
SETUP_EVENT_POLL_INTERVAL_MS = 100
MAX_CLEANUP_RESULT_CHARACTERS = 8000
UNINSTALL_REVIEW_EXIT_CODE = 2  # Managed data removed; unconfirmed history retained.
WINDOWS_PATH_BUFFER_CHARACTERS = 32768


def uninstall_in_progress() -> bool:
    """The Windows uninstaller holds this guard through program-file removal."""
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = kernel.OpenMutexW(MUTEX_SYNCHRONIZE_ACCESS, False, UNINSTALL_MUTEX)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:  # No uninstall guard exists.
            return False
        raise ctypes.WinError(error)
    kernel.CloseHandle(handle)
    return True


def read_json(path: Path) -> dict:
    """Read local configuration; malformed files must not be silently overwritten."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(tr('Cannot read {path}. Restore or rename this preferences file before retrying.', path=path)) from exc
    if not isinstance(value, dict):
        raise ValueError(tr('Expected an object in {path}.', path=path))
    return value


def write_json(path: Path, value: dict) -> None:
    """Write and atomically replace one preferences file, cleaning partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_selection(data_root: str, model: str, game_folder: str = "") -> tuple[Path, str]:
    """Validate installation choices and resolve a log path without editing the game."""
    if model not in MODEL_CATALOG:
        raise ValueError(tr('Choose one of the available local models.'))
    root = Path(data_root).expanduser()
    if not data_root.strip() or not root.is_absolute() or root == Path(root.anchor):
        raise ValueError(tr('Choose a dedicated runtime and model folder, such as D:\\L2TranslatorData.'))
    # Preserve the chosen lexical path so the storage guard can reject junctions
    # instead of silently turning a redirected folder into another destination.
    root = Path(os.path.abspath(root))
    if root.exists() and not root.is_dir():
        raise ValueError(tr('The runtime and model folder is a file. Choose a folder.'))
    if getattr(sys, "frozen", False) and (root.is_relative_to(Path(sys.executable).parent) or root in Path(sys.executable).parents):
        raise ValueError(tr('Choose a data folder separate from the installed program folder.'))
    log_path = str(resolve_system_log(game_folder)) if game_folder.strip() else ""
    return root, log_path


def save_configuration(data_root: Path, model: str, log_path: str, settings_path: Path = SETTINGS_PATH,
                       location_path: Path = RUNTIME_PATH) -> None:
    """Commit model location/settings together; restore location if saving fails."""
    # Read first: a corrupt preferences file must not silently erase user choices.
    settings = read_json(settings_path)
    settings.update(model=model, ollama_url=ENDPOINT)
    if log_path:
        settings["log_path"] = log_path
    else:
        settings.setdefault("log_path", "")
    settings.setdefault("ui_language", get_language())
    settings.setdefault("target_language", "English")
    settings.setdefault("overlay", {})
    previous_location_exists = location_path.exists()
    previous_location = read_json(location_path)
    write_json(location_path, {"schema_version": 1, "data_root": str(data_root), "app_version": VERSION})
    try:
        write_json(settings_path, settings)
    except OSError as exc:
        try:
            if previous_location_exists:
                write_json(location_path, previous_location)
            else:
                location_path.unlink(missing_ok=True)
        except OSError as rollback_error:
            raise OSError(f"Could not save preferences or restore the previous runtime location. Check {location_path} before launching again.") from rollback_error
        raise exc


def hardware_summary(data_root: Path) -> str:
    """Show local hardware/disk facts in setup only; nothing is uploaded."""
    parts = ["Windows 10/11, 64-bit"]
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                        ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                        ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                        ("extended", ctypes.c_ulonglong)]
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            parts.append(tr('{value0:.0f} GiB RAM', value0=memory.total / 1024**3))
        try:
            # Resolve the Windows binary explicitly, never via the install folder/PATH.
            result = subprocess.run([str(_system_powershell()), "-NoProfile", "-NonInteractive", "-Command",
                                     "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                                    capture_output=True, text=True, timeout=HARDWARE_QUERY_TIMEOUT_SECONDS,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0 and result.stdout.strip():
                parts.append(tr("GPU: {device}", device=", ".join(result.stdout.strip().splitlines())))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    candidate = data_root
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        parts.append(tr('{value0:.1f} GiB free on selected drive', value0=shutil.disk_usage(candidate).free / 1024**3))
    except OSError:
        pass
    summary = parts[0]
    for part in parts[1:]:
        # Keep nested message metadata so changing language does not rerun a
        # hardware process, replace the underlying facts, or leave stale labels.
        summary = tr("{head} · {tail}", head=summary, tail=part)
    return summary


def self_test() -> dict:
    """Exercise bundled imports/Tcl and parser without creating any windows."""
    interpreter = tk.Tcl()
    # Exercise native libraries without a window, model, clipboard or network.
    import hashlib
    import ssl
    import zlib
    native_sample = b"Lineage 2 Chat Translator native dependency check"
    if zlib.decompress(zlib.compress(native_sample)) != native_sample:
        raise AssertionError("Bundled compression library failed.")
    if hashlib.sha256(b"abc").hexdigest() != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad":
        raise AssertionError("Bundled hashing library failed.")
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise AssertionError("Bundled TLS library failed.")
    system_ucrt = False
    if os.name == "nt" and getattr(sys, "frozen", False):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel.GetModuleFileNameW.restype = ctypes.c_uint
        kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        kernel.GetSystemDirectoryW.restype = ctypes.c_uint
        module = kernel.GetModuleHandleW("ucrtbase.dll")
        loaded = ctypes.create_unicode_buffer(WINDOWS_PATH_BUFFER_CHARACTERS)
        system = ctypes.create_unicode_buffer(WINDOWS_PATH_BUFFER_CHARACTERS)
        loaded_length = kernel.GetModuleFileNameW(module, loaded, len(loaded)) if module else 0
        system_length = kernel.GetSystemDirectoryW(system, len(system))
        if not (0 < loaded_length < len(loaded) and 0 < system_length < len(system)):
            raise AssertionError("Could not verify the Windows runtime library.")
        system_ucrt = Path(loaded.value).resolve() == (Path(system.value) / "ucrtbase.dll").resolve()
        if not system_ucrt:
            raise AssertionError("The packaged app did not use the Windows system runtime.")
    record = parse_chat_line("(12:34:56) Log: (Receive)[S_SAY2] SayPacket2ID : 123 Name : Sample Say Msg:WTS [Club] 10kk Type:25")
    if record is None or record.message != "WTS [Club] 10kk":
        raise AssertionError("Bundled parser failed the synthetic chat check.")
    if DEFAULT_MODEL not in MODEL_CATALOG:
        raise AssertionError("Default model missing from catalog.")
    from item_catalog import get_catalog, get_item_name
    catalog = get_catalog()
    if (get_item_name(217) != "Shining Knife" or get_item_name(738) != "Sword of Solidarity"
            or get_item_name(16025) != "Necklace of Freya" or len(catalog.items) < 10000):
        raise AssertionError("Bundled item database is missing or incomplete.")
    from translator_core import decode_item_links
    item_text, protected = decode_item_links("\x08\tType=1 \tID=999 \tClassID=217 \tTitle=\x1b闪耀刀\x1b\x08")
    if item_text != "[Shining Knife]" or protected != ("[Shining Knife]",):
        raise AssertionError("Bundled item ID resolution failed.")
    party_record = parse_chat_line(
        "(12:34:56) Log: (Receive)[S_SAY2] SayPacket2ID : 123 Name : Sample Say Msg:"
        "\x08\tType=10 \tClassID=217 \tTitle=\x1bNeed healer [12~19] (8/9)\x1b\x08 Type:25")
    if (party_record is None or party_record.message != "Need healer [12~19] (8/9)"
            or party_record.item_links or party_record.party_details != ("[12~19]", "(8/9)")):
        raise AssertionError("Bundled party-link separation failed.")
    from chat_links import mark_link_boundaries, restore_link_boundaries, normalize_display
    marked, boundaries = mark_link_boundaries(party_record.message, party_record.link_spans)
    styled = normalize_display(restore_link_boundaries(marked, boundaries))
    if (len(styled.link_spans) != 1 or styled.link_spans[0].kind != "party"
            or styled[styled.link_spans[0].start:styled.link_spans[0].end] != party_record.message):
        raise AssertionError("Bundled link display provenance failed.")
    import chat_overlay
    import reply_composer
    import translator_app
    if catalog_issues() or tr("Settings", language="ja") == "Settings":
        raise AssertionError("The bundled offline interface language catalogs are incomplete.")
    # Imports only: never create a tray icon/window or start its native thread.
    import windows_tray
    import tray_controls
    if not callable(windows_tray.NativeTray) or not callable(tray_controls.TrayControls):
        raise AssertionError("The bundled system tray components are missing.")
    from app_updater import RUNNER_PATH
    helper = RUNNER_PATH.read_text(encoding="utf-8-sig")
    if len(helper) < 1000 or "JobPath" not in helper or "Lineage2ChatTranslatorUpdate" not in helper:
        raise AssertionError("The bundled automatic update helper is missing or invalid.")
    from update_trust import PUBLIC_KEY_BLOB_B64, verify_bundled_signing_key
    if "$releasePublicKeyBlob = '" + PUBLIC_KEY_BLOB_B64 + "'" not in helper:
        raise AssertionError("The application's update verification keys do not match.")
    # Exercise native verification against a public proof, never the private key.
    verify_bundled_signing_key()
    from runtime_integrity import verify_bundled_frontend
    # Hash only; this headless test never launches the backend or a model.
    verify_bundled_frontend()
    cleanup_helper = (Path(__file__).parent / "assets" / "cleanup-program-files.ps1").read_text(encoding="utf-8-sig")
    if (len(cleanup_helper) < 1000 or "Invoke-RegisteredCleanup" not in cleanup_helper
            or "L2ProminenceChatTranslator" not in cleanup_helper):
        raise AssertionError("The bundled external cleanup helper is missing or invalid.")
    # Presence only: a headless self-test must never change installation ACLs.
    security_helper = (Path(__file__).parent / "assets" / "protect-install-directory.ps1").read_text(encoding="utf-8-sig")
    if "Invoke-InstallDirectoryProtection" not in security_helper or "VerifyOnly" not in security_helper:
        raise AssertionError("The bundled installation permission helper is missing or invalid.")
    if getattr(sys, "frozen", False):
        # Validate metadata only: packaged checks must never remove program files.
        plan = read_cleanup_plan(Path(__file__).parent / "assets" / CLEANUP_PLAN_NAME, VERSION)
        if "Lineage2ChatTranslator.exe" not in plan["current_paths"]:
            raise AssertionError("The bundled program-file inventory is incomplete.")
        # GetModuleHandle reports only one matching module; it cannot prove that
        # a stale app-local copy was not also loaded. Reject that leftover file.
        if (Path(__file__).parent / "ucrtbase.dll").exists():
            raise AssertionError("An obsolete app-local Windows runtime remains; rerun the installer to finish cleanup.")
    # Constructing a client performs no network requests.
    client = OllamaClient(ENDPOINT, DEFAULT_MODEL)
    if client.base_url != ENDPOINT:
        raise AssertionError("Unexpected local endpoint.")
    # Pure URL listings must preserve complete addresses without inference,
    # including hosts beginning with a familiar game abbreviation.
    def no_network(*args, **kwargs):
        raise AssertionError("The packaged URL check attempted a network request.")
    client._request = no_network
    url_listing = "WTS pvp.example.com/WTB/10kk - Kick. com /sample - Twitch.tv/sample"
    if client.translate(url_listing) != url_listing:
        raise AssertionError("Bundled source URL protection failed.")
    if ICON.read_bytes()[:4] != b"\x00\x00\x01\x00" or PNG.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("Bundled application icon is missing or invalid.")
    return {"ok": True, "version": VERSION, "frozen": bool(getattr(sys, "frozen", False)),
            "python": sys.version.split()[0], "tcl": interpreter.eval("info patchlevel"),
            "models": list(MODEL_CATALOG), "settings_path": str(SETTINGS_PATH),
            "app_settings_path": str(translator_app.SETTINGS),
            "item_catalog_version": catalog.version, "item_catalog_entries": len(catalog.items),
            "checks": ["bundled imports", "Tcl runtime", "native TLS, hashing and compression", "synthetic log parser", "model catalog", "loopback client", "bundled icon", "bundled item database", "localized item ID resolution", "party link separation", "link display provenance", "source URL protection", "bundled update helper", "bundled external cleanup helper", "bundled installation permission helper", "offline interface language catalogs"] + (["bundled cleanup inventory", "no obsolete app-local UCRT"] if getattr(sys, "frozen", False) else []) + (["Windows system UCRT"] if system_ucrt else []),
            "system_tray_imports": True,
            "windows_opened": False, "network_requests": False}


def diagnose() -> dict:
    """Return local diagnostic details on explicit request, without reading chat."""
    location = read_json(RUNTIME_PATH)
    settings = read_json(SETTINGS_PATH)
    root = Path(location.get("data_root") or default_data_directory())
    return {"version": VERSION, "settings_path": str(SETTINGS_PATH), "runtime_path": str(RUNTIME_PATH),
            "data_root": str(root), "runtime_exists": (root / "ollama" / "ollama.exe").is_file(),
            "model": settings.get("model", DEFAULT_MODEL), "hardware": hardware_summary(root),
            "windows_opened": False, "services_started": False}


class SingleInstance:
    """One setup/translator process per interactive Windows session."""
    def __init__(self):
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel.CreateMutexW(None, False, "Local\\L2ProminenceChatTranslator")
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def close(self):
        if self.handle:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(self.handle)
            self.handle = None


def detached_launch_notice(reason: str) -> None:
    """Show a bounded notice without keeping installed program files locked."""
    notices = {
        "duplicate": (
            "The translator or its setup is already open or closing.\n\n"
            "If you just closed the translator, wait for its Closing window to disappear, then try again. "
            "Otherwise, close the existing window before switching models or starting another copy."
        ),
        "uninstall": "Uninstall is in progress. Wait for it to finish before opening the translator or setup.",
        "update": "An update is being installed. The translator will reopen automatically when it finishes.",
    }
    try:
        # Only the fixed notices above enter the script; no command-line, chat,
        # path, server response or exception text is interpolated.
        notice = tr(notices[reason]).replace("'", "''")
        script = ("$ErrorActionPreference = 'Stop'\n"
                  "$popup = New-Object -ComObject WScript.Shell\n"
                  "[void]$popup.Popup('" + notice + f"', {LAUNCH_NOTICE_SECONDS}, 'Lineage 2 Chat Translator', {POPUP_INFORMATION_ICON})\n")
        powershell = _system_powershell()
        # This system process owns the popup after the rejected launcher exits.
        # Its working directory and executable are outside the installed app.
        subprocess.Popen([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                          "-Command", script], cwd=powershell.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        # A notice failure must never keep this rejected copy alive or reopen
        # a Tk error dialog that would block replacing/uninstalling its files.
        pass


def save_ui_language(language: str, settings_path: Path | None = None) -> str:
    """Save only the interface preference, preserving the chat target and model."""
    path = SETTINGS_PATH if settings_path is None else settings_path
    settings = read_json(path)
    language = normalize_language(language)
    settings["ui_language"] = language
    write_json(path, settings)
    return language


class SetupWindow:
    def __init__(self, root: tk.Tk, *, automatic: bool = False, installer_mode: bool = False, ui_language: str | None = None):
        self.root = root
        # A configured normal launch validates its service in the background.
        # Withdraw before building widgets so setup never flashes on success.
        if automatic:
            self.root.withdraw()
        self.events = queue.Queue()
        self.busy = False
        self.ready = False
        self.launch_requested = False
        # Keep the managers that started work, not the currently edited folder.
        # A failed stop stays tracked so Close (or final exit) can retry it.
        self._service_managers = []
        self._work_lock = threading.RLock()
        self._exiting = False
        self._closing = False
        self._cancel_event = threading.Event()
        self._cancellable = False
        self.installer_mode = installer_mode
        self.setup_completed = False
        self.automatic = automatic
        self._hardware_started = False
        self._startup_frame = None
        self._startup_progress = None
        self.settings = read_json(SETTINGS_PATH)
        set_language(ui_language or self.settings.get("ui_language") or get_language())
        self.ui_language = tk.StringVar(value=LANGUAGE_NAMES[get_language()])
        self._language_variables = []
        location = read_json(RUNTIME_PATH)
        self.data = tk.StringVar(value=location.get("data_root") or str(default_data_directory()))
        self.model_tags = list(MODEL_CATALOG)
        selected = self.settings.get("model", DEFAULT_MODEL)
        if selected not in MODEL_CATALOG:
            selected = DEFAULT_MODEL
        self.labels = [tr(MODEL_CATALOG[tag]["label"]) for tag in self.model_tags]
        self.model = tk.StringVar(value=self.labels[self.model_tags.index(selected)])
        log_path = self.settings.get("log_path", "")
        if not isinstance(log_path, str) or not log_path.strip():
            log_path = ""
        self.game = tk.StringVar(value=str(Path(log_path).parent) if log_path else "")
        self.initial_game = self.game.get()
        self.accepted = tk.BooleanVar(value=False)
        self.detail = self._message_var()
        self.model_note = self._message_var()
        self.details_visible = False
        self.status = self._message_var(value="Choose your model and folders, then download to get started.")
        self.hardware = self._message_var(value="Checking memory, graphics hardware, and available storage…")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(SETUP_EVENT_POLL_INTERVAL_MS, self._drain)
        if automatic:
            self.root.after(0, self.launch_existing)
        else:
            self._show_setup()

    def _message_var(self, value=""):
        variable = LocalizedStringVar(master=self.root, value=value)
        self._language_variables.append(variable)
        return variable

    def _ui_language_changed(self, _event=None):
        previous = get_language()
        language = language_code(self.ui_language.get())
        selected = self.selected_tag()
        try:
            save_ui_language(language)
        except (ValueError, OSError) as exc:
            self.ui_language.set(LANGUAGE_NAMES[previous])
            messagebox.showerror(tr("Interface language"), tr("Could not save the interface language: {error}", error=exc), parent=self.root)
            return
        self.settings["ui_language"] = language
        set_language(language)
        self.labels = [tr(MODEL_CATALOG[tag]["label"]) for tag in self.model_tags]
        self.model_box.configure(values=self.labels)
        self.model.set(self.labels[self.model_tags.index(selected)])
        localize_tree(self.root)
        for variable in self._language_variables:
            variable.refresh_language()
        self._refresh_model_text()
        self.root.title(tr("Lineage 2 Chat Translator — Setup {VERSION}", VERSION=VERSION))
        self._sync_cancel_action()

    def _show_setup(self):
        """Reveal configuration only when requested or startup needs attention."""
        if self._hide_starting():
            self.root.title(tr('Lineage 2 Chat Translator — Setup {VERSION}', VERSION=VERSION))
            self.root.minsize(700, 680)
            self.root.geometry("740x700")
            self.root.resizable(True, True)
            self.root.configure(background="#f4f6f9")
            self._setup_frame.pack(fill="both", expand=True)
        self.root.deiconify()
        if not self._hardware_started:
            self._hardware_started = True
            initial_data = Path(self.data.get())
            threading.Thread(target=lambda: self.events.put(("hardware", hardware_summary(initial_data))),
                             daemon=True).start()

    def _show_starting(self):
        """Use the same root for a compact progress screen, keeping setup hidden."""
        if self._startup_frame is not None:
            return
        self._setup_frame.pack_forget()
        self.root.title(tr('Lineage 2 Chat Translator — Starting…'))
        self.root.minsize(460, 210)
        self.root.geometry("460x210")
        self.root.resizable(False, False)
        self.root.configure(background="#111820")
        self._startup_frame = tk.Frame(self.root, background="#111820", padx=24, pady=24)
        self._startup_frame.pack(fill="both", expand=True)
        tk.Label(self._startup_frame, text=tr("Starting translator…"), background="#111820", foreground="#edf3f8",
                 font=("Segoe UI", 16, "bold")).pack(pady=(0, 12))
        self.status.set(tr('Starting local Ollama.\nThe translator will open automatically.'))
        tk.Label(self._startup_frame, textvariable=self.status,
                 background="#111820", foreground="#a4b4c3", justify="center", wraplength=400).pack(pady=(0, 18))
        self._startup_progress = ttk.Progressbar(self._startup_frame, mode="indeterminate", length=340)
        self._startup_progress.pack()
        self._startup_progress.start(20)
        # Respect larger Windows text scaling while keeping the default compact.
        self.root.update_idletasks()
        width = max(460, self._startup_frame.winfo_reqwidth())
        height = max(210, self._startup_frame.winfo_reqheight())
        self.root.geometry(f"{width}x{height}")
        self.root.deiconify()
        self.root.focus_set()

    def _hide_starting(self):
        if getattr(self, "_startup_frame", None) is None:
            return False
        self._startup_progress.stop()
        self._startup_frame.destroy()
        self._startup_frame = None
        self._startup_progress = None
        return True

    def selected_tag(self) -> str:
        return self.model_tags[self.labels.index(self.model.get())]

    def _build(self):
        self.root.title(tr('Lineage 2 Chat Translator — Setup {VERSION}', VERSION=VERSION))
        self.root.geometry("740x700")
        self.root.minsize(700, 680)
        self.root.configure(background="#f4f6f9")
        self.root.option_add("*Font", ("Segoe UI", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Setup.TFrame", background="#f4f6f9")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Setup.TLabel", background="#f4f6f9", foreground="#667383")
        style.configure("Heading.TLabel", background="#f4f6f9", foreground="#172033", font=("Segoe UI", 19, "bold"))
        style.configure("Card.TLabel", background="#ffffff", foreground="#172033")
        style.configure("Field.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 10, "bold"))
        style.configure("Note.TLabel", background="#ffffff", foreground="#667383", font=("Segoe UI", 9))
        style.configure("Setup.TEntry", padding=7, fieldbackground="#ffffff")
        style.configure("Setup.TCombobox", padding=7, fieldbackground="#ffffff")
        style.map("Setup.TCombobox", fieldbackground=[("readonly", "#ffffff")], selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", "#172033")])
        style.configure("Setup.TCheckbutton", background="#ffffff", foreground="#172033")
        style.map("Setup.TCheckbutton", background=[("active", "#ffffff")])
        style.configure("Secondary.TButton", padding=(10, 6), background="#eaf0f8", foreground="#24334d", borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#dbe5f3")])
        style.configure("License.TButton", padding=(2, 4), background="#ffffff", foreground="#245ce5", borderwidth=0)
        style.map("License.TButton", background=[("active", "#ffffff")], foreground=[("active", "#1746b5")])
        style.configure("Details.TButton", padding=(0, 5), background="#f4f6f9", foreground="#52657d", borderwidth=0)
        style.map("Details.TButton", background=[("active", "#f4f6f9")], foreground=[("active", "#245ce5")])
        style.configure("Primary.TButton", font=("Segoe UI", 12, "bold"), padding=(18, 12), background="#245ce5", foreground="#ffffff", borderwidth=0, focusthickness=2, focuscolor="#9bb6ff")
        style.map("Primary.TButton", background=[("disabled", "#91a9e5"), ("active", "#1948be"), ("pressed", "#153eaa")], foreground=[("disabled", "#ffffff")])
        frame = ttk.Frame(self.root, padding=24, style="Setup.TFrame")
        self._setup_frame = frame
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        # The first field and first tab stop always selects the interface language.
        language_row = ttk.Frame(frame, style="Setup.TFrame")
        language_row.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(language_row, text="Language / 语言", style="Setup.TLabel").pack(side="left", padx=(0, 10))
        self.language_box = ttk.Combobox(language_row, textvariable=self.ui_language,
                                        values=language_choices(), state="readonly", width=25,
                                        height=len(LANGUAGE_NAMES),
                                        style="Setup.TCombobox")
        self.language_box.pack(side="right")
        self.language_box.bind("<<ComboboxSelected>>", self._ui_language_changed)
        ttk.Label(frame, text="Set up your translator", style="Heading.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="Download once. Translate locally.", style="Setup.TLabel").grid(row=2, column=0, sticky="w", pady=(5, 18))
        card = ttk.Frame(frame, padding=18, style="Card.TFrame")
        card.grid(row=3, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text="Translation model", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.model_box = ttk.Combobox(card, textvariable=self.model, values=self.labels, state="readonly", style="Setup.TCombobox")
        self.model_box.grid(row=1, column=0, sticky="ew", pady=(6, 7))
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)
        ttk.Label(card, textvariable=self.detail, style="Note.TLabel", wraplength=600).grid(row=2, column=0, sticky="w")
        ttk.Label(card, textvariable=self.model_note, style="Note.TLabel", wraplength=600).grid(row=3, column=0, sticky="w", pady=(3, 7))
        terms = ttk.Frame(card, style="Card.TFrame")
        terms.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        self.accept_box = ttk.Checkbutton(terms, text="I accept the model license", variable=self.accepted, style="Setup.TCheckbutton")
        self.accept_box.pack(side="left")
        self.license_button = ttk.Button(terms, text="Read license ↗", command=self._open_license, style="License.TButton")
        self.license_button.pack(side="right")
        self.controls = [self.model_box, self.accept_box, self.license_button]
        self._path_row(card, 5, "Model storage", self.data, False)
        self._path_row(card, 7, "Lineage 2 system folder · required", self.game, True)
        ttk.Label(card, text="Choose before downloading. You can change it later in the translator's Settings.", style="Note.TLabel", wraplength=600).grid(row=9, column=0, sticky="w", pady=(4, 0))
        self.details_button = ttk.Button(frame, text="Details & model tools ▸", style="Details.TButton", command=self._toggle_details)
        self.details_button.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.details_panel = ttk.Frame(frame, style="Setup.TFrame")
        self.details_panel.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.details_panel.columnconfigure(0, weight=1)
        ttk.Label(self.details_panel, textvariable=self.hardware, style="Setup.TLabel", wraplength=640).grid(row=0, column=0, sticky="w", pady=(0, 8))
        buttons = ttk.Frame(self.details_panel, style="Setup.TFrame")
        buttons.grid(row=1, column=0, sticky="w", pady=(0, 8))
        self.remove_button = ttk.Button(buttons, text="Remove selected model…", command=self.remove, style="Secondary.TButton")
        if not self.installer_mode:
            self.remove_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="Stop local service", command=self.stop_service, style="Secondary.TButton")
        self.stop_button.pack(side="left")
        self.output = tk.Text(self.details_panel, height=5, wrap="word", state="disabled", font=("Segoe UI", 9), background="#ffffff", relief="flat", padx=8, pady=6)
        self.output.grid(row=2, column=0, sticky="ew")
        self.details_panel.grid_remove()
        frame.rowconfigure(6, weight=1)
        self.status_label = ttk.Label(frame, textvariable=self.status, style="Setup.TLabel", wraplength=640, justify="left")
        self.status_label.grid(row=7, column=0, sticky="w", pady=(10, 8))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=8, column=0, sticky="ew", pady=(0, 10))
        self.progress.grid_remove()
        actions = ttk.Frame(frame, style="Setup.TFrame")
        actions.grid(row=9, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.install_button = ttk.Button(actions, text="Download & configure", command=self.install, style="Primary.TButton")
        self.install_button.grid(row=0, column=0, sticky="ew")
        self.open_button = ttk.Button(actions, text="Open translator →", command=self.open_translator, style="Primary.TButton")
        self.open_button.grid(row=0, column=0, sticky="ew")
        self.open_button.grid_remove()
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_work, style="Secondary.TButton")
        self.cancel_button.grid(row=1, column=0, sticky="e", pady=(8, 0))
        self.cancel_button.grid_remove()
        self.controls.extend([self.install_button, self.stop_button])
        if not self.installer_mode:
            self.controls.append(self.remove_button)
        self._model_changed()
        localize_tree(self.root)
        self.language_box.focus_set()

    def _path_row(self, frame, row, label, variable, game):
        ttk.Label(frame, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=(4, 0))
        holder = ttk.Frame(frame, style="Card.TFrame")
        holder.grid(row=row + 1, column=0, sticky="ew", pady=(6, 10))
        holder.columnconfigure(0, weight=1)
        entry = ttk.Entry(holder, textvariable=variable, style="Setup.TEntry")
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        button = ttk.Button(holder, text="Browse…", command=lambda: self._browse(variable, game), style="Secondary.TButton")
        button.grid(row=0, column=1)
        self.controls.extend([entry, button])
        variable.trace_add("write", lambda *_: self._invalidate())

    def _invalidate(self):
        self.ready = False
        if hasattr(self, "open_button"):
            self._sync_actions()

    def _sync_actions(self):
        self.install_button.configure(state="disabled" if self.busy else "normal", text=(tr('Working…') if self.busy else tr('Download & configure')))
        self.open_button.configure(state="disabled" if self.busy else "normal")
        if self.ready:
            self.install_button.grid_remove()
            self.open_button.grid()
        else:
            self.open_button.grid_remove()
            self.install_button.grid()

    def _toggle_details(self):
        self.details_visible = not self.details_visible
        self.details_button.configure(text=(tr('Details & model tools ▾') if self.details_visible else tr('Details & model tools ▸')))
        if self.details_visible:
            self.details_panel.grid()
        else:
            self.details_panel.grid_remove()
        self.root.update_idletasks()
        change = 210 if self.details_visible else -210
        height = max(680, self.root.winfo_height() + change)
        height = min(height, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{self.root.winfo_width()}x{height}")

    def _model_changed(self, _event=None):
        self._invalidate()
        self.accepted.set(False)
        self._refresh_model_text()

    def _refresh_model_text(self):
        choice = MODEL_CATALOG[self.selected_tag()]
        disk = required_disk_bytes(self.selected_tag()) / 1024**3
        set_message(self.detail, "{download:.1f} GB download · Allow {disk:.1f} GiB free · {license}",
                    download=choice["download_gb"], disk=disk, license=choice["license"])
        notes = {DEFAULT_MODEL: "Current tested model. A GPU is recommended; Greeklish can still be inaccurate.",
                 "qwen2.5:3b": "Smaller download. Less accurate with slang and Greeklish.",
                 "qwen3.5:4b-q4_K_M": "Experimental. Translation quality has not been validated."}
        self.model_note.set(notes[self.selected_tag()])

    def _open_license(self):
        webbrowser.open(MODEL_CATALOG[self.selected_tag()]["license_url"])

    def _browse(self, variable, game):
        chosen = filedialog.askdirectory(parent=self.root, title=(tr('Choose Lineage II system folder') if game else tr('Choose runtime and model folder')))
        if chosen:
            variable.set(chosen)
            if not game:
                threading.Thread(target=lambda: self.events.put(("hardware", hardware_summary(Path(chosen)))), daemon=True).start()

    def _set_busy(self, busy):
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else ("readonly" if control is self.model_box else "normal"))
        self._sync_actions()
        self._sync_cancel_action()
        if busy:
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(value=0)
            self.progress.grid_remove()

    def _report(self, text):
        # Preserve localized template metadata through the worker queue.
        self.events.put(("progress", text if isinstance(text, str) else str(text)))

    def _sync_cancel_action(self):
        button = getattr(self, "cancel_button", None)
        if button is not None:
            if self.busy and self._cancellable:
                button.grid()
                button.configure(state="disabled" if self._cancel_event.is_set() else "normal",
                                 text=(tr('Cancelling…') if self._cancel_event.is_set() else tr('Cancel')))
            else:
                button.grid_remove()

    @staticmethod
    def _check_cancelled(cancel_event):
        if cancel_event.is_set():
            raise SetupCancelled("Setup cancelled. Unfinished model downloads can be resumed.")

    def _runtime_manager(self, data_root, cancel_event):
        manager = RuntimeManager(data_root, progress=self._report, cancel_event=cancel_event)
        self._service_managers.append(manager)
        return manager

    def _start_work(self, work, *, cleanup_first=False):
        # Each operation owns a fresh token; old managers retain their old token.
        # Cleanup-only work must not reset it or become cancellable itself.
        self._cancellable = cleanup_first
        cancel_event = threading.Event() if cleanup_first else self._cancel_event
        if cleanup_first:
            self._cancel_event = cancel_event
        self._sync_cancel_action()
        def serialized_work():
            # Final exit either waits for the active operation or prevents a
            # queued worker from starting a new service after cleanup.
            with self._work_lock:
                if not self._exiting:
                    if cleanup_first:
                        if cancel_event.is_set():
                            self._operation_result("cancelled", "", cancel_event=cancel_event)
                            return
                        # A previous partial startup may still own a live child
                        # without a listener. Do not create another manager until
                        # that failed cleanup has actually completed.
                        try:
                            self._cleanup_services()
                        except Exception as exc:
                            self.events.put(("cancel_error" if cancel_event.is_set() else "error", str(exc)))
                            return
                        if cancel_event.is_set():
                            self._operation_result("cancelled", "", cancel_event=cancel_event)
                            return
                        work(cancel_event)
                    else:
                        work()
        threading.Thread(target=serialized_work, daemon=True).start()

    def _cleanup_services(self):
        """Release only children started by these managers; retain failed stops."""
        with self._work_lock:
            failures = []
            for manager in self._service_managers[:]:
                try:
                    manager.stop_owned_service()
                except Exception as exc:
                    failures.append(str(exc))
                else:
                    self._service_managers.remove(manager)
            if failures:
                raise RuntimeError(tr('Could not finish local service cleanup: ') + "\n".join(failures))

    def finish(self):
        """Cover abnormal setup exits and failures before translator handoff."""
        # Signal before waiting on the worker, so a network read can be aborted.
        self._cancel_event.set()
        with self._work_lock:
            self._exiting = True
            self._cleanup_services()

    def _operation_result(self, kind, message, failure=None, *, cancel_event):
        # Setup/model tools do not retain a GPU service while idle. Only a
        # verified launch keeps it alive for the translator's deliberate handoff.
        cleanup_failure = None
        try:
            self._cleanup_services()
        except Exception as exc:
            cleanup_failure = str(exc)
            failure = (failure + "\n" if failure else "") + str(exc)
        if cancel_event.is_set():
            self.events.put(("cancel_error", cleanup_failure) if cleanup_failure else ("cancelled", ""))
        else:
            self.events.put(("error", failure) if failure is not None else (kind, message))

    def install(self):
        """Configure local runtime/model only after validated choices and license consent."""
        try:
            chosen_game = self.game.get().strip()
            if not chosen_game:
                raise ValueError(tr("Choose your Lineage II system folder before downloading. You can change it later in the translator's Settings."))
            # An unchanged folder must preserve even a manually selected log filename.
            initial_game = getattr(self, "initial_game", "")
            saved_log = self.settings.get("log_path")
            if (isinstance(saved_log, str) and saved_log.strip() and initial_game
                    and Path(chosen_game).expanduser().resolve() == Path(initial_game).expanduser().resolve()):
                chosen_game = ""
            data_root, log = validate_selection(self.data.get(), self.selected_tag(), chosen_game)
            if not self.accepted.get():
                raise ValueError(tr("Review and accept the selected model's license before downloading."))
            confirmed_root = None
            confirmed_identity = None
            if storage_requires_confirmation(data_root, SETTINGS_PATH.parent):
                # Bind approval to the directory shown, not a replacement at
                # the same path while the prompt or worker handoff is pending.
                confirmed_identity = storage_confirmation_identity(data_root, SETTINGS_PATH.parent)
                approved = messagebox.askyesno(
                    tr('Review existing model storage'),
                    tr('Please confirm this is the folder you use for this translator:\n\n') + str(data_root) + tr("\n\nIts runtime, models, logs and service-home folders will be treated as this app's data and removed if you uninstall. Your Windows account keeps full access. No model files are deleted during this review. Other unconfirmed historical folders will be left for manual review.\n\nUse and secure this folder? Choose No to select another folder."),
                    parent=self.root, default="no")
                if not approved:
                    return
                confirmed_root = data_root
        except (ValueError, OSError, SetupError) as exc:
            messagebox.showerror(tr('Setup'), str(exc), parent=self.root)
            return
        model = self.selected_tag()
        self.ready = False
        self._set_busy(True)
        def work(cancel_event):
            failure = None
            try:
                # Track failed/partial setup as well as completed installations.
                if confirmed_root is None:
                    register_data_root(data_root, SETTINGS_PATH.parent)
                else:
                    register_data_root(data_root, SETTINGS_PATH.parent, confirmed_root=confirmed_root,
                                       confirmed_identity=confirmed_identity)
                manager = self._runtime_manager(data_root, cancel_event)
                manager.prepare(model)
                self._check_cancelled(cancel_event)
                self._report(tr('Testing translation with a synthetic Russian sentence…'))
                client = OllamaClient(ENDPOINT, model, "English")
                with manager.cancellable_client(client):
                    result = client.translate("Привет! Ищем хилера в пати.")
                self._check_cancelled(cancel_event)
                self._report(tr('Sample translation: ') + result)
                self._report(tr('Cleaning up installation files…'))
                manager.cleanup_installation()
                self._check_cancelled(cancel_event)
                save_configuration(data_root, model, log)
            except Exception as exc:
                failure = str(exc)
            self._operation_result("ready", "Setup complete. Your model and preferences are saved. Open the translator when ready.", failure, cancel_event=cancel_event)
        self._start_work(work, cleanup_first=True)

    def remove(self):
        # Maintenance can start its own service. Keep it in configuration,
        # outside the installer's bounded download-and-finish lifecycle.
        if getattr(self, "installer_mode", False):
            return
        tag = self.selected_tag()
        try:
            data_root, _ = validate_selection(self.data.get(), tag)
            current = read_json(SETTINGS_PATH).get("model")
            if tag == current:
                raise ValueError(tr('Configure a different model first, then select this model to remove it.'))
        except (ValueError, OSError) as exc:
            messagebox.showerror(tr('Remove model'), str(exc), parent=self.root)
            return
        if not messagebox.askyesno(tr('Remove downloaded model'), tr("Remove {tag} from this translator's model folder?\n\nYou can download it again later.", tag=tag), parent=self.root):
            return
        self._set_busy(True)
        def work(cancel_event):
            failure = None
            try:
                manager = self._runtime_manager(data_root, cancel_event)
                manager.ensure_service()
                self._check_cancelled(cancel_event)
                manager.remove_model(tag)
            except Exception as exc:
                failure = str(exc)
            self._operation_result("removed", f"Removed {tag}.", failure, cancel_event=cancel_event)
        self._start_work(work, cleanup_first=True)

    def launch_existing(self):
        """Check saved installation in a worker and hand off when its local model is ready."""
        try:
            location = read_json(RUNTIME_PATH)
            settings = read_json(SETTINGS_PATH)
        except (ValueError, OSError) as exc:
            self._show_setup()
            self.status.set(tr('Could not open the saved configuration: ') + str(exc))
            return
        if not location.get("data_root") or settings.get("model") not in MODEL_CATALOG:
            self._show_setup()
            return
        log_path = settings.get("log_path")
        if not isinstance(log_path, str) or not log_path.strip():
            self._show_setup()
            self.status.set(tr("Choose your Lineage II system folder to finish setup. You can change it later in the translator's Settings."))
            return
        try:
            if storage_requires_confirmation(Path(location["data_root"]), SETTINGS_PATH.parent):
                self._show_setup()
                self.status.set(tr('Review your existing model folder, then choose Download & configure to secure it. Existing model files will be reused after verification.'))
                return
        except (OSError, ValueError, SetupError) as exc:
            self._show_setup()
            self.status.set(tr('The saved model folder needs review: ') + str(exc))
            return
        if getattr(self, "automatic", False):
            self._show_starting()
        self._set_busy(True)
        self.status.set(tr('Checking your local translation service…'))
        def work(cancel_event):
            try:
                register_data_root(Path(location["data_root"]), SETTINGS_PATH.parent)
                manager = self._runtime_manager(Path(location["data_root"]), cancel_event)
                if not manager.executable.is_file():
                    raise ValueError(tr('The local Ollama runtime is missing. Use Download & configure to restore it.'))
                manager.ensure_service()
                self._check_cancelled(cancel_event)
                model = settings["model"]
                if model not in {entry.get("name", entry.get("model")) for entry in manager.installed_models()}:
                    raise ValueError(tr('The selected model is missing. Use Download & configure to restore it.'))
                client = OllamaClient(ENDPOINT, model)
                with manager.cancellable_client(client):
                    client._verify_local_model()
                self._check_cancelled(cancel_event)
                self.events.put(("launch", ""))
            except Exception as exc:
                self._operation_result("launch", "", str(exc), cancel_event=cancel_event)
        self._start_work(work, cleanup_first=True)

    def stop_service(self):
        try:
            location = read_json(RUNTIME_PATH)
            if not location.get("data_root"):
                raise ValueError(tr('There is no configured service to stop. If an earlier translator uses this port, close its local Ollama service first or choose its existing data folder.'))
            data_root = Path(location["data_root"])
        except (ValueError, OSError) as exc:
            messagebox.showerror(tr('Local service'), str(exc), parent=self.root)
            return
        self._invalidate()
        self._set_busy(True)
        def work():
            try:
                self._cleanup_services()
                RuntimeManager(data_root, progress=self._report).stop_service()
                self.events.put(("stopped", "Local service stopped. You can now choose a different storage folder. Changing folders downloads a separate runtime and model; it does not move existing files."))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        self._start_work(work)

    def _drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "hardware":
                    self.hardware.set(value)
                elif kind == "progress":
                    # Late progress cannot hide the user's cancellation request.
                    if not self._cancel_event.is_set():
                        self.status.set(value)
                    self.output.configure(state="normal")
                    self.output.insert("end", value + "\n")
                    self.output.see("end")
                    self.output.configure(state="disabled")
                elif kind in ("ready", "launch", "removed", "stopped", "error") and (
                        self._closing or (self._cancellable and self._cancel_event.is_set())):
                    # Close/Cancel wins even when success was already queued.
                    # A queued launch still owns a service, so finish cleanup
                    # off the UI thread before acknowledging cancellation.
                    self._begin_cancel_cleanup()
                elif kind == "launch":
                    self.launch_requested = True
                    self._hide_starting()
                    self.root.destroy()
                    return
                elif kind == "closed":
                    self._hide_starting()
                    self.root.destroy()
                    return
                elif kind == "cancelled":
                    self.ready = self.setup_completed = self.launch_requested = False
                    self._cancellable = False
                    self._set_busy(False)
                    if self._closing:
                        self._hide_starting()
                        self.root.destroy()
                        return
                    self._show_setup()
                    self.status.set(tr('Setup cancelled. Unfinished model downloads can be resumed. Retry when ready.'))
                elif kind in ("close_error", "cancel_error"):
                    self._closing = False
                    self.ready = self.setup_completed = self.launch_requested = False
                    self._cancellable = False
                    self._set_busy(False)
                    self._show_setup()
                    self.status.set(value + tr(' Close this window again to retry.'))
                else:
                    self._cancellable = False
                    self.ready = kind == "ready"
                    if self.ready and getattr(self, "installer_mode", False):
                        self.setup_completed = True
                        self.root.destroy()
                        return
                    self._set_busy(False)
                    self.status.set((value if kind != "error" else tr('Setup stopped: ') + value + tr(' Retry with Download & configure.')))
                    if kind == "error":
                        self._show_setup()
        except queue.Empty:
            pass
        self.root.after(SETUP_EVENT_POLL_INTERVAL_MS, self._drain)

    def open_translator(self):
        if self.ready and not self.busy:
            # Configuration has already released its service. Start and verify
            # the saved runtime again before opening a working translator.
            self.launch_existing()

    def cancel_work(self):
        if self.busy and self._cancellable:
            self.ready = False
            self._cancel_event.set()
            self.status.set(tr("Cancelling… Stopping setup's work and local service."))
            self._sync_cancel_action()

    def _begin_cancel_cleanup(self):
        self._cancellable = False
        self._set_busy(True)
        def work():
            try:
                self._cleanup_services()
            except Exception as exc:
                self.events.put(("cancel_error", str(exc)))
            else:
                self.events.put(("cancelled", ""))
        self._start_work(work)

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.busy:
            if self._cancellable:
                self.cancel_work()
            else:
                self.status.set(tr('Closing… Waiting for local service cleanup.'))
            return
        if not self._service_managers:
            self.root.destroy()
            return
        self._set_busy(True)
        self.status.set(tr('Closing… Stopping the local translation service.'))
        def work():
            try:
                self._cleanup_services()
            except Exception as exc:
                self.events.put(("close_error", str(exc)))
            else:
                self.events.put(("closed", ""))
        self._start_work(work)


def main(argv=None) -> int:
    """Route headless maintenance separately from guarded setup/translator windows."""
    parser = argparse.ArgumentParser(description="Lineage 2 Chat Translator")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--configure", action="store_true")
    modes.add_argument("--install-model", action="store_true", help="Complete model setup, then return to the installer")
    modes.add_argument("--uninstall-cleanup", action="store_true", help="Remove this installation's managed data before program uninstall")
    modes.add_argument("--cleanup-program-files", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--diagnose", action="store_true")
    parser.add_argument("--ui-language", choices=tuple(LANGUAGE_NAMES), help="Interface language selected by the installer")
    parser.add_argument("--output", type=Path, help="Write headless JSON to this file")
    parser.add_argument("--cleanup-result", type=Path, help="Write a plain-text result for the Windows installer or uninstaller")
    args = parser.parse_args(argv)
    # Maintenance stays headless; only an explicit installer language changes prefs.
    if args.ui_language:
        set_language(args.ui_language)
    elif not (args.self_test or args.diagnose):
        try:
            set_language(read_json(SETTINGS_PATH).get("ui_language") or get_language())
        except (ValueError, OSError):
            pass
    if args.cleanup_program_files:
        # A frozen process may map the exact DLL it is asked to remove. Only the
        # installer's external system helper performs this maintenance now.
        result = "Program-file cleanup is performed by the installer. Close the translator and rerun the installer."
        if args.cleanup_result:
            try:
                args.cleanup_result.write_text(result[:MAX_CLEANUP_RESULT_CHARACTERS] + "\n", encoding="utf-8-sig")
            except OSError:
                return 1
        elif sys.stderr:
            print(result, file=sys.stderr)
        return 1
    if args.uninstall_cleanup:
        mutex = SingleInstance()
        try:
            if not mutex.acquire():
                raise ValueError(tr('Close the translator and its setup window, then retry uninstalling.'))
            if update_in_progress():
                raise ValueError(tr('An update is in progress. Wait for it to finish before uninstalling.'))
            report = uninstall_data(SETTINGS_PATH.parent)
            code, result = 0, "Local model, runtime, settings and log cleanup completed."
            retained = (report.get("unconfirmed_records_preserved", [])
                        + report.get("unconfirmed_preferences_preserved", [])) if isinstance(report, dict) else []
            if retained:
                code = UNINSTALL_REVIEW_EXIT_CODE
                result += ("\nManual review required: unconfirmed historical folders were preserved. "
                           "Their records remain at:\n" + "\n".join(retained))
        except Exception as exc:
            code, result = 1, str(exc)
        finally:
            mutex.close()
        if args.cleanup_result:
            try:
                args.cleanup_result.write_text(result[:MAX_CLEANUP_RESULT_CHARACTERS] + "\n", encoding="utf-8-sig")
            except OSError:
                return 1
        elif sys.stderr and code:
            print(result, file=sys.stderr)
        return code
    if args.self_test or args.diagnose:
        try:
            report = self_test() if args.self_test else diagnose()
            code = 0
        except Exception as exc:
            report, code = {"ok": False, "error": str(exc), "version": VERSION}, 1
        if args.output:
            write_json(args.output, report)
        elif sys.stdout:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return code
    mutex = SingleInstance()
    mutex_released = False
    root = None
    try:
        if not mutex.acquire():
            # Release both the mutex and this process's mapped program files.
            # The detached system notice can remain after this copy exits.
            mutex.close()
            mutex_released = True
            detached_launch_notice("duplicate")
            return 2 if args.install_model else 0
        # Check after acquiring the instance lock so a launch cannot slip
        # between data cleanup and Inno's subsequent program-file removal.
        if uninstall_in_progress():
            mutex.close()
            mutex_released = True
            detached_launch_notice("uninstall")
            return 2
        if update_in_progress():
            mutex.close()
            mutex_released = True
            detached_launch_notice("update")
            return 2
        if args.ui_language:
            save_ui_language(args.ui_language)
        cleanup_update_cache(SETTINGS_PATH.parent, strict=False)
        root = tk.Tk()
        apply_window_icon(root)
        previous_update = consume_update_result(SETTINGS_PATH.parent)
        if previous_update and previous_update.get("status") == "error":
            root.withdraw()
            messagebox.showwarning(tr('Translator update'),
                                   previous_update.get("message", "The last update could not be completed. Retry from Settings."),
                                   parent=root)
        window = SetupWindow(root, automatic=not (args.configure or args.install_model), installer_mode=args.install_model)
        try:
            root.mainloop()
            root = None
            if args.install_model:
                return 0 if window.setup_completed else 2
            if window.launch_requested:
                import translator_app
                # Also makes source execution of this entrypoint use packaged prefs.
                translator_app.SETTINGS = SETTINGS_PATH
                location = read_json(RUNTIME_PATH)
                if not location.get("data_root"):
                    raise ValueError(tr('The configured local model folder is missing. Open setup and configure it again.'))
                translator_app.main(migrate_legacy_models=False, runtime_root=Path(location["data_root"]))
            return 0
        finally:
            # Keep setup's exact-child ownership through import/configuration
            # failures and the app's lifetime; an already-stopped child is a no-op.
            # This runs before releasing the single-instance lock.
            window.finish()
    except Exception as exc:
        if root is None:
            root = tk.Tk()
            root.withdraw()
            apply_window_icon(root)
        messagebox.showerror(tr('Lineage 2 Chat Translator'), str(exc), parent=root)
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if not mutex_released:
            mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
