"""Tk-owned hide/restore state; native callbacks carry data through a queue.

Only a requested hide with a successfully registered notification icon can
withdraw the launcher. A delayed callback cannot hide a restored/closing window.
Translation, overlay and backend ownership are independent of this controller.
"""
from __future__ import annotations

import queue
import time
import tkinter as tk

from app_icon import ICON
from windows_tray import NativeTray
from ui_i18n import tr

TRAY_POLL_INTERVAL_MS = 100
TRAY_SHUTDOWN_SECONDS = 5.0


class TrayControls:
    def __init__(self, root, *, enabled, on_exit, on_error, close_enabled=None, factory=None):
        self.root = root
        self.enabled = enabled
        self.close_enabled = close_enabled or (lambda: False)
        self.on_exit = on_exit
        self.on_error = on_error
        self._factory = factory or NativeTray
        self._events = queue.Queue()
        self._native = None
        self._retired = []
        self._generation = 0
        self._hidden = False
        self._hide_reason = None
        self._native_ready = False
        self._closed = False
        self._poll_timer = None
        self._minimize_timer = None
        self._last_state = "normal"
        # Root bindtags also see child events. Every handler filters its widget.
        root.bind("<Unmap>", self._unmapped, add="+")
        root.bind("<Map>", self._mapped, add="+")
        root.bind("<Configure>", self._configured, add="+")
        root.bind("<Destroy>", self._destroyed, add="+")

    def _configured(self, event):
        if event.widget is self.root and not self._closed:
            state = self.root.state()
            if state in ("normal", "zoomed"):
                self._last_state = state

    def _unmapped(self, event):
        if (event.widget is not self.root or self._closed or not self.enabled()
                or self._hidden or self._minimize_timer is not None):
            return
        # Windows/Tk can deliver Unmap before the state query becomes iconic.
        self._minimize_timer = self.root.after_idle(self._minimize)

    def _mapped(self, event):
        if (event.widget is self.root and not self._closed
                and self.root.state() in ("normal", "zoomed")):
            self._configured(event)
            if self._native is not None:
                self._remove()

    def _destroyed(self, event):
        if event.widget is self.root:
            self.close()

    def _minimize(self):
        self._minimize_timer = None
        if (self._closed or not self.enabled() or self.root.state() != "iconic"
                or self._native is not None):
            return
        self._hide_reason = "minimize"
        self._start()

    def hide_on_close(self):
        """Keep the session alive; hide only after the icon is usable."""
        if self._closed or not self.close_enabled():
            return
        state = self.root.state()
        if state in ("normal", "zoomed"):
            self._last_state = state
        self._cancel_timer("_minimize_timer")
        self._hide_reason = "close"
        if self._native is None:
            self._start()
        elif self._native_ready:
            self._hide_if_requested()

    def _hide_enabled(self):
        if self._hide_reason == "close":
            return self.close_enabled()
        return self._hide_reason == "minimize" and self.enabled()

    def _hide_if_requested(self):
        state = self.root.state()
        if (self._hide_enabled() and not self._hidden
                and (state == "iconic" or (self._hide_reason == "close"
                                          and state in ("normal", "zoomed")))):
            self._hidden = True
            self.root.withdraw()
        elif not self._hidden:
            self._remove()

    def _start(self):
        self._generation += 1
        generation = self._generation
        events = self._events
        # This closure runs on the native thread: it must never call Tk.
        def receive(kind, message=None):
            events.put((generation, kind, message))
        try:
            self._native = self._factory(ICON, receive)
            self._native.start()
        except Exception as exc:
            self._fallback(str(exc))
            return
        self._poll_timer = self.root.after(TRAY_POLL_INTERVAL_MS, self._drain)

    def _drain(self):
        self._poll_timer = None
        if self._closed:
            return
        # Coalesce native click noise; bound work on Tk's event loop.
        for _ in range(100):
            try:
                generation, kind, message = self._events.get_nowait()
            except queue.Empty:
                break
            if generation != self._generation or self._native is None:
                continue
            if kind == "ready":
                self._native_ready = True
                self._hide_if_requested()
            elif kind == "restore":
                self.restore()
            elif kind == "exit":
                self.on_exit()
                # The callback owns shutdown; do not process any later clicks.
                return
            elif kind == "error":
                self._fallback(message or "Windows could not create the tray icon.")
        if self._native is not None and not self._closed:
            self._poll_timer = self.root.after(TRAY_POLL_INTERVAL_MS, self._drain)

    def _cancel_timer(self, name):
        timer = getattr(self, name)
        setattr(self, name, None)
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except tk.TclError:
                pass

    def _remove(self):
        """Invalidate callbacks before requesting native teardown."""
        self._generation += 1
        native, self._native = self._native, None
        self._hidden = False
        self._hide_reason = None
        self._native_ready = False
        self._cancel_timer("_poll_timer")
        # A stopped thread may still report failed resource cleanup. Keep that
        # failure for shutdown/update checks; a zero timeout never blocks Tk.
        self._retired = [item for item in self._retired if not item.wait_closed(0)]
        if native is not None:
            self._retired.append(native)
            native.close()

    def restore(self):
        """Reveal the existing launcher; preserve its session and window state."""
        if self._closed:
            return
        restored_state = self._last_state
        self._cancel_timer("_minimize_timer")
        try:
            self._remove()
        finally:
            # Even a teardown error must leave ordinary window/taskbar access.
            self.root.deiconify()
            if restored_state == "zoomed":
                self.root.state("zoomed")
            self.root.lift()

    def preference_changed(self):
        if not self._closed:
            if self._native is not None and not self._hide_enabled():
                self.restore()
            if not self.enabled():
                self._cancel_timer("_minimize_timer")

    def _fallback(self, message):
        # A native failure must never strand the launcher without an icon.
        try:
            self.restore()
        finally:
            self.on_error(tr("System tray unavailable: {error}", error=tr(message)))

    def close(self):
        """Stop accepting events; native cleanup completes on its owner thread."""
        if self._closed:
            return
        self._closed = True
        self._cancel_timer("_minimize_timer")
        self._remove()

    def is_alive(self):
        return ((self._native is not None and self._native.is_alive())
                or any(item.is_alive() for item in self._retired))

    def wait_closed(self, timeout=TRAY_SHUTDOWN_SECONDS):
        """Wait only off Tk's thread, after close, before update handoff/exit."""
        deadline = time.monotonic() + timeout
        # Visit every retiring instance even if one fails, so independent native
        # windows/icons still get their bounded chance to finish cleanup.
        results = [item.wait_closed(max(0.0, deadline - time.monotonic()))
                   for item in tuple(self._retired)]
        return all(results)
