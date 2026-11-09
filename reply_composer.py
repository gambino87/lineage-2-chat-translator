"""A compact local reply translator. Submitted replies are translated and copied."""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import ttk

from translator_core import MAX_CHAT_CHARACTERS, OllamaClient, REPLY_LANGUAGES, UNIVERSAL_TRADE_LANGUAGE


CHOOSE_LANGUAGE = "Choose language"
DETECTING_LANGUAGE = "Detecting…"
# Keep UI work small while one local inference request runs off the Tk thread.
REPLY_EVENT_POLL_MS = 100
REPLY_EVENTS_PER_POLL = 20
REPLY_WORKER_POLL_SECONDS = 0.2


def whisper_recipient(sender: str) -> str:
    """Return one exact player-name token, never a guessed or shortened name."""
    if not isinstance(sender, str) or any(not char.isprintable() for char in sender):
        return ""
    name = sender.strip()
    if not name or any(char.isspace() or char in "\"'‘’“”" for char in name):
        return ""
    return name


@dataclass(frozen=True)
class ReplyJob:
    kind: str
    selection: int
    revision: int
    text: str
    target: str = "English"
    item_links: tuple[str, ...] = ()
    recipient: str = ""
    party_details: tuple[str, ...] = ()


class ReplyComposer:
    """Owns a small Tk panel plus one coalescing background-request worker.

    Tk variables are accessed only on the UI thread. Selection and revision
    numbers prevent old requests from changing a newer reply or language choice.
    """

    def __init__(self, parent, config):
        self.config = {key: config[key] for key in ("ollama_url", "model")}
        self.root = parent.winfo_toplevel()
        self.connected = False
        self._closed = False
        self._stop = threading.Event()
        self._jobs = queue.Queue(maxsize=1)
        self._events = queue.Queue()
        self._selection = 0
        self._revision = 0
        self._record = None
        self._recipient = ""
        self._translating = None
        self._waiting_reply = None
        self._result_context = None
        self._timer = None
        self.title = tk.StringVar(master=parent, value="Reply · choose a message above")
        self.language = tk.StringVar(master=parent, value=CHOOSE_LANGUAGE)
        self.draft = tk.StringVar(master=parent)
        self.output = tk.StringVar(master=parent)
        self.status = tk.StringVar(master=parent, value="Click Reply, write in English, then press Enter to translate and copy.")
        self._build(parent)
        self.draft.trace_add("write", self._draft_changed)
        self._update_actions()
        threading.Thread(target=self._worker, daemon=True, name="local-reply-translator").start()
        self._timer = self.root.after(REPLY_EVENT_POLL_MS, self._drain_events)

    def _build(self, parent):
        style = ttk.Style(parent)
        style.configure("Reply.TButton", padding=(9, 4))
        style.configure("Reply.TEntry", padding=4, fieldbackground="#1b2632", foreground="#edf3f8")
        style.map("Reply.TEntry",
                  fieldbackground=[("readonly", "#1b2632"), ("disabled", "#1b2632")],
                  foreground=[("readonly", "#edf3f8"), ("disabled", "#a4b4c3")])
        self.frame = ttk.Frame(parent, padding=(0, 6, 0, 0))
        self.frame.columnconfigure(1, weight=1)
        heading = ttk.Frame(self.frame)
        heading.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, textvariable=self.title, width=1, anchor="w",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(heading, text="Reply in", font=("Segoe UI", 9)).grid(row=0, column=1, padx=(0, 5))
        self.language_box = ttk.Combobox(heading, textvariable=self.language,
            values=REPLY_LANGUAGES, state="readonly", width=15)
        self.language_box.grid(row=0, column=2, sticky="e")
        self.language_box.bind("<<ComboboxSelected>>", self._language_changed)

        ttk.Label(self.frame, text="English", font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.input = ttk.Entry(self.frame, textvariable=self.draft, style="Reply.TEntry")
        self.input.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=2)
        self.input.bind("<Return>", self.translate)
        self.translate_button = ttk.Button(self.frame, text="Translate & copy", style="Reply.TButton", command=self.translate)
        self.translate_button.grid(row=1, column=2, sticky="ew", pady=2)
        ttk.Label(self.frame, text="Translated", font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", padx=(0, 8))
        self.result = ttk.Entry(self.frame, textvariable=self.output, state="readonly", style="Reply.TEntry")
        self.result.grid(row=2, column=1, sticky="ew", padx=(0, 6), pady=2)
        self.copy_button = ttk.Button(self.frame, text="Copy again", style="Reply.TButton", command=self.copy)
        self.copy_button.grid(row=2, column=2, sticky="ew", pady=2)
        ttk.Label(self.frame, textvariable=self.status, foreground="#a4b4c3",
                  font=("Segoe UI", 8), wraplength=680).grid(row=3, column=0, columnspan=3, sticky="w", pady=(3, 0))

    def _drop_pending(self):
        while True:
            try:
                self._jobs.get_nowait()
                self._jobs.task_done()
            except queue.Empty:
                break

    def _queue_latest(self, job):
        self._drop_pending()
        self._jobs.put_nowait(job)

    def _clear_output(self):
        self.output.set("")
        self._result_context = None

    def _invalidate_selection(self):
        """Make every earlier result ineligible to change or copy a newer reply."""
        self._selection += 1
        self._revision += 1
        self._translating = None
        self._waiting_reply = None
        self._drop_pending()
        self._clear_output()

    def _valid_result(self):
        return (self._result_context is not None and bool(self.output.get())
                and self._result_context == (
                    self._selection, self._revision, self.draft.get().strip(),
                    self.language.get(), self._recipient))

    def _update_actions(self):
        subject = f"Reply to {self._recipient}" if self._recipient else "Reply"
        language = self.language.get()
        if language in REPLY_LANGUAGES:
            heading = f"{subject} in {language}"
        elif language == DETECTING_LANGUAGE:
            heading = f"{subject} · detecting language…"
        elif self._record is not None:
            heading = f"{subject} · choose language"
        else:
            heading = "Reply · choose a message above"
        self.title.set(heading)
        valid = (self.connected and self.language.get() in (*REPLY_LANGUAGES, DETECTING_LANGUAGE)
                 and bool(self.draft.get().strip()) and self._translating is None
                 and self._waiting_reply is None)
        self.translate_button.configure(state="normal" if valid else "disabled")
        self.copy_button.configure(state="normal" if self._valid_result() else "disabled")

    def select(self, record, *, whisper=False):
        """Choose a language/recipient without sending any keystrokes to the game."""
        if self._closed:
            return
        self._invalidate_selection()
        self._recipient = whisper_recipient(record.sender) if whisper else ""
        if whisper and not self._recipient:
            self._record = None
            self.language.set(CHOOSE_LANGUAGE)
            self.status.set("This message has no valid player name for a whisper. Choose another message.")
            self._update_actions()
            return
        self._record = record
        if record.source_language in REPLY_LANGUAGES:
            self.language.set(record.source_language)
            self.status.set(f"Reply in {record.source_language}. Press Enter to translate and copy." if self.connected
                            else "Local model is offline. Reconnect to translate your reply.")
        elif record.source_language in ("Unknown", UNIVERSAL_TRADE_LANGUAGE):
            self.language.set(CHOOSE_LANGUAGE)
            self.status.set("Universal trade message. Choose a reply language, then press Enter to translate and copy."
                            if record.source_language == UNIVERSAL_TRADE_LANGUAGE else
                            "Language is unclear. Choose it manually, then press Enter to translate and copy.")
        elif self.connected:
            self.language.set(DETECTING_LANGUAGE)
            self.status.set("Identifying the original language locally… You can start typing your English reply.")
            self._queue_latest(ReplyJob("detect", self._selection, self._revision,
                                       record.message, item_links=record.item_links,
                                       recipient=self._recipient, party_details=record.party_details))
        else:
            self.language.set(CHOOSE_LANGUAGE)
            self.status.set("Local model is offline. You can choose a reply language while reconnecting.")
        self._update_actions()
        self.input.focus_set()
        self.input.selection_range(0, "end")

    def set_connected(self, connected):
        if self._closed:
            return
        was_connected = self.connected
        self.connected = bool(connected)
        if not self.connected:
            self._invalidate_selection()
            if self.language.get() == DETECTING_LANGUAGE:
                self.language.set(CHOOSE_LANGUAGE)
            self.status.set("Connect to local Ollama to translate a reply.")
        elif not was_connected:
            if self._record is not None and self.language.get() not in REPLY_LANGUAGES:
                self.select(self._record, whisper=bool(self._recipient))
                return
            self.status.set("Choose a message or reply language, then write in English.")
        self._update_actions()

    def _language_changed(self, _event=None):
        if self._closed:
            return
        self._invalidate_selection()
        self.status.set(f"Reply language: {self.language.get()}. Press Enter to translate and copy.")
        self._update_actions()
        self.input.focus_set()

    def _draft_changed(self, *_args):
        if self._closed:
            return
        self._revision += 1
        self._translating = None
        self._waiting_reply = None
        self._clear_output()
        # Typing does not cancel a pending source-language detection.
        if self.connected and self.language.get() in REPLY_LANGUAGES:
            self.status.set("Press Enter to translate and copy your reply.")
        elif self.language.get() == DETECTING_LANGUAGE:
            self.status.set("Identifying the original language… Press Enter when your reply is ready.")
        self._update_actions()

    def translate(self, _event=None):
        if self._closed:
            return "break"
        text = self.draft.get().strip()
        target = self.language.get()
        if not self.connected:
            self.status.set("Connect to local Ollama to translate a reply.")
        elif not text:
            self.status.set("Type a sentence in English first.")
        elif len(text) > MAX_CHAT_CHARACTERS:
            self._clear_output()
            self.status.set("Please keep the reply under 4,000 characters.")
        elif target == DETECTING_LANGUAGE:
            self._waiting_reply = (self._selection, self._revision, text, self._recipient)
            self.status.set("Reply ready. Waiting for the language, then translating and copying…")
        elif target not in REPLY_LANGUAGES:
            self.status.set("Choose the reply language first; short game messages can be ambiguous.")
        elif self._translating is None:
            self._waiting_reply = None
            self._revision += 1
            self._clear_output()
            self._translating = (self._selection, self._revision)
            self._queue_latest(ReplyJob("translate", self._selection, self._revision, text, target,
                                       recipient=self._recipient))
            self.status.set(f"Translating into {target}… The result will be copied automatically.")
        self._update_actions()
        return "break"

    def copy(self):
        """Copy only the still-current, explicitly submitted reply to Windows.

        The OS clipboard is shared with other apps and may retain/history-sync
        this text. Do not read it or clear unrelated clipboard contents on exit.
        """
        if self._closed or not self._valid_result():
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.output.get())
        except tk.TclError:
            self.status.set("Could not copy. Select the translated text and copy it manually.")
        else:
            copied = (f"Copied whisper to {self._recipient} in {self.language.get()}." if self._recipient
                      else f"Copied in {self.language.get()}.")
            self.status.set(copied + " Paste it into the game when you’re ready.")

    def clear_selection(self):
        if self._closed:
            return
        self._invalidate_selection()
        self._record = None
        self._recipient = ""
        self.language.set(CHOOSE_LANGUAGE)
        self.status.set("Click Reply on a message, or choose a reply language manually.")
        self._update_actions()

    def _job_is_current(self, job):
        return (not self._closed and self.connected and job.selection == self._selection
                and job.recipient == self._recipient
                and (job.kind == "detect" or job.revision == self._revision))

    def _execute_job(self, job, clients):
        # No Tk values are read here: the job is an immutable UI-thread snapshot.
        if not self._job_is_current(job):
            return
        try:
            target = "English" if job.kind == "detect" else job.target
            if target not in clients:
                clients[target] = OllamaClient(self.config["ollama_url"], self.config["model"], target)
            if job.kind == "detect":
                options = {"item_links": job.item_links} if job.item_links else {}
                if job.party_details:
                    options["party_details"] = job.party_details
                value = clients[target].detect_language(job.text, **options)
            else:
                # A reply is a new message, not the selected party listing.
                # Its source item's names/ranges/counts must not constrain it.
                value = clients[target].translate(job.text)
            result = (job, value, None)
        except Exception as exc:
            result = (job, None, str(exc))
        if not self._closed:
            self._events.put(result)

    def _worker(self):
        clients = {}
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=REPLY_WORKER_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                self._execute_job(job, clients)
            finally:
                self._jobs.task_done()

    def _drain_events(self):
        """Apply current results on Tk's thread, then copy only a successful reply."""
        self._timer = None
        if self._closed:
            return
        for _ in range(REPLY_EVENTS_PER_POLL):
            try:
                job, value, error = self._events.get_nowait()
            except queue.Empty:
                break
            if not self._job_is_current(job):
                continue
            if job.kind == "detect":
                waiting = self._waiting_reply
                self._waiting_reply = None
                if error:
                    self.language.set(CHOOSE_LANGUAGE)
                    self.status.set("Could not identify the language. Choose it manually. " + error)
                elif value in REPLY_LANGUAGES:
                    self.language.set(value)
                    self.status.set(f"Detected {value}. You can change the language if needed.")
                else:
                    self.language.set(CHOOSE_LANGUAGE)
                    self.status.set("Language is unclear. Choose it manually before translating your reply.")
                if (not error and value in REPLY_LANGUAGES
                        and waiting == (self._selection, self._revision,
                                        self.draft.get().strip(), self._recipient)):
                    self.translate()
            else:
                # Check the current text too, even if an embedding does not use
                # the normal StringVar write trace.
                if job.text != self.draft.get().strip() or job.target != self.language.get():
                    continue
                self._translating = None
                if error or not isinstance(value, str) or not value.strip():
                    self._clear_output()
                    self.status.set("Reply translation failed. " + (error or "The local model returned no text."))
                elif any((ord(char) < 32 and not char.isspace()) or ord(char) == 127 for char in value):
                    # Whitespace is flattened below, but invisible NUL/ESC and
                    # editing controls must never be copied into the game input.
                    self._clear_output()
                    self.status.set("Reply translation failed. The model returned unsupported control characters; nothing was copied.")
                else:
                    translated = " ".join(value.split())
                    self.output.set(f'"{job.recipient} {translated}' if job.recipient else translated)
                    self._result_context = (self._selection, self._revision, job.text,
                                            job.target, job.recipient)
                    self.copy()
            self._update_actions()
        self._timer = self.root.after(REPLY_EVENT_POLL_MS, self._drain_events)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._drop_pending()
        if self._timer is not None:
            try:
                self.root.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
