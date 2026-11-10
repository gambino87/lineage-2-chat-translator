"""An attached, animated channel drawer owned only by the translator overlay."""
from __future__ import annotations

import math
import time
import tkinter as tk


ANIMATION_SECONDS = 0.18
ANIMATION_FRAME_MS = 16
MAX_COLUMNS = 3
CONTENT_PADDING = 8
CHOICE_GAP = 6
BORDER_WIDTH = 1


def tray_geometry(anchor, bounds, wanted_height, progress):
    """Keep the tray above its anchor, overlapping it only at the screen top.

    The chat window never moves. The popup's lower edge is fixed throughout an
    animation, so opening reveals it upward even when its full size is clamped.
    """
    x, anchor_y, width, _height = anchor
    left, top, desktop_width, desktop_height = bounds
    desktop_width, desktop_height = max(1, desktop_width), max(1, desktop_height)
    width = min(max(1, width), desktop_width)
    full_height = min(max(1, wanted_height), desktop_height)
    bottom = max(top + full_height, min(anchor_y, top + desktop_height))
    height = max(1, min(full_height, round(full_height * max(0.0, min(1.0, progress)))))
    return max(left, min(x, left + desktop_width - width)), bottom - height, width, height


def choice_columns(width, choice_width):
    """Use measured checkbox widths so narrow overlays do not clip labels."""
    usable = max(1, width - 2 * (CONTENT_PADDING + BORDER_WIDTH))
    return max(1, min(MAX_COLUMNS, usable // max(1, choice_width + CHOICE_GAP)))


class ChannelTray:
    """A separate clipped popup; its transient animation is never persisted."""

    def __init__(self, owner, *, options, selected, on_change, get_anchor,
                 get_bounds, native_factory, palette, opacity, on_open_change, channel_colors=None):
        self.options = tuple(options)
        self.on_change = on_change
        self.get_anchor = get_anchor
        self.get_bounds = get_bounds
        self.on_open_change = on_open_change
        self.palette = palette
        self.channel_colors = dict(channel_colors or {})
        self._opacity = opacity
        self._open = False
        self._shown = False
        self._destroyed = False
        self._timer = None
        self._generation = 0
        self._progress = 0.0
        self._start_progress = 0.0
        self._started_at = 0.0
        self._duration = ANIMATION_SECONDS
        self._layout_width = None
        self._wanted_height = 1

        self.window = tk.Toplevel(owner)
        self.window.withdraw()
        self.window.title("Overlay channels")
        self.window.overrideredirect(True)
        self.window.configure(bg=palette["border"])
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", opacity)
        # No transient(root): this tray must stay with an overlay whose main
        # launcher may be minimized. It owns no game handle or input hook.
        self._native = native_factory(self.window) if native_factory else None
        self._variables = {}
        self._choices = []
        self._build()
        self.window.bind("<Escape>", self._escape)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.sync(selected, opacity)

    @property
    def is_open(self):
        """The requested state, including while opening or closing animates."""
        return self._open

    def _build(self):
        colors = self.palette
        self.content = tk.Frame(self.window, bg=colors["header"],
                                padx=CONTENT_PADDING, pady=CONTENT_PADDING)
        self.heading = tk.Frame(self.content, bg=colors["header"])
        self.heading.pack(fill="x", pady=(0, 4))
        tk.Label(self.heading, text="Channels", bg=colors["header"], fg=colors["foreground"],
                 font=("Tahoma", 9, "bold")).pack(side="left")
        for text, command in (("×", lambda: self._animate_to(False)),
                              ("None", lambda: self._choose([])),
                              ("All", lambda: self._choose([key for key, _label in self.options]))):
            tk.Button(self.heading, text=text, command=command, bg=colors["header"],
                      fg=colors["foreground"], activebackground=colors["border"],
                      activeforeground=colors["foreground"], relief="flat", borderwidth=0,
                      padx=6, pady=1, font=("Tahoma", 9), cursor="hand2").pack(side="right")
        self.choices = tk.Frame(self.content, bg=colors["header"])
        self.choices.pack(fill="x")
        for key, label in self.options:
            variable = tk.BooleanVar(master=self.window, value=False)
            self._variables[key] = variable
            color = self.channel_colors.get(key, colors["foreground"])
            self._choices.append(tk.Checkbutton(
                self.choices, text=label, variable=variable, command=self._selected,
                bg=colors["header"], fg=color,
                activebackground=colors["header"], activeforeground=color,
                selectcolor=colors["background"], anchor="w", font=("Tahoma", 9),
                highlightcolor=colors["foreground"], cursor="hand2"))

    def _choose(self, selected):
        if self._destroyed:
            return
        for key, variable in self._variables.items():
            variable.set(key in selected)
        self._selected()

    def _selected(self):
        if not self._destroyed:
            self.on_change([key for key, variable in self._variables.items() if variable.get()])

    def sync(self, selected, opacity):
        """Reflect changes from overlay settings without firing user callbacks."""
        if self._destroyed:
            return
        for key, variable in self._variables.items():
            variable.set(key in selected)
        self._opacity = opacity
        self.window.attributes("-alpha", opacity)
        if self._shown and self._native:
            self._native.style(False, opacity)

    def _layout(self, width):
        if width == self._layout_width:
            return
        self.window.update_idletasks()
        choice_width = max((widget.winfo_reqwidth() for widget in self._choices), default=1)
        choice_height = max((widget.winfo_reqheight() for widget in self._choices), default=1)
        columns = choice_columns(width, choice_width)
        for index, widget in enumerate(self._choices):
            widget.grid(row=index // columns, column=index % columns,
                        sticky="w", padx=(0, CHOICE_GAP), pady=1)
        rows = math.ceil(len(self._choices) / columns)
        self._wanted_height = (2 * (CONTENT_PADDING + BORDER_WIDTH) +
                               self.heading.winfo_reqheight() + 4 + rows * (choice_height + 2))
        self._layout_width = width

    def _place(self):
        anchor, bounds = self.get_anchor(), self.get_bounds()
        width = min(anchor[2], max(1, bounds[2]))
        self._layout(width)
        x, y, width, height = tray_geometry(anchor, bounds, self._wanted_height, self._progress)
        self.content.place(x=BORDER_WIDTH, y=BORDER_WIDTH,
                           width=max(1, width - 2 * BORDER_WIDTH),
                           height=max(1, self._wanted_height - 2 * BORDER_WIDTH))
        if self._native:
            # Synchronize Tk's clip size before setting signed monitor positions.
            self.window.geometry(f"{width}x{height}")
            self.window.update_idletasks()
            self._native.place(x, y, width, height)
        else:
            self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def reposition(self):
        if not self._destroyed and self._shown:
            self._place()

    def toggle(self):
        if not self._destroyed:
            self._animate_to(not self._open)

    def _cancel_animation(self):
        self._generation += 1
        if self._timer is not None:
            try:
                self.window.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None

    def _advance(self):
        elapsed = (time.monotonic() - self._started_at) / max(self._duration, 0.001)
        fraction = max(0.0, min(1.0, elapsed))
        eased = 1.0 - (1.0 - fraction) ** 2
        target = 1.0 if self._open else 0.0
        self._progress = self._start_progress + (target - self._start_progress) * eased
        return fraction >= 1.0

    def _animate_to(self, opened):
        if self._destroyed:
            return
        if self._timer is not None:
            self._advance()
        self._cancel_animation()
        changed = self._open != opened
        self._open = opened
        self._start_progress = self._progress
        self._started_at = time.monotonic()
        self._duration = ANIMATION_SECONDS * abs((1.0 if opened else 0.0) - self._progress)
        if opened and not self._shown:
            self._place()
            if self._native:
                # Map without activation, then restore interactive mouse input.
                # The overlay's locked style includes WS_EX_NOACTIVATE.
                self._native.style(True, self._opacity)
            self.window.deiconify()
            if self._native:
                self._native.style(False, self._opacity)
            self._shown = True
        if changed:
            self.on_open_change(opened)
        self._tick(self._generation)

    def _tick(self, generation):
        if self._destroyed or generation != self._generation:
            return
        self._timer = None
        complete = self._advance()
        self._place()
        if complete:
            if not self._open:
                self.window.withdraw()
                self._shown = False
            return
        self._timer = self.window.after(ANIMATION_FRAME_MS, lambda: self._tick(generation))

    def _escape(self, _event=None):
        self._animate_to(False)
        return "break"

    def hide(self):
        """Immediately cancel/withdraw on owner hide, lock or shutdown."""
        if self._destroyed:
            return
        changed = self._open
        self._cancel_animation()
        self._open = self._shown = False
        self._progress = 0.0
        self.window.withdraw()
        if changed:
            self.on_open_change(False)

    def destroy(self):
        if not self._destroyed:
            self.hide()
            self._destroyed = True
            self.window.destroy()
