"""Attached, animated drawers owned only by the translator overlay."""
from __future__ import annotations

import math
import time
import tkinter as tk
from ui_i18n import tr


ANIMATION_SECONDS = 0.22
ANIMATION_FRAME_MS = 1000 / 60
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


class AnimatedDrawer:
    """A separate clipped popup; its transient animation is never persisted."""

    def __init__(self, owner, *, title, get_anchor, get_bounds, native_factory,
                 palette, opacity, on_open_change, on_closed=None):
        self.title = title
        self.get_anchor = get_anchor
        self.get_bounds = get_bounds
        self.on_open_change = on_open_change
        self.on_closed = on_closed
        self.palette = palette
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
        self._anchor = None
        self._bounds = None
        self._last_placement = None
        self._localized_widgets = []

        self.window = tk.Toplevel(owner)
        self.window.withdraw()
        self.window.title(tr(self.title))
        self.window.overrideredirect(True)
        self.window.configure(bg=palette["border"])
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", opacity)
        # No transient(root): this tray must stay with an overlay whose main
        # launcher may be minimized. It owns no game handle or input hook.
        self._native = native_factory(self.window) if native_factory else None
        self._build()
        self.window.bind("<Escape>", self._escape)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

    @property
    def is_open(self):
        """The requested state, including while opening or closing animates."""
        return self._open

    @property
    def is_shown(self):
        """Remain shown until the closing animation has actually withdrawn."""
        return self._shown

    def _localized_widget(self, factory, parent, **options):
        source = options.get("text", "")
        options["text"] = tr(source)
        widget = factory(parent, **options)
        self._localized_widgets.append((widget, source))
        return widget

    def refresh_language(self):
        """Retranslate and remeasure labels without changing selections or animation."""
        if self._destroyed:
            return
        self.window.title(tr(self.title))
        for widget, source in self._localized_widgets:
            widget.configure(text=tr(source))
        self._layout_width = None
        self.reposition()

    def set_opacity(self, opacity):
        if self._destroyed:
            return
        self._opacity = opacity
        self.window.attributes("-alpha", opacity)
        if self._shown and self._native:
            self._native.style(False, opacity)

    def _place(self, *, refresh=False):
        if refresh or self._anchor is None:
            self._anchor, self._bounds = self.get_anchor(), self.get_bounds()
        anchor, bounds = self._anchor, self._bounds
        width = min(anchor[2], max(1, bounds[2]))
        layout_changed = self._layout(width)
        x, y, width, height = tray_geometry(anchor, bounds, self._wanted_height, self._progress)
        if layout_changed:
            self.content.place(x=BORDER_WIDTH, y=BORDER_WIDTH,
                               width=max(1, width - 2 * BORDER_WIDTH),
                               height=max(1, self._wanted_height - 2 * BORDER_WIDTH))
            if self._native:
                # Settle Tk's requested size only when the controls reflow.
                self.window.geometry(f"{width}x{height}")
                self.window.update_idletasks()
            self._last_placement = None
        placement = x, y, width, height
        if placement == self._last_placement:
            return
        if self._native:
            # Mapped native resizes update Tk's recorded size. One atomic move
            # and resize avoids drawing at the old y first, and lets the normal
            # event loop paint instead of flushing every window on every frame.
            self._native.place(x, y, width, height)
        else:
            self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self._last_placement = placement

    def reposition(self):
        if not self._destroyed and self._shown:
            self._place(refresh=True)

    def toggle(self):
        if not self._destroyed:
            self._animate_to(not self._open)

    def open(self):
        if not self._open:
            self._animate_to(True)

    def close(self):
        if self._open:
            self._animate_to(False)

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
        eased = fraction * fraction * (3.0 - 2.0 * fraction)
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
        self._duration = ANIMATION_SECONDS * abs((1.0 if opened else 0.0) - self._progress)
        if opened and not self._shown:
            # A withdrawn HWND does not feed its native size back into Tk.
            # Prepare it again on reopen so mapping cannot flash the old size.
            self._layout_width = None
            self._place(refresh=True)
            if self._native:
                # Map without activation, then restore interactive mouse input.
                # The overlay's locked style includes WS_EX_NOACTIVATE.
                self._native.style(True, self._opacity)
            self.window.deiconify()
            if self._native:
                self._native.style(False, self._opacity)
            # Mapping queues Tk geometry work. Finish it before restoring the
            # signed native position and starting the clock, so a slow first
            # layout cannot consume the opening animation or overwrite a frame.
            self.window.update_idletasks()
            self._last_placement = None
            self._shown = True
        if changed:
            self.on_open_change(opened)
        self._started_at = time.monotonic()
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
                if self.on_closed:
                    self.on_closed()
            return
        # Rendering time belongs inside the frame budget. Skip missed deadlines
        # after a busy event loop instead of adding another full frame or trying
        # to replay frames that can no longer be displayed.
        now = time.monotonic()
        frame_seconds = ANIMATION_FRAME_MS / 1000
        next_frame = math.floor((now - self._started_at) / frame_seconds) + 1
        deadline = min(self._started_at + next_frame * frame_seconds,
                       self._started_at + self._duration)
        delay = max(1, math.ceil((deadline - now) * 1000))
        self._timer = self.window.after(delay, lambda: self._tick(generation))

    def _escape(self, _event=None):
        self.close()
        return "break"

    def hide(self):
        """Immediately cancel/withdraw on owner hide, lock or shutdown."""
        if self._destroyed:
            return
        changed = self._open
        self._cancel_animation()
        self._open = self._shown = False
        self._progress = 0.0
        self._last_placement = None
        self.window.withdraw()
        if changed:
            self.on_open_change(False)

    def destroy(self):
        if not self._destroyed:
            self.hide()
            self._destroyed = True
            self.window.destroy()


class ChannelTray(AnimatedDrawer):
    def __init__(self, owner, *, options, selected, on_change, channel_colors=None, **kwargs):
        self.options = tuple(options)
        self.on_change = on_change
        self.channel_colors = dict(channel_colors or {})
        self._variables = {}
        self._choices = []
        super().__init__(owner, title="Overlay channels", **kwargs)
        self.sync(selected, self._opacity)

    def _build(self):
        colors = self.palette
        self.content = tk.Frame(self.window, bg=colors["header"],
                                padx=CONTENT_PADDING, pady=CONTENT_PADDING)
        self.heading = tk.Frame(self.content, bg=colors["header"])
        self.heading.pack(fill="x", pady=(0, 4))
        self._localized_widget(tk.Label, self.heading, text="Channels", bg=colors["header"], fg=colors["foreground"],
                 font=("Tahoma", 9, "bold")).pack(side="left")
        for text, command in (("×", self.close),
                              ("None", lambda: self._choose([])),
                              ("All", lambda: self._choose([key for key, _label in self.options]))):
            self._localized_widget(tk.Button, self.heading, text=text, command=command, bg=colors["header"],
                      fg=colors["foreground"], activebackground=colors["border"],
                      activeforeground=colors["foreground"], relief="flat", borderwidth=0,
                      padx=6, pady=1, font=("Tahoma", 9), cursor="hand2").pack(side="right")
        self.choices = tk.Frame(self.content, bg=colors["header"])
        self.choices.pack(fill="x")
        for key, label in self.options:
            variable = tk.BooleanVar(master=self.window, value=False)
            self._variables[key] = variable
            color = self.channel_colors.get(key, colors["foreground"])
            self._choices.append(self._localized_widget(tk.Checkbutton,
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
        self.set_opacity(opacity)

    def _layout(self, width):
        if width == self._layout_width:
            return False
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
        return True


class SettingsDrawer(AnimatedDrawer):
    """The overlay's settings, width-fitted and scrollable on short monitors."""

    def __init__(self, owner, *, build_content, layout_content, **kwargs):
        self.build_content = build_content
        self.layout_content = layout_content
        self._layout_height = None
        super().__init__(owner, title="Chat overlay settings", **kwargs)

    def _build(self):
        colors = self.palette
        self.content = tk.Frame(self.window, bg=colors["header"])
        self.scrollbar = tk.Scrollbar(self.content, orient="vertical", width=12,
            bg=colors["header"], troughcolor=colors["background"], bd=0)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(self.content, bg=colors["header"], bd=0,
            highlightthickness=0, yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.configure(command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=colors["header"],
                             padx=CONTENT_PADDING, pady=CONTENT_PADDING)
        self._body_id = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.build_content(self.body)
        # Bind only this owned popup, so scrolling never hooks other windows.
        self.window.bind("<MouseWheel>", self._wheel)
        self.window.bind("<Button-4>", lambda event: self._wheel(event, -1))
        self.window.bind("<Button-5>", lambda event: self._wheel(event, 1))

    def _wheel(self, event, units=None):
        if units is None:
            units = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _layout(self, width):
        available_height = max(1, self._bounds[3])
        if width == self._layout_width and available_height == self._layout_height:
            return False
        body_width = max(1, width - 2 * BORDER_WIDTH - 12)
        self.canvas.itemconfigure(self._body_id, width=body_width)
        self.layout_content(max(1, body_width - 2 * CONTENT_PADDING))
        self.window.update_idletasks()
        body_height = self.body.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, body_width, body_height))
        self._wanted_height = min(body_height + 2 * BORDER_WIDTH, available_height)
        self._layout_width = width
        self._layout_height = available_height
        return True
