"""A separate, local-only translation window for Reborn Legacy chat."""
from __future__ import annotations

import json
import argparse
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import OrderedDict, deque
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from translator_core import (ChatRecord, LogTail, OllamaClient, REPLY_LANGUAGES, MAX_CHAT_CHARACTERS,
                             UNIVERSAL_TRADE_LANGUAGE, is_universal_trade_message, read_recent_chat)
from chat_overlay import CHANNEL_COLORS, ChatOverlay, normalize_overlay_settings
from reply_composer import ReplyComposer
from game_log_setup import resolve_system_log
from app_paths import settings_file
from app_version import VERSION
from app_icon import apply_window_icon
from installer_runtime import DEFAULT_MODEL, ENDPOINT, RuntimeManager
from update_controls import UpdateControls
from app_updater import discard_update, launch_update
from tray_controls import TrayControls

PROJECT = Path(__file__).resolve().parent
SETTINGS = settings_file()
DEFAULTS = {
    "log_path": "",
    "ollama_url": ENDPOINT,
    "model": DEFAULT_MODEL,
    "target_language": "English",
    "include_notices": False,
    "show_original": False,
    "minimize_to_tray": False,
    "overlay": {},
}
BG, PANEL, FG, MUTED, ACCENT = "#111820", "#1b2632", "#edf3f8", "#a4b4c3", "#66dec3"
DISCORD_URL = "https://discord.gg/V4ph4Ww5m"

# Bound retained chat and pending work; these are in-memory limits, not logs.
MAX_QUEUED_TRANSLATIONS = 80
MAX_TRANSCRIPT_MESSAGES = 250
MAX_CACHED_TRANSLATIONS = 128
RECENT_LOG_SCAN_MESSAGES = 60
RECENT_REPLAY_MESSAGES = 8
NOTICE_CHANNELS = frozenset({10, 11})  # Client system/notice channels.

# UI scheduling uses milliseconds; the worker queue uses seconds.
UI_POLL_INTERVAL_MS = 100
LOG_POLL_INTERVAL_MS = 500
FIRST_RUN_SETUP_DELAY_MS = 250
MAX_EVENTS_PER_TICK = 100
WORKER_QUEUE_WAIT_SECONDS = 0.2
AUTOSCROLL_BOTTOM_FRACTION = 0.995  # Follow only when already near the bottom.


def load_settings(*, migrate_legacy_models: bool = True) -> dict:
    """Load known preferences only; chat text is never stored in settings."""
    settings = DEFAULTS.copy()
    try:
        saved = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    for key in ("log_path", "ollama_url", "model", "target_language"):
        if not isinstance(settings[key], str) or not settings[key].strip():
            settings[key] = DEFAULTS[key]
    settings["include_notices"] = settings["include_notices"] is True
    settings["show_original"] = settings["show_original"] is True
    settings["minimize_to_tray"] = settings["minimize_to_tray"] is True
    # An earlier open window may save its old model when it closes.
    # Migrate the removed 3B model and initial trials to the selected 9B model.
    if migrate_legacy_models and not getattr(sys, "frozen", False) and settings["model"] in ("qwen2.5:3b", "qwen3:4b", "l2-translator:4b"):
        settings["model"] = DEFAULTS["model"]
    settings["overlay"] = normalize_overlay_settings(settings.get("overlay"))
    return settings


def service_shutdown_callback(config: dict, runtime_root: Path | None = None):
    """Capture this launch's managed installation; never discover one by PID/name."""
    if config.get("ollama_url", "").rstrip("/") != ENDPOINT:
        return None
    if runtime_root is None:
        try:
            location = json.loads((PROJECT / "runtime.local.json").read_text(encoding="utf-8-sig"))
            value = location.get("data_root")
            if not isinstance(value, str) or not value.strip():
                return None
            runtime_root = Path(value)
        except (OSError, ValueError, AttributeError):
            return None
    shutdown_log = Path(runtime_root).expanduser().resolve() / "logs" / "shutdown.log"

    def report(message):
        try:
            shutdown_log.parent.mkdir(parents=True, exist_ok=True)
            with shutdown_log.open("a", encoding="utf-8") as output:
                output.write(f"{datetime.now().astimezone().isoformat()} {message}\n")
        except OSError:
            pass

    manager = RuntimeManager(runtime_root, progress=report)
    lock = threading.Lock()
    attempted = False

    def shutdown_once():
        nonlocal attempted
        with lock:
            if attempted:
                return
            attempted = True
            try:
                manager.stop_service()
            except Exception as exc:
                report(f"Could not stop the dedicated service: {exc}")
                raise

    return shutdown_once


class TranslatorApp:
    def __init__(self, root: tk.Tk, *, migrate_legacy_models: bool = True, on_shutdown=None):
        self.root = root
        self._shutdown_service = on_shutdown
        self._closing = False
        self._pending_update = None
        self._update_handed_off = False
        self._reply_focus_timer = None
        self._reply_focus_request = 0
        self.config = load_settings(migrate_legacy_models=migrate_legacy_models)
        self.jobs: queue.Queue = queue.Queue(maxsize=MAX_QUEUED_TRANSLATIONS)
        self.events: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.tail: LogTail | None = None
        self.reading = False
        self.connected = False
        self.completed = 0
        self.busy = False
        self.screen_generation = 0
        self.last_file_error = ""
        self.translation_error = False
        self._choosing_system_folder = False
        self._rows = deque()
        self._reply_records = {}
        self._row_sequence = 0
        self.target = tk.StringVar(value=self.config["target_language"])
        self.log_path = tk.StringVar(value=self.config["log_path"])
        self.notices = tk.BooleanVar(value=self.config["include_notices"])
        self.show_original = tk.BooleanVar(value=self.config["show_original"])
        self.minimize_to_tray = tk.BooleanVar(value=self.config["minimize_to_tray"])
        self.on_top = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Connecting to local Ollama…")
        self.counter = tk.StringVar(value="No messages translated yet")
        self.connection = tk.StringVar(value="CONNECTING")
        self._build()
        self.overlay = ChatOverlay(self.root, self.config["overlay"], on_change=self._overlay_changed,
                                   on_reply=self.select_reply, model=self.config["model"])
        self.overlay_button.configure(text="Hide overlay" if self.overlay.settings["enabled"] else "Show overlay")
        self.tray = TrayControls(self.root, enabled=self.minimize_to_tray.get,
                                 on_exit=self.close, on_error=self.status.set)
        threading.Thread(target=self._worker, daemon=True, name="local-translator").start()
        self.check_connection()
        self.root.after(UI_POLL_INTERVAL_MS, self._drain_events)
        self.root.after(LOG_POLL_INTERVAL_MS, self._poll_log)
        self.root.after(FIRST_RUN_SETUP_DELAY_MS, self._first_run_log_setup)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self):
        self.root.title(f"Lineage 2 Chat Translator v{VERSION} — Local Ollama")
        self.root.geometry("940x700")
        self.root.minsize(760, 540)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", ("Segoe UI", 10))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background="#2b3c4d", foreground=FG, padding=(9, 5), borderwidth=0)
        style.map("TButton", background=[("active", "#3b5268")], foreground=[("disabled", "#708190")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#10251f")
        style.map("Accent.TButton", background=[("active", "#8cebd6")])
        style.configure("Link.TButton", background=BG, foreground=ACCENT,
                        font=("Segoe UI", 9), padding=(2, 2))
        style.map("Link.TButton", background=[("active", BG)],
                  foreground=[("active", FG)])
        style.configure("TCheckbutton", background=BG, foreground=MUTED)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, padding=3)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG, padding=4)
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

        outer = ttk.Frame(self.root, padding=12)
        self._content = outer
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1, minsize=130)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(header, text="Lineage 2 Chat Translator", bg=BG, fg=FG,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        self.settings_button = ttk.Button(header, text="Settings", command=self.toggle_settings)
        self.settings_button.pack(side="right")
        tk.Label(header, textvariable=self.connection, bg=BG, fg=ACCENT,
                 font=("Segoe UI", 9, "bold"), padx=12).pack(side="right")
        tk.Label(header, text=f"· {self.config['model']}", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True, padx=(12, 0))

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.read_button = ttk.Button(toolbar, text="Start reading", style="Accent.TButton", command=self.toggle_reading)
        self.read_button.pack(side="left", padx=(0, 6))
        self.recent_button = ttk.Button(toolbar, text="Translate recent chat", command=self.translate_recent)
        self.recent_button.pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Clear", command=self.clear).pack(side="left", padx=(0, 6))
        self.overlay_button = ttk.Button(toolbar, text="Show overlay", command=lambda: self.overlay.toggle())
        self.overlay_button.pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Overlay settings…", command=lambda: self.overlay.open_settings()).pack(side="left")

        self.settings_panel = ttk.Frame(outer)
        self.settings_panel.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        controls = ttk.Frame(self.settings_panel)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Label(controls, text="Translate to").pack(side="left", padx=(0, 6))
        ttk.Combobox(controls, textvariable=self.target, state="readonly", width=12,
            values=("English", "Russian", "Spanish", "Portuguese", "German", "French", "Polish", "Greek", "Korean", "Japanese", "Chinese")).pack(side="left")
        ttk.Checkbutton(controls, text="Original text", variable=self.show_original,
                        command=self._render_rows).pack(side="left", padx=8)
        ttk.Checkbutton(controls, text="Include notices", variable=self.notices).pack(side="left")
        ttk.Checkbutton(controls, text="Main window on top", variable=self.on_top,
                        command=lambda: self.root.attributes("-topmost", self.on_top.get())).pack(side="right")
        ttk.Checkbutton(self.settings_panel, text="Minimize to system tray", variable=self.minimize_to_tray,
                        command=self._tray_setting_changed).pack(anchor="w", pady=(0, 6))
        source = ttk.Frame(self.settings_panel)
        source.pack(fill="x", pady=(0, 6))
        ttk.Label(source, text="Chat log", foreground=MUTED).pack(side="left", padx=(0, 8))
        self.path_entry = ttk.Entry(source, textvariable=self.log_path)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.path_entry.bind("<Return>", lambda _: self.restart_log())
        ttk.Button(source, text="Log file…", command=self.browse).pack(side="right")
        ttk.Button(source, text="System folder…", command=self.choose_system_folder).pack(side="right", padx=(0, 6))
        self.updates = UpdateControls(self.settings_panel, current_version=VERSION,
                                      on_ready=self._update_ready)
        self.updates.frame.pack(fill="x", pady=(3, 0))
        self.recent_button.configure(state="disabled")
        self.settings_panel.grid_remove()

        display = ttk.Frame(outer)
        display.grid(row=3, column=0, sticky="nsew")
        self.transcript = tk.Text(display, bg=PANEL, fg=FG, relief="flat", borderwidth=0,
            padx=8, pady=6, height=12, wrap="word", state="disabled", font=("Segoe UI", 10),
            insertbackground=FG, selectbackground="#375869", spacing1=0, spacing3=2)
        self.transcript.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(display, command=self.transcript.yview)
        scrollbar.pack(side="right", fill="y")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("meta", foreground=MUTED, font=("Segoe UI", 9))
        self.transcript.tag_configure("sender", font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("original", foreground=MUTED, font=("Segoe UI", 9))
        self.transcript.tag_configure("translation", foreground=FG)
        self.transcript.tag_configure("note", foreground=MUTED)
        self.transcript.tag_configure("error", foreground="#ffbb9d")
        self.transcript.tag_configure("reply", foreground=ACCENT, underline=True, font=("Segoe UI", 9))
        self.transcript.tag_bind("reply", "<Button-1>", self._reply_clicked)
        self.transcript.tag_bind("reply", "<Enter>", lambda _: self.transcript.configure(cursor="hand2"))
        self.transcript.tag_bind("reply", "<Leave>", lambda _: self.transcript.configure(cursor="arrow"))
        for channel, color in CHANNEL_COLORS.items():
            self.transcript.tag_configure(f"channel{channel}", foreground=color)
        self._note("Waiting for new chat. Use Translate recent chat to read saved messages.")

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(5, 8))
        ttk.Label(footer, textvariable=self.status, foreground=MUTED, font=("Segoe UI", 9), wraplength=500).pack(side="left")
        ttk.Label(footer, textvariable=self.counter, foreground=MUTED, font=("Segoe UI", 9)).pack(side="right")
        self.reply = ReplyComposer(outer, self.config)
        self.reply.frame.grid(row=5, column=0, sticky="ew")
        ttk.Button(outer, text="Discord — feedback and bug reports", style="Link.TButton",
                   cursor="hand2", command=self._open_discord).grid(
                       row=6, column=0, sticky="e", pady=(5, 0))

    def _open_discord(self):
        """Open the fixed community invite only on a user's click; send no chat data."""
        try:
            if webbrowser.open_new_tab(DISCORD_URL):
                return
        except (OSError, webbrowser.Error):
            pass
        self.status.set(f"Could not open Discord. Visit {DISCORD_URL}")

    def toggle_settings(self):
        if self.settings_panel.winfo_ismapped():
            self.settings_panel.grid_remove()
            self.settings_button.configure(text="Settings")
        else:
            self.settings_panel.grid()
            self.settings_button.configure(text="Hide settings")
            if hasattr(self, "updates"):
                self.updates.settings_shown()

    def _update_ready(self, prepared):
        """Save preferences and close the owned session before installation."""
        pending = getattr(self, "_pending_update", None)
        if pending is prepared:
            return
        if self._closing or self.stop.is_set() or pending is not None:
            discard_update(prepared)
            return
        if not self.save_settings():
            # The controls retain cleanup ownership when this callback rejects
            # a job, and restore the Install button so the user can retry.
            raise ValueError("Your preferences could not be saved. Check the folder permissions and try again.")
        self._pending_update = prepared
        self.close()

    def _tray_setting_changed(self):
        """Persist the opt-in immediately; disabling it restores a hidden root."""
        self.save_settings()
        self.tray.preference_changed()

    def select_reply(self, record, *, whisper=False):
        if self.stop.is_set() or getattr(self, "_closing", False):
            return
        self._cancel_reply_focus()
        if hasattr(self, "tray"):
            self.tray.restore()
        else:
            self.root.deiconify()
            self.root.lift()
        self.reply.select(record, whisper=whisper)
        # The overlay click can still change focus while its bindings run.
        # Activate the input after dispatch, when the restored root is mapped.
        request = self._reply_focus_request
        self._reply_focus_timer = self.root.after_idle(lambda: self._focus_reply_input(request))

    def _cancel_reply_focus(self):
        self._reply_focus_request = getattr(self, "_reply_focus_request", 0) + 1
        timer = getattr(self, "_reply_focus_timer", None)
        self._reply_focus_timer = None
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except tk.TclError:
                pass

    def _focus_reply_input(self, request):
        if request != self._reply_focus_request:
            return
        self._reply_focus_timer = None
        if self.stop.is_set() or getattr(self, "_closing", False):
            return
        try:
            # focus_set only remembers focus when another app is foreground.
            # This stronger request is limited to the user's reply click.
            self.reply.input.focus_force()
        except tk.TclError:
            pass  # The input may have been destroyed during application close.

    def _reply_clicked(self, event):
        for tag in self.transcript.tag_names(f"@{event.x},{event.y}"):
            record = self._reply_records.get(tag)
            if record is not None:
                self.select_reply(record)
                break
        return "break"

    def _note(self, text: str, error: bool = False):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text + "\n", "error" if error else "note")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _forget_row(self, tag):
        self._reply_records.pop(tag, None)
        self.transcript.tag_delete(tag)
        self.transcript.mark_unset(tag)

    def _trim(self):
        """Drop old transcript text and its matching clickable reply records."""
        while len(self._rows) > MAX_TRANSCRIPT_MESSAGES:
            tag, _ = self._rows.popleft()
            self.transcript.delete("1.0", self._rows[0][0])
            self._forget_row(tag)

    def _write_row(self, tag, data):
        record, translated, target, source, seconds, error = data
        one_line = lambda text: " ".join(str(text).split())
        self.transcript.mark_set(tag, "end-1c")
        self.transcript.mark_gravity(tag, "left")
        self._reply_records[tag] = record
        color = f"channel{record.channel}" if record.channel in CHANNEL_COLORS else "translation"
        self.transcript.insert("end", f"{record.timestamp}  ", "meta")
        self.transcript.insert("end", one_line(record.sender or "Notice") + ": ", (color, "sender"))
        self.transcript.insert("end", one_line(record.message if error else translated), color)
        self.transcript.insert("end", "  Reply", ("reply", tag))
        self.transcript.insert("end", "\n", "translation")
        if error:
            self.transcript.insert("end", "  Translation unavailable: " + one_line(translated) + "\n", "error")
        elif self.show_original.get() and record.message.strip() != translated.strip():
            self.transcript.insert("end", "  " + one_line(record.message) + "\n", "original")

    def _render_rows(self):
        follow = self.transcript.yview()[1] >= AUTOSCROLL_BOTTOM_FRACTION
        position = self.transcript.yview()[0]
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        for tag, data in self._rows:
            self._forget_row(tag)
            self._write_row(tag, data)
        self.transcript.configure(state="disabled")
        if follow:
            self.transcript.see("end")
        else:
            self.transcript.yview_moveto(position)

    def _append(self, record, translated, target, source, seconds, error=False):
        follow = not self._rows or self.transcript.yview()[1] >= AUTOSCROLL_BOTTOM_FRACTION
        self.transcript.mark_set("main_view", self.transcript.index("@0,0"))
        self.transcript.configure(state="normal")
        self._row_sequence += 1
        tag = f"main_reply_{self._row_sequence}"
        data = (record, translated, target, source, seconds, error)
        self._rows.append((tag, data))
        self._write_row(tag, data)
        self._trim()
        self.transcript.configure(state="disabled")
        if follow:
            self.transcript.see("end")
        else:
            self.transcript.yview("main_view")
        self.overlay.add_message(record, translated, source=source, error=error)
        if not error:
            self.completed += 1
            self.counter.set(f"{self.completed} translated · {seconds:.1f}s last")

    def check_connection(self):
        # Capture Tk values on the UI thread; worker threads never access Tk.
        target_language = self.target.get()
        def safe_check():
            try:
                client = OllamaClient(self.config["ollama_url"], self.config["model"], target_language)
                models = client.list_models()
                if self.config["model"] not in models:
                    raise RuntimeError(f"Model {self.config['model']} is not installed. Run Setup Ollama.ps1.")
                self.events.put(("connected", None))
            except Exception as exc:
                self.events.put(("connection_error", str(exc)))
        threading.Thread(target=safe_check, daemon=True, name="ollama-check").start()

    def toggle_reading(self):
        if self.reading:
            self.reading = False
            if self.tail:
                self.tail.close()
                self.tail = None
            self.read_button.configure(text="Start reading")
            self.status.set("Reading paused. Queued translations will finish.")
        elif not self.log_path.get().strip():
            self.choose_system_folder()
        elif not self.connected:
            self.status.set("Checking local Ollama…")
            self.check_connection()
        else:
            self.tail = LogTail(self.log_path.get(), start_at_end=True)
            self.last_file_error = ""
            try:
                # Establish the cursor now, so messages arriving before the
                # first scheduled poll are not mistaken for old chat.
                self.tail.poll()
            except (OSError, ValueError) as exc:
                self.tail.close()
                self.tail = None
                self._note("Could not read chat log: " + str(exc), error=True)
                return
            self.reading = True
            self.read_button.configure(text="Pause reading")
            self.status.set("Watching for new chat…")

    def _poll_log(self):
        """Read new records from the shared read-only log and queue bounded work."""
        if self.stop.is_set():
            return
        if self.reading and self.tail:
            try:
                for record in self.tail.poll():
                    if self.notices.get() or record.channel not in NOTICE_CHANNELS:
                        self.enqueue(record, "Live")
                if not Path(self.log_path.get()).exists():
                    self.status.set("Waiting for the chat log to appear…")
                elif not self.busy and self.jobs.empty() and not self.translation_error:
                    self.status.set("Watching for new chat…")
                self.last_file_error = ""
            except (OSError, ValueError) as exc:
                detail = str(exc)
                if detail != self.last_file_error:
                    self._note("Could not read chat log: " + detail, error=True)
                    self.last_file_error = detail
                self.status.set("Chat log unavailable; retrying…")
        self.root.after(LOG_POLL_INTERVAL_MS, self._poll_log)

    def enqueue(self, record, source):
        """Reject oversized/overflow work instead of building an unbounded backlog."""
        if not record.message.strip():
            return
        if len(record.message) > MAX_CHAT_CHARACTERS:
            self._note(f"Skipped an unusually long chat entry (over {MAX_CHAT_CHARACTERS:,} characters).", error=True)
            return
        try:
            self.jobs.put_nowait((record, self.target.get(), source, self.screen_generation))
            self.status.set(f"Translating locally · {self.jobs.qsize()} queued")
        except queue.Full:
            self._append(record, "The queue is full. This message was not translated.", self.target.get(), source, 0, error=True)

    def translate_recent(self):
        if not self.log_path.get().strip():
            self.choose_system_folder()
            return
        try:
            records = read_recent_chat(self.log_path.get(), limit=RECENT_LOG_SCAN_MESSAGES)
            if not self.notices.get():
                records = [r for r in records if r.channel not in NOTICE_CHANNELS]
            if not records:
                self._note("No matching chat entries found in this log.")
            for record in records[-RECENT_REPLAY_MESSAGES:]:
                self.enqueue(record, "Saved chat")
        except (OSError, ValueError) as exc:
            self._note("Could not read saved chat: " + str(exc), error=True)

    def _worker(self):
        """Translate sequentially off Tk; keep only a bounded, memory-only cache."""
        clients = {}
        cache = OrderedDict()
        while not self.stop.is_set():
            try:
                record, target, source, generation = self.jobs.get(timeout=WORKER_QUEUE_WAIT_SECONDS)
            except queue.Empty:
                continue
            started = time.monotonic()
            self.events.put(("working", None))
            try:
                if self.stop.is_set():
                    break
                # Identical visible text can come from plain chat or a party
                # link; only the latter carries immutable range/count details.
                key = (target, record.message, record.item_links, record.party_details)
                if key in cache:
                    translated, source_language = cache[key]
                    cache.move_to_end(key)
                else:
                    if target not in clients:
                        clients[target] = OllamaClient(self.config["ollama_url"], self.config["model"], target)
                    options = {"item_links": record.item_links} if record.item_links else {}
                    if record.party_details:
                        options["party_details"] = record.party_details
                    translated = clients[target].translate(record.message, **options)
                    if self.stop.is_set():
                        break
                    try:
                        if is_universal_trade_message(record.message, **options):
                            source_language = UNIVERSAL_TRADE_LANGUAGE
                        else:
                            language = clients[target].detect_language(record.message, **options)
                            source_language = language if language in REPLY_LANGUAGES else "Unknown"
                    except Exception:
                        # An unavailable language label must not discard a good
                        # translation. Reply can retry detection when clicked.
                        source_language = None
                    cache[key] = (translated, source_language)
                    if len(cache) > MAX_CACHED_TRANSLATIONS:
                        cache.popitem(last=False)
                record = replace(record, source_language=source_language)
                self.events.put(("result", (record, translated, target, source, time.monotonic()-started, False, generation)))
            except Exception as exc:
                self.events.put(("result", (record, str(exc), target, source, 0, True, generation)))
            finally:
                self.jobs.task_done()

    def _drain_events(self):
        """Apply worker results on Tk's thread, ignoring results from before Clear."""
        if self.stop.is_set():
            return
        for _ in range(MAX_EVENTS_PER_TICK):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                was_connected = self.connected
                self.connected = True
                self.connection.set("LOCAL · READY")
                self.reply.set_connected(True)
                self.translation_error = False
                self.recent_button.configure(state="normal")
                if not was_connected and not self.reading:
                    if self.log_path.get().strip():
                        self.toggle_reading()
                    else:
                        self.status.set("Choose your Lineage II system folder to read chat.")
            elif kind == "connection_error":
                self.connected = False
                self.connection.set("OLLAMA OFFLINE")
                self.reply.set_connected(False)
                self.read_button.configure(text="Retry connection")
                self.recent_button.configure(state="disabled")
                self.status.set("Start Ollama with Start Translator.cmd, then click Start reading.")
                self._note(data, error=True)
            elif kind == "working":
                self.busy = True
                self.status.set(f"Translating locally · {self.jobs.qsize()} queued")
            elif kind == "result":
                self.busy = False
                if data[-1] == self.screen_generation:
                    self._append(*data[:-1])
                    self.translation_error = data[-2]
                    self.connection.set("TRANSLATION ERROR" if self.translation_error else "LOCAL · READY")
                self.status.set(f"Translating locally · {self.jobs.qsize()} queued" if not self.jobs.empty()
                                else "Translation failed · See the message above" if self.translation_error
                                else "Watching for new chat…" if self.reading else "Ready · reading paused")
        self.root.after(UI_POLL_INTERVAL_MS, self._drain_events)

    def clear(self):
        """Clear visible/queued chat; the worker's bounded cache lasts until exit."""
        self._cancel_reply_focus()
        self.screen_generation += 1
        while True:
            try:
                self.jobs.get_nowait()
                self.jobs.task_done()
            except queue.Empty:
                break
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        for tag, _ in self._rows:
            self._forget_row(tag)
        self._rows.clear()
        self.reply.clear_selection()
        self.completed = 0
        self.counter.set("No messages translated yet")
        self.overlay.clear()
        self.translation_error = False
        self.connection.set("LOCAL · READY" if self.connected else "OLLAMA OFFLINE")
        self.status.set("Finishing the current request…" if self.busy else
                        "Watching for new chat…" if self.reading else
                        "Ready · reading paused" if self.connected else "Connect to local Ollama before translating.")

    def _first_run_log_setup(self):
        if not self.log_path.get().strip() and not self.stop.is_set():
            # Setup collects the game folder before downloading the model.
            # Older/source installations may still lack a path: explain the
            # choice inline instead of opening a modal picker on startup.
            self.settings_panel.grid()
            self.settings_button.configure(text="Hide settings")
            self.status.set("Choose your game folder with Settings → System folder… to read chat.")

    def choose_system_folder(self):
        if self._choosing_system_folder:
            return
        self._choosing_system_folder = True
        try:
            selected = filedialog.askdirectory(parent=self.root,
                title="Choose your Lineage II system folder", mustexist=True,
                initialdir=str(Path(self.log_path.get()).parent) if self.log_path.get().strip() else str(Path.home()))
        finally:
            self._choosing_system_folder = False
        if not selected:
            if not self.log_path.get().strip():
                self.status.set("Choose a system folder in Settings when you are ready to read chat.")
            return
        try:
            log = resolve_system_log(selected)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Choose a Lineage II folder", str(exc), parent=self.root)
            if not self.log_path.get().strip():
                self.status.set("Choose your Lineage II system folder under Settings → System folder…")
            return
        self.log_path.set(str(log))
        saved = self.restart_log()
        if self.connected and not self.reading:
            self.toggle_reading()
        if not saved:
            self._note("This chat log is selected for now, but the preference could not be saved. Check that the translator folder is writable.", error=True)
        if not log.is_file():
            self._note("Chat log path selected. Start the game yourself; chat can appear here when the client creates Network.log and records messages.")

    def browse(self):
        selected = filedialog.askopenfilename(parent=self.root, title="Choose the Lineage II chat log",
            initialdir=str(Path(self.log_path.get()).parent) if self.log_path.get().strip() else str(Path.home()),
            filetypes=(("Log files", "*.log"), ("All files", "*.*")))
        if selected:
            self.log_path.set(selected)
            self.restart_log()

    def restart_log(self):
        if self.reading:
            self.toggle_reading()
            self.toggle_reading()
        return self.save_settings()

    def _overlay_changed(self, settings):
        self.config["overlay"] = dict(settings)
        self.overlay_button.configure(text="Hide overlay" if settings["enabled"] else "Show overlay")
        if not self.stop.is_set():
            self.save_settings()

    def save_settings(self):
        """Replace preferences atomically; never serialize transcript or replies."""
        self.config.update(log_path=self.log_path.get(), target_language=self.target.get(), include_notices=self.notices.get(),
                           show_original=self.show_original.get())
        if hasattr(self, "minimize_to_tray"):
            self.config["minimize_to_tray"] = self.minimize_to_tray.get() is True
        if hasattr(self, "overlay"):
            self.config["overlay"] = dict(self.overlay.settings)
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            temporary = SETTINGS.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
            temporary.replace(SETTINGS)
            return True
        except OSError:
            self.status.set("Could not save preferences. Check that the project folder is writable.")
            return False

    def close(self):
        """Stop input, save preferences, and finish service/update work before exit."""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self.stop.set()
        self._cancel_reply_focus()
        self._close_errors = []
        # A stale widget or failed log close must not skip service shutdown.
        cleanup = [("Saving preferences", self.save_settings),
                   ("Closing replies", self.reply.close),
                   ("Closing the overlay", self.overlay.destroy)]
        tray = getattr(self, "tray", None)
        if tray is not None:
            cleanup.insert(0, ("Removing the system tray icon", tray.close))
        if hasattr(self, "updates"):
            cleanup.insert(0, ("Canceling update checks", self.updates.close))
        if self.tail:
            cleanup.insert(0, ("Closing the chat log", self.tail.close))
        for label, action in cleanup:
            try:
                if action() is False:
                    self._close_errors.append(f"{label}: could not complete this step.")
            except Exception as exc:
                self._close_errors.append(f"{label}: {exc}")
        shutdown = getattr(self, "_shutdown_service", None)
        self._shutdown_results = queue.Queue(maxsize=1)
        self._shutdown_progress = None
        pending_update = getattr(self, "_pending_update", None)
        if shutdown is None and pending_update is None and (tray is None or tray.wait_closed(0)):
            self._shutdown_results.put(None)
            self._finish_shutdown()
            return
        # Keep the window visible while the launcher still holds its instance
        # lock. Network/process checks must not block Tk's event thread.
        try:
            self._show_closing_state()
        except Exception as exc:
            # The service still needs to stop if a widget has disappeared.
            self._close_errors.append(f"Showing closing progress: {exc}")

        def work():
            failure = None
            try:
                shutdown_errors = []
                if shutdown is not None:
                    try:
                        shutdown()
                    except Exception as exc:
                        shutdown_errors.append(str(exc))
                # A tray failure must not skip backend shutdown. Wait off Tk's
                # thread and refuse update handoff while tray code is still live.
                if tray is not None:
                    try:
                        if not tray.wait_closed():
                            raise RuntimeError("Windows could not finish system tray cleanup. Reopen the translator and retry.")
                    except Exception as exc:
                        shutdown_errors.append(str(exc))
                if shutdown_errors:
                    raise RuntimeError("\n".join(shutdown_errors))
                if pending_update is not None:
                    if self._close_errors:
                        raise RuntimeError("The update was canceled because shutdown could not finish cleanly.")
                    launch_update(pending_update)
                    self._update_handed_off = True
            except Exception as exc:
                failure = str(exc)
                if pending_update is not None and not getattr(self, "_update_handed_off", False):
                    try:
                        discard_update(pending_update)
                    except Exception as cleanup_error:
                        failure += " Temporary update cleanup could not finish: " + str(cleanup_error)
            self._shutdown_results.put(failure)

        self._shutdown_thread = threading.Thread(target=work, daemon=False, name="stop-local-ollama")
        self._shutdown_thread.start()
        self.root.after(UI_POLL_INTERVAL_MS, self._finish_shutdown)

    def _show_closing_state(self):
        # Remove old controls and their key bindings, including focused entries.
        # Reader/reply callbacks are already stopped before destroying widgets.
        self._content.destroy()
        updating = getattr(self, "_pending_update", None) is not None
        self.root.title("Lineage 2 Chat Translator — " + ("Updating…" if updating else "Closing…"))
        frame = ttk.Frame(self.root, padding=32)
        frame.pack(expand=True)
        ttk.Label(frame, text="Updating—please wait…" if updating else "Closing—please wait…", font=("Segoe UI", 16, "bold")).pack(pady=(0, 12))
        details = ("Stopping the local Ollama service before installing the update.\nThe translator will reopen automatically."
                   if updating else "Stopping the local Ollama service.\nThis window will close automatically when shutdown finishes.")
        ttk.Label(frame, text=details,
                  justify="center", foreground=MUTED).pack(pady=(0, 18))
        self._shutdown_progress = ttk.Progressbar(frame, mode="indeterminate", length=360)
        self._shutdown_progress.pack()
        self._shutdown_progress.start(20)
        self.root.deiconify()
        self.root.focus_set()

    def _finish_shutdown(self):
        try:
            failure = self._shutdown_results.get_nowait()
        except queue.Empty:
            self.root.after(UI_POLL_INTERVAL_MS, self._finish_shutdown)
            return
        try:
            if self._shutdown_progress is not None:
                try:
                    self._shutdown_progress.stop()
                except tk.TclError:
                    pass  # The progress widget may already have been destroyed.
            errors = list(self._close_errors)
            if failure is not None:
                errors.append(("Update handoff failed: " if getattr(self, "_pending_update", None) is not None
                               else "Could not confirm that the local Ollama service stopped: ") + failure)
            if errors:
                explanation = ("The update was not installed. The translator will close; reopen it and retry the update.\n\n"
                               if getattr(self, "_pending_update", None) is not None
                               else "The translator will close, but some shutdown steps could not be completed.\n\n")
                messagebox.showwarning("Translator shutdown",
                    explanation + "\n\n".join(errors), parent=self.root)
        finally:
            self.root.destroy()


def main(*, migrate_legacy_models: bool = True, runtime_root: Path | None = None):
    shutdown = service_shutdown_callback(load_settings(migrate_legacy_models=migrate_legacy_models), runtime_root)
    root = None
    application = None
    try:
        root = tk.Tk()
        apply_window_icon(root)
        application = TranslatorApp(root, migrate_legacy_models=migrate_legacy_models, on_shutdown=shutdown)
        root.mainloop()
    except Exception as exc:
        if root is not None:
            messagebox.showerror("Lineage 2 Chat Translator", str(exc), parent=root)
            root.destroy()
        raise
    finally:
        if application is not None:
            try:
                application.tray.close()
                application.tray.wait_closed()
            except Exception as exc:
                if sys.stderr:
                    print(f"Could not remove the system tray icon: {exc}", file=sys.stderr)
        # Covers startup failure or a main-loop exit other than the X button.
        # The captured callback serializes concurrent calls and runs only once.
        if shutdown is not None:
            try:
                shutdown()
            except Exception as exc:
                if sys.stderr:
                    print(f"Could not stop the dedicated local service: {exc}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lineage 2 Chat Translator")
    parser.add_argument("--runtime-root", type=Path)
    main(runtime_root=parser.parse_args().runtime_root)
