"""Settings-only update controls; network and preparation stay off Tk's thread."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import app_updater

CHECK_INTERVAL_SECONDS = 15 * 60
EVENT_POLL_MS = 100
MAX_EVENTS_PER_TICK = 100  # Bound work per Tk callback so typing stays responsive.
MAX_ERROR_MESSAGE_CHARS = 300


class UpdateControls:
    """Settings-only view; workers return data, and only Tk's thread edits widgets."""
    def __init__(self, parent, *, current_version, on_ready,
                 foreground="#edf3f8", muted="#a4b4c3"):
        self.current_version = current_version
        self.on_ready = on_ready
        self.busy = False
        self._closed = False
        self._operation = None
        self._generation = 0
        self._last_success = None
        self._release = None
        self._can_install = False
        self._handed_off = None
        self._events = queue.Queue()
        self._events_lock = threading.Lock()
        self._cancel = threading.Event()
        self._timer = None
        self.cleanup_errors = []
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(1, weight=1)
        ttk.Label(self.frame, text="Updates", foreground=foreground).grid(
            row=0, column=0, sticky="nw", padx=(0, 12), pady=5)
        self.status = tk.StringVar(master=self.frame, value=f"v{current_version} · Not checked yet")
        ttk.Label(self.frame, textvariable=self.status, foreground=muted,
                  wraplength=450, justify="left").grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.button = ttk.Button(self.frame, text="Check for updates", command=self._clicked)
        self.button.grid(row=0, column=2, sticky="ne")
        self._schedule()

    def settings_shown(self):
        """Check when Settings first opens, then at most once per 15 minutes."""
        if self._closed or self.busy:
            return
        if self._last_success is None or time.monotonic() - self._last_success >= CHECK_INTERVAL_SECONDS:
            self.check()

    def check(self):
        """Manual checks are allowed even within the automatic check interval."""
        if self._closed or self.busy:
            return
        self._release = None
        self._can_install = False
        generation = self._begin("check", "Checking…", f"v{self.current_version} · Checking for updates…")

        def work():
            try:
                release = app_updater.check_for_update(self.current_version)
                support = app_updater.installation_support() if release is not None else (False, "")
                self._publish("checked", generation, (release, support))
            except Exception as exc:
                self._publish("error", generation, ("check", str(exc)))

        self._start_worker(work, generation)

    def _clicked(self):
        if self._closed or self.busy:
            return
        if self._release is not None and self._can_install:
            self._prepare()
        else:
            self.check()

    def _prepare(self):
        release = self._release
        if self._closed or self.busy or release is None:
            return
        generation = self._begin("prepare", "Downloading…", f"Downloading v{release.version}…")

        def work():
            last_percent = None

            def progress(downloaded, total):
                nonlocal last_percent
                if self._cancel.is_set():
                    return
                percent = max(0, min(100, int(downloaded * 100 / total))) if total > 0 else None
                if percent != last_percent:
                    last_percent = percent
                    self._publish("progress", generation, percent)

            try:
                supported, reason = app_updater.installation_support()
                if not supported:
                    raise RuntimeError(reason or "Automatic updates require the installed application.")
                prepared = app_updater.prepare_update(release, progress=progress, cancelled=self._cancel)
                self._publish("prepared", generation, prepared)
            except Exception as exc:
                self._publish("error", generation, ("prepare", str(exc)))

        self._start_worker(work, generation)

    def _begin(self, operation, button_text, status):
        self._generation += 1
        self.busy = True
        self._operation = operation
        self.button.configure(text=button_text, state="disabled", style="TButton")
        self.status.set(status)
        return self._generation

    def _start_worker(self, work, generation):
        try:
            threading.Thread(target=work, daemon=True, name="translator-update-" + self._operation).start()
        except Exception as exc:
            self._publish("error", generation, (self._operation, str(exc)))

    def _publish(self, kind, generation, value):
        # Closing and publishing use the same lock: a verified download cannot
        # fall between the final queue drain and cancellation, leaking a job.
        with self._events_lock:
            accepted = not self._closed and generation == self._generation
            if accepted:
                self._events.put((kind, generation, value))
        if not accepted and kind == "prepared":
            self._discard(value)

    def _discard(self, prepared):
        # Ownership moves to the parent before on_ready, which may close this
        # panel synchronously. It alone decides whether to install or discard.
        handed_off = self._handed_off
        if handed_off is not None and (prepared is handed_off or prepared.stage == handed_off.stage):
            return
        try:
            app_updater.discard_update(prepared)
        except Exception as exc:
            self.cleanup_errors.append(str(exc))

    @staticmethod
    def _error_message(value):
        return " ".join(str(value).split())[:MAX_ERROR_MESSAGE_CHARS] or "Please try again."

    def _idle_button(self):
        if self._release is not None and self._can_install:
            self.button.configure(text=f"Install v{self._release.version} & restart",
                                  state="normal", style="Accent.TButton")
        else:
            self.button.configure(text="Check for updates", state="normal", style="TButton")

    def _schedule(self):
        if not self._closed:
            try:
                self._timer = self.frame.after(EVENT_POLL_MS, self._drain_events)
            except tk.TclError:
                self.close()

    def _drain_events(self):
        """Apply current-generation results without reviving canceled operations."""
        self._timer = None
        for _ in range(MAX_EVENTS_PER_TICK):
            try:
                kind, generation, value = self._events.get_nowait()
            except queue.Empty:
                break
            if self._closed or generation != self._generation:
                if kind == "prepared":
                    self._discard(value)
                continue
            if kind == "checked" and self._operation == "check":
                self._release, support = value
                self._can_install, reason = support
                self.busy = False
                self._operation = None
                self._last_success = time.monotonic()
                if self._release is None:
                    self.status.set(f"v{self.current_version} · Up to date")
                elif self._can_install:
                    self.status.set(f"v{self.current_version} · Update v{self._release.version} available. Models and settings will be kept.")
                else:
                    self.status.set(f"v{self.current_version} · Update v{self._release.version} available. "
                                    + self._error_message(reason or "Install the packaged app to use automatic updates."))
                self._idle_button()
            elif kind == "progress" and self._operation == "prepare":
                version = self._release.version
                self.status.set(f"Downloading v{version}…" + (f" {value}%" if value is not None else ""))
            elif kind == "error" and self._operation == value[0]:
                operation, message = value
                self.busy = False
                self._operation = None
                if operation == "check":
                    self._last_success = None
                self.status.set(("Could not check for updates: " if operation == "check" else "Update was not installed: ")
                                + self._error_message(message))
                self._idle_button()
            elif kind == "prepared":
                if self._operation != "prepare":
                    self._discard(value)
                    continue
                self._operation = "handoff"
                self._handed_off = value
                self.status.set("Update verified. Closing the translator to install and restart…")
                self.button.configure(text="Installing…", state="disabled", style="TButton")
                try:
                    self.on_ready(value)
                except Exception as exc:
                    self._handed_off = None
                    self._discard(value)
                    if not self._closed:
                        self.busy = False
                        self._operation = None
                        self.status.set("Update was not installed: " + self._error_message(exc))
                        self._idle_button()
        self._schedule()

    def close(self):
        """Cancel work and discard queued jobs, retaining parent-owned handoff."""
        pending = []
        with self._events_lock:
            self._closed = True
            self._cancel.set()
            self.busy = False
            while True:
                try:
                    kind, _generation, value = self._events.get_nowait()
                except queue.Empty:
                    break
                if kind == "prepared":
                    pending.append(value)
        if self._timer is not None:
            try:
                self.frame.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        for prepared in pending:
            self._discard(prepared)
        return not self.cleanup_errors
