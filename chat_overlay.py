"""A separate desktop chat overlay. Only its own window receives native styles.

No game handles, process memory, input hooks, injected code, or saved chat.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import os
import re
import tkinter as tk
from tkinter import messagebox

from translator_core import LANGUAGE_CODES, MAX_CHAT_CHARACTERS, UNIVERSAL_TRADE_LANGUAGE
from chat_links import LINK_COLORS, insert_linked_text, normalize_display
from reply_composer import whisper_recipient
from overlay_tray import ChannelTray, SettingsDrawer, choice_columns
from ui_i18n import tr


# Protocol IDs, not words in the message: a party advertisement in Say stays Say.
# L2J ChatType reference: https://github.com/rukhavi/l2j-mobius/blob/master/java/com/l2jserver/gameserver/enums/ChatType.java
CHANNEL_FILTERS = (
    ("say", "Say", (0,)), ("party", "Party", (3,)), ("clan", "Clan", (4,)),
    ("alliance", "Alliance", (9,)), ("whisper", "Whisper", (2,)),
    ("shout", "Shout", (1,)), ("trade", "Trade", (8,)),
    ("world", "World / global", (25,)), ("hero", "Hero", (17,)),
    ("command", "Command channel", (15, 16, 21)),
    ("other", "Other / notices", ()),
)
ALL_CHANNEL_FILTERS = tuple(key for key, _label, _ids in CHANNEL_FILTERS)
CHANNEL_FILTER_BY_ID = {channel: key for key, _label, ids in CHANNEL_FILTERS for channel in ids}
DEFAULT_OVERLAY = {
    "enabled": False, "opacity": 0.85, "font_size": 11,
    "width": 540, "height": 230, "x": None, "y": None,
    "locked": False, "show_original": False,
    "channels": list(ALL_CHANNEL_FILTERS),
}
BACKGROUND = "#10100f"
HEADER = "#201f1a"
BORDER = "#625841"
GOLD = "#d4c59a"
MUTED = "#a59e8b"
# Shout orange, Whisper magenta, Party green and channel 25 red match supplied chat references.
CHANNEL_COLORS = {0: "#f0f0ed", 1: "#ff7b0c", 2: "#ff0cff",
                  3: "#0cff0c", 4: "#c2aeef", 8: "#e4b1d6",
                  9: "#b6dd89", 17: "#ffe390", 25: "#f15353"}
# Filter labels use the same channel color/fallback as rendered messages.
CHANNEL_FILTER_COLORS = {key: CHANNEL_COLORS.get(ids[0] if ids else 0, CHANNEL_COLORS[0])
                         for key, _label, ids in CHANNEL_FILTERS}
HISTORY_LIMIT = 250  # Bounded in-memory display only; never persisted as chat.
OPACITY_RANGE = (0.35, 1.0)
FONT_SIZE_RANGE = (8, 22)
WIDTH_RANGE = (300, 3000)
HEIGHT_RANGE = (120, 2000)
COORDINATE_RANGE = (-32000, 32000)
ERROR_DISPLAY_CHARACTERS = 180
SENDER_DISPLAY_CHARACTERS = 100
TIMESTAMP_DISPLAY_CHARACTERS = 16
GEOMETRY_SAVE_DELAY_MS = 250  # Coalesce dragging instead of saving every motion.


def _number(value, default, lower, upper, integer=False):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    result = min(upper, max(lower, result))
    return int(result) if integer else round(result, 2)


def normalize_overlay_settings(value) -> dict:
    """Accept only known preferences and bound them before applying to a window."""
    source = value if isinstance(value, dict) else {}
    result = DEFAULT_OVERLAY.copy()
    for key in ("enabled", "locked", "show_original"):
        result[key] = source.get(key, result[key]) is True
    for key, bounds in (("opacity", OPACITY_RANGE), ("font_size", FONT_SIZE_RANGE),
                        ("width", WIDTH_RANGE), ("height", HEIGHT_RANGE)):
        result[key] = _number(source.get(key), DEFAULT_OVERLAY[key], *bounds, integer=key != "opacity")
    for key in ("x", "y"):
        candidate = source.get(key)
        result[key] = None if candidate is None else _number(candidate, None, *COORDINATE_RANGE, integer=True)
    channels = source.get("channels")
    # Missing/invalid preferences keep the old show-all behavior. An explicit
    # empty list means show nothing. Always return a fresh list for each window.
    if isinstance(channels, list):
        selected = {key for key in channels if isinstance(key, str) and key in ALL_CHANNEL_FILTERS}
        result["channels"] = [key for key in ALL_CHANNEL_FILTERS if key in selected] if selected or not channels else list(ALL_CHANNEL_FILTERS)
    else:
        result["channels"] = list(ALL_CHANNEL_FILTERS)
    return result


def channel_is_visible(channel: int, settings: dict) -> bool:
    """Unknown/server-specific IDs belong to Other instead of silently vanishing."""
    return CHANNEL_FILTER_BY_ID.get(channel, "other") in settings.get("channels", ALL_CHANNEL_FILTERS)


def clamp_placement(settings: dict, bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Return x,y,width,height wholly within the supplied virtual desktop bounds."""
    prefs = normalize_overlay_settings(settings)
    left, top, desktop_width, desktop_height = bounds
    desktop_width, desktop_height = max(1, desktop_width), max(1, desktop_height)
    width, height = min(prefs["width"], desktop_width), min(prefs["height"], desktop_height)
    x = left + 32 if prefs["x"] is None else prefs["x"]
    y = top + desktop_height - height - 70 if prefs["y"] is None else prefs["y"]
    return (max(left, min(x, left + desktop_width - width)),
            max(top, min(y, top + desktop_height - height)), width, height)


def resize_from_top_right(rect, delta_x, delta_y, bounds) -> tuple[int, int, int, int]:
    """Resize against a fixed bottom-left corner, including at desktop limits."""
    x, y, width, height = rect
    left, top, desktop_width, desktop_height = bounds
    right, bottom_limit = left + max(1, desktop_width), top + max(1, desktop_height)
    x = max(left, min(x, right - 1))
    bottom = max(top + 1, min(y + height, bottom_limit))
    max_width, max_height = min(WIDTH_RANGE[1], right - x), min(HEIGHT_RANGE[1], bottom - top)
    width = max(min(WIDTH_RANGE[0], max_width), min(width + delta_x, max_width))
    height = max(min(HEIGHT_RANGE[0], max_height), min(height - delta_y, max_height))
    return x, bottom - height, width, height


def _one_line(value, limit=MAX_CHAT_CHARACTERS) -> str:
    """Prevent embedded newlines from impersonating additional overlay rows."""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


@dataclass(frozen=True)
class OverlayMessage:
    timestamp: str
    sender: str
    original: str
    translated: str
    channel: int
    error: bool = False
    record: object | None = field(default=None, compare=False, repr=False)


def make_overlay_message(record, translated, source="Live", error=False) -> OverlayMessage:
    """Build a bounded display row without changing its original reply target."""
    sender = _one_line(record.sender, SENDER_DISPLAY_CHARACTERS) or ("Sample" if source == "Sample" else "Notice")
    return OverlayMessage(_one_line(record.timestamp, TIMESTAMP_DISPLAY_CHARACTERS), sender,
                          normalize_display(record.message, spans=getattr(record, "link_spans", ()), limit=MAX_CHAT_CHARACTERS),
                          normalize_display(translated, spans=() if error else None,
                                            limit=ERROR_DISPLAY_CHARACTERS if error else MAX_CHAT_CHARACTERS),
                          int(record.channel), bool(error), record)


def message_parts(message: OverlayMessage, settings: dict) -> tuple[str, str, str, str, str]:
    """Return the fixed compact row, keeping its clickable language separate."""
    timestamp = f"[{message.timestamp}]"
    source_language = getattr(message.record, "source_language", None)
    language = "[--]" if source_language == UNIVERSAL_TRADE_LANGUAGE else f"[{LANGUAGE_CODES.get(source_language, '??')}]"
    sender = tr(message.sender) if message.record is not None and not str(getattr(message.record, "sender", message.sender)).strip() else message.sender
    name = f"[{sender}]: "
    text = tr("Translation unavailable: ") + str(message.translated) if message.error else message.translated
    original = message.original if settings.get("show_original") and message.original != message.translated else ""
    return timestamp, language, name, text, original


class OverlayHistory:
    def __init__(self, limit=HISTORY_LIMIT):
        if limit < 1:
            raise ValueError("History limit must be positive")
        self.messages: deque[OverlayMessage] = deque(maxlen=limit)

    def append(self, message):
        dropped = len(self.messages) == self.messages.maxlen
        self.messages.append(message)
        return dropped

    def clear(self):
        self.messages.clear()


WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
# Win32 constants describe operations only on our process-owned overlay HWND.
GA_ROOT = 2
GWL_EXSTYLE = -20
LWA_ALPHA = 0x2
ALPHA_OPAQUE = 255
HWND_TOPMOST = -1
SWP_NOSIZE = 0x1
SWP_NOMOVE = 0x2
SWP_NOACTIVATE = 0x10
SWP_FRAMECHANGED = 0x20
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
MONITOR_DEFAULTTONEAREST = 2


def overlay_extended_style(current: int, locked: bool) -> int:
    """Change only the overlay's input flags; keep unrelated Windows styles."""
    style = (current | WS_EX_LAYERED) & ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    return style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE if locked else style


class _WindowsOverlay:
    """Native operations are restricted to the HWND belonging to this Tk window."""
    def __init__(self, window):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.window = window
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.get_style = getattr(self.user32, "GetWindowLongPtrW", None) or self.user32.GetWindowLongW
        self.set_style = getattr(self.user32, "SetWindowLongPtrW", None) or self.user32.SetWindowLongW
        self.get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_style.restype = ctypes.c_ssize_t
        self.set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_style.restype = ctypes.c_ssize_t
        self.user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
        self.user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        self._monitor_info = MonitorInfo
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.wintypes = wintypes

    def handle(self):
        """Validate ownership each time so native styles cannot target the game."""
        hwnd = self.user32.GetAncestor(self.window.winfo_id(), GA_ROOT)
        owner = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(owner))
        if not hwnd or owner.value != os.getpid():
            raise OSError("Overlay window does not belong to this process")
        return hwnd

    def bounds(self):
        return tuple(self.user32.GetSystemMetrics(index) for index in
                     (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN))

    def work_area(self):
        """Keep the tray on the overlay's monitor, away from gaps and taskbars."""
        monitor = self.user32.MonitorFromWindow(self.handle(), MONITOR_DEFAULTTONEAREST)
        info = self._monitor_info()
        info.cbSize = self.ctypes.sizeof(info)
        if not monitor or not self.user32.GetMonitorInfoW(monitor, self.ctypes.byref(info)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        rect = info.rcWork
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def style(self, locked, opacity):
        hwnd = self.handle()
        current = self.get_style(hwnd, GWL_EXSTYLE)
        self.ctypes.set_last_error(0)
        previous = self.set_style(hwnd, GWL_EXSTYLE, overlay_extended_style(current, locked))
        if previous == 0 and self.ctypes.get_last_error():
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if not self.user32.SetLayeredWindowAttributes(hwnd, 0, round(opacity * ALPHA_OPAQUE), LWA_ALPHA):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        # Topmost, no move/resize/activation; refresh the changed window styles.
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED
        if not self.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def place(self, x, y, width, height):
        if not self.user32.SetWindowPos(self.handle(), HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def rect(self):
        rect = self.wintypes.RECT()
        if not self.user32.GetWindowRect(self.handle(), self.ctypes.byref(rect)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


class ChatOverlay:
    def __init__(self, root, settings=None, on_change=None, on_reply=None, *, model=""):
        self.root = root
        self.model = model
        self.settings = normalize_overlay_settings(settings)
        self.on_change = on_change
        self.on_reply = on_reply
        self._reply_records = {}
        self.history = OverlayHistory()
        self._marks = deque()
        self._sequence = 0
        self._unseen = 0
        self._destroyed = False
        self._geometry_timer = None
        self._layout_timer = None
        self._settings_window = None
        self._settings_drawer = None
        self._pending_drawer = None
        self._vars = {}
        self._channel_vars = {}
        self._tray = None
        self._drag_origin = None
        self._resize_origin = None
        self._localized_widgets = []
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.title(tr("Lineage 2 Chat Translator — Overlay"))
        # No transient() here: the overlay must remain when the controls minimize.
        self.window.overrideredirect(True)
        self.window.configure(bg=BORDER)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", self.settings["opacity"])
        self._native = _WindowsOverlay(self.window) if os.name == "nt" else None
        if self._native is None:
            self.settings["locked"] = False
        self._build()
        self.window.update_idletasks()
        self._place_from_settings()
        self._apply_appearance()
        self.window.bind("<Configure>", self._configured)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        if self.settings["enabled"]:
            self.show()

    def _build(self):
        border = tk.Frame(self.window, bg=BACKGROUND)
        border.pack(fill="both", expand=True, padx=1, pady=1)
        self.header = tk.Frame(border, bg=HEADER, height=23)
        self.header.pack(fill="x")
        # Reserve controls first so a long model name cannot hide them.
        self.grip = self._label(self.header, text="◥", bg=HEADER, fg=BORDER,
                             cursor="size_ne_sw", font=("Tahoma", 10), padx=5, pady=2)
        self.grip.pack(side="right")
        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonRelease-1>", self._end_geometry)
        self._header_button("×", self.hide).pack(side="right")
        self.settings_button = self._header_button("Settings", self._toggle_settings_drawer)
        self.settings_button.pack(side="right")
        self._settings_open_changed(False)
        self.channels_button = self._header_button("Channels", self._toggle_channel_tray)
        self.channels_button.pack(side="right")
        self._tray_open_changed(False)
        self.lock_button = self._header_button("Lock", lambda: self._change("locked", True))
        self.lock_button.pack(side="right")
        titles = tk.Frame(self.header, bg=HEADER)
        titles.pack(side="left", fill="x", expand=True)
        self.caption = self._label(titles, text="Translations", bg=HEADER, fg=GOLD,
                                font=("Tahoma", 9, "bold"), padx=8, pady=4, anchor="w")
        self.caption.pack(side="left")
        self.model_label = tk.Label(titles, text=f"· {self.model}" if self.model else "",
                                    bg=HEADER, fg=MUTED, font=("Tahoma", 9), anchor="w")
        self.model_label.pack(side="left", fill="x", expand=True)
        for widget in (self.header, titles, self.caption, self.model_label):
            widget.configure(cursor="fleur")
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_geometry)
        body = tk.Frame(border, bg=BACKGROUND)
        body.pack(fill="both", expand=True)
        self.viewport = tk.Frame(body, bg=BACKGROUND)
        self.text = tk.Text(self.viewport, bg=BACKGROUND, fg=CHANNEL_COLORS[0], borderwidth=0,
                            relief="flat", highlightthickness=0, wrap="word", height=4,
                            padx=6, pady=3, state="disabled", cursor="arrow",
                            selectbackground="#3f4b51", spacing1=0, spacing3=2)
        # A short transcript hugs the lower edge; once full, it uses the whole
        # viewport and scrolls normally. No blank lines are added to chat.
        self.text.place(x=0, rely=1.0, anchor="sw", relwidth=1.0, height=1)
        self.scrollbar = tk.Scrollbar(body, command=self._scroll, width=10, bd=0,
                                      bg=HEADER, troughcolor=BACKGROUND, activebackground=BORDER)
        self.scrollbar.pack(side="right", fill="y")
        self.viewport.pack(side="left", fill="both", expand=True)
        self.viewport.bind("<Configure>", self._schedule_layout)
        self.text.configure(yscrollcommand=self._view_changed)
        if self.on_reply:
            # One binding serves every row. Individual tags contain only a
            # bounded record lookup, not a new Tcl/Python callback per message.
            self.text.tag_bind("reply", "<Button-1>", self._reply_clicked)
            self.text.tag_bind("reply", "<Enter>", self._reply_enter)
            self.text.tag_bind("reply", "<Leave>", self._reply_leave)
            self.text.tag_bind("whisper", "<Button-1>", self._whisper_clicked)
            self.text.tag_bind("whisper", "<Enter>", self._reply_enter)
            self.text.tag_bind("whisper", "<Leave>", self._reply_leave)
        self.bottom = tk.Frame(border, bg=BACKGROUND, height=15)
        self.bottom.pack(fill="x")
        self.latest = self._button(self.bottom, text="", command=self._latest, bg=BACKGROUND,
                                fg=GOLD, activebackground=HEADER, activeforeground=GOLD,
                                borderwidth=0, highlightthickness=0, font=("Tahoma", 8),
                                padx=5, pady=0, cursor="hand2", takefocus=False)
        self.latest.pack(side="left")

    def _header_button(self, text, command):
        return self._button(self.header, text=text, command=command, bg=HEADER, fg=MUTED,
                         activebackground="#343024", activeforeground=GOLD,
                         relief="flat", bd=0, highlightthickness=0, padx=6, pady=2,
                         font=("Tahoma", 8), cursor="hand2", takefocus=False)

    def _localized_widget(self, factory, parent, **options):
        """Retain English sources so repeated language changes never chain translations."""
        source = options.get("text", "")
        options["text"] = tr(source)
        widget = factory(parent, **options)
        if not hasattr(self, "_localized_widgets"):
            self._localized_widgets = []
        self._localized_widgets.append((widget, source))
        return widget

    def _label(self, parent, **options):
        return self._localized_widget(tk.Label, parent, **options)

    def _button(self, parent, **options):
        return self._localized_widget(tk.Button, parent, **options)

    def _checkbutton(self, parent, **options):
        return self._localized_widget(tk.Checkbutton, parent, **options)

    def refresh_language(self):
        """Refresh the live overlay without changing chat, placement, or user selection."""
        if self._destroyed:
            return
        self.window.title(tr("Lineage 2 Chat Translator — Overlay"))
        for widget, source in self._localized_widgets:
            widget.configure(text=tr(source))
        self._update_caption()
        self._tray_open_changed(self._tray.is_open if self._tray is not None else False)
        self._settings_open_changed(self._settings_drawer.is_open if self._settings_drawer is not None else False)
        if self._settings_window is not None:
            self._settings_window.title(tr("Chat overlay settings"))
        if self._tray is not None:
            self._tray.refresh_language()
        if self._settings_drawer is not None:
            self._settings_drawer.refresh_language()
        self._render_all()
        self.latest.configure(text=tr("{count} new · Latest ↓", count=self._unseen) if self._unseen else "")

    def _toggle_channel_tray(self):
        if self._destroyed or self.settings["locked"]:
            return
        self._request_drawer("channels", toggle=True)

    def _toggle_settings_drawer(self):
        self._request_drawer("settings", toggle=True)

    def _drawer_options(self, on_open_change):
        return dict(get_anchor=self._rect, get_bounds=self._tray_bounds,
                    native_factory=_WindowsOverlay if self._native else None,
                    palette={"background": BACKGROUND, "header": HEADER, "border": BORDER,
                             "foreground": GOLD, "muted": MUTED},
                    opacity=self.settings["opacity"], on_open_change=on_open_change,
                    on_closed=self._drawer_closed)

    def _request_drawer(self, name, *, toggle=False):
        """Wait for the old popup to withdraw before mapping the requested one."""
        if self._destroyed or (name == "channels" and self.settings["locked"]):
            return
        if self._pending_drawer == name:
            if toggle:
                self._pending_drawer = None
            return
        self._pending_drawer = None
        drawer = self._tray if name == "channels" else self._settings_drawer
        if drawer is not None and drawer.is_open:
            if toggle:
                drawer.close()
            return
        other = self._settings_drawer if name == "channels" else self._tray
        if other is not None and other.is_shown:
            self._pending_drawer = name
            other.close()
            return
        if drawer is None and name == "channels":
            self._tray = ChannelTray(
                self.window, options=[(key, label) for key, label, _ids in CHANNEL_FILTERS],
                selected=self.settings["channels"], on_change=lambda selected: self._change("channels", selected),
                channel_colors=CHANNEL_FILTER_COLORS,
                **self._drawer_options(self._tray_open_changed))
            drawer = self._tray
        elif drawer is None:
            self._settings_text_start = len(self._localized_widgets)
            self._settings_drawer = SettingsDrawer(
                self.window, build_content=self._build_settings_content,
                layout_content=self._layout_settings_content,
                **self._drawer_options(self._settings_open_changed))
            drawer = self._settings_drawer
            self._settings_window = drawer.window
        self._sync_vars()
        drawer.open()

    def _drawer_closed(self):
        name = self._pending_drawer
        self._pending_drawer = None
        if name is not None:
            self._request_drawer(name)

    def _tray_open_changed(self, opened):
        if not self._destroyed:
            self.channels_button.configure(text=tr("Channels") + (" ▾" if opened else " ▴"))

    def _settings_open_changed(self, opened):
        if not self._destroyed and getattr(self, "settings_button", None) is not None:
            self.settings_button.configure(text=tr("Settings") + (" ▾" if opened else " ▴"))

    def _reposition_tray(self):
        if getattr(self, "_tray", None) is not None:
            self._tray.reposition()
        if getattr(self, "_settings_drawer", None) is not None:
            self._settings_drawer.reposition()

    def _hide_drawers(self):
        self._pending_drawer = None
        for drawer in (getattr(self, "_tray", None), getattr(self, "_settings_drawer", None)):
            if drawer is not None:
                drawer.hide()

    def _bounds(self):
        if self._native:
            return self._native.bounds()
        return self.window.winfo_vrootx(), self.window.winfo_vrooty(), self.window.winfo_screenwidth(), self.window.winfo_screenheight()

    def _tray_bounds(self):
        if self._native:
            try:
                return self._native.work_area()
            except OSError:
                pass  # A monitor may disappear while the overlay is moved.
        return self._bounds()

    def _place(self, x, y, width, height):
        if self._native:
            # Keep Tk's requested size in sync, then use signed desktop
            # coordinates directly (negative Tk offsets mean right/bottom).
            self.window.geometry(f"{width}x{height}")
            self.window.update_idletasks()
            self._native.place(x, y, width, height)
        else:
            self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self._reposition_tray()

    def _place_from_settings(self):
        prefs = dict(self.settings)
        # Reset onto the primary monitor, not the bottom-left corner of a
        # possibly L-shaped virtual desktop where no monitor may exist.
        if prefs["x"] is None:
            prefs["x"] = 32
        if prefs["y"] is None:
            prefs["y"] = max(0, self.root.winfo_screenheight() - prefs["height"] - 70)
        x, y, width, height = clamp_placement(prefs, self._bounds())
        self.settings.update(x=x, y=y, width=width, height=height)
        self._place(x, y, width, height)

    def _rect(self):
        if self._native:
            return self._native.rect()
        return self.window.winfo_x(), self.window.winfo_y(), self.window.winfo_width(), self.window.winfo_height()

    def _emit(self):
        if self.on_change and not self._destroyed:
            self.on_change(dict(self.settings))

    def _apply_appearance(self):
        size = self.settings["font_size"]
        self.text.configure(font=("Tahoma", size))
        for channel, color in CHANNEL_COLORS.items():
            self.text.tag_configure(f"channel{channel}", foreground=color)
        self.text.tag_configure("normal", foreground=CHANNEL_COLORS[0])
        self.text.tag_configure("sender", font=("Tahoma", size, "bold"))
        self.text.tag_configure("meta", foreground=MUTED, font=("Tahoma", max(8, size - 1)))
        self.text.tag_configure("original", foreground="#898b86", font=("Tahoma", max(8, size - 1)))
        self.text.tag_configure("error", foreground="#efb19b")
        self.text.tag_configure("reply", foreground=GOLD, underline=True,
                                font=("Tahoma", max(8, size - 1)))
        for tag, color in LINK_COLORS.items():
            self.text.tag_configure(tag, foreground=color)
        self.window.attributes("-alpha", self.settings["opacity"])
        self.window.attributes("-topmost", True)
        if self._native:
            self._native.style(self.settings["locked"], self.settings["opacity"])
        if self.settings["locked"]:
            if getattr(self, "_pending_drawer", None) == "channels":
                self._pending_drawer = None
            if getattr(self, "_tray", None) is not None:
                self._tray.hide()
                self._drawer_closed()
        self._update_caption()
        self._render_all()

    def _update_caption(self):
        caption = tr("Translations")
        if not self.settings["channels"]:
            caption += " · " + tr("no channels")
        elif len(self.settings["channels"]) < len(ALL_CHANNEL_FILTERS):
            caption += " · " + tr("filtered")
        if self.settings["locked"]:
            caption += " · " + tr("locked")
        self.caption.configure(text=caption)
        self.lock_button.configure(text=tr("Locked" if self.settings["locked"] else "Lock"))

    def _change(self, key, value):
        previous = dict(self.settings)
        self.settings = normalize_overlay_settings(dict(self.settings, **{key: value}))
        if key == "locked" and self.settings["locked"] and self._native is None:
            self.settings["locked"] = False
        try:
            self._apply_appearance()
        except (OSError, tk.TclError) as exc:
            self.settings = previous
            self.settings["locked"] = False
            try:
                self._apply_appearance()
            except (OSError, tk.TclError):
                pass
            messagebox.showerror(tr("Overlay setting"), tr("Could not apply this setting: {error}", error=exc), parent=self._settings_window or self.root)
        self._sync_vars()
        if key == "channels":
            # A changed selection starts at its newest visible row; old unread
            # counts belong to the previous selection and must not carry over.
            self._latest()
        self._emit()

    def _view_changed(self, first, last):
        self.scrollbar.set(first, last)
        if float(last) >= 0.995:
            self._unseen = 0
            self.latest.configure(text="")

    def _scroll(self, *args):
        self.text.yview(*args)

    def _schedule_layout(self, _event=None):
        if not self._destroyed and self._layout_timer is None:
            self._layout_timer = self.root.after_idle(self._align_bottom)

    def _align_bottom(self):
        self._layout_timer = None
        if self._destroyed:
            return
        follow = self.text.yview()[1] >= 0.995
        # Force current wrap/font metrics, including optional original lines.
        pixels = self.text.count("1.0", "end", "update", "ypixels")
        if isinstance(pixels, (tuple, list)):
            pixels = pixels[0]
        content_height = int(pixels or 0) + 2 * int(self.text.cget("pady"))
        height = min(max(1, content_height), max(1, self.viewport.winfo_height()))
        if height != self.text.winfo_height():
            self.text.place_configure(height=height)
            # Let placement settle before following the final line. This also
            # keeps a resize from hiding the newest message below the viewport.
            if follow:
                self._layout_timer = self.root.after_idle(self._finish_bottom_layout)

    def _finish_bottom_layout(self):
        self._layout_timer = None
        if not self._destroyed:
            self._latest()

    def _latest(self):
        self.text.see("end")
        self._unseen = 0
        self.latest.configure(text="")

    def _reply_enter(self, _event=None):
        if not self._destroyed and not self.settings["locked"]:
            self.text.configure(cursor="hand2")

    def _reply_leave(self, _event=None):
        if not self._destroyed:
            self.text.configure(cursor="arrow")

    def _whisper_clicked(self, event):
        return self._reply_clicked(event, whisper=True)

    def _reply_clicked(self, event, *, whisper=False):
        if self._destroyed or self.settings["locked"] or not self.on_reply:
            return "break"
        for tag in self.text.tag_names(f"@{event.x},{event.y}"):
            record = self._reply_records.get(tag)
            if record is not None:
                if whisper:
                    if whisper_recipient(record.sender):
                        self.on_reply(record, whisper=True)
                else:
                    self.on_reply(record)
                break
        return "break"

    def _forget_message(self, mark):
        tag = mark + "_reply"
        if tag in self._reply_records:
            del self._reply_records[tag]
            self.text.tag_delete(tag)
        self.text.mark_unset(mark)

    def _write_message(self, message):
        if self._marks:
            self.text.insert("end", "\n")
        self._sequence += 1
        mark = f"overlay_message_{self._sequence}"
        self.text.mark_set(mark, "end-1c")
        self.text.mark_gravity(mark, "left")
        self._marks.append(mark)
        timestamp, language, name, translated, original = message_parts(message, self.settings)
        color_tag = "error" if message.error else (f"channel{message.channel}" if message.channel in CHANNEL_COLORS else "normal")
        self.text.insert("end", timestamp, "meta")
        if self.on_reply and message.record is not None:
            tag = mark + "_reply"
            self._reply_records[tag] = message.record
            self.text.insert("end", language, ("reply", tag))
        else:
            self.text.insert("end", language, "meta")
        name_tags = (color_tag, "sender")
        if self.on_reply and message.record is not None and whisper_recipient(message.record.sender):
            name_tags += ("whisper", tag)
        self.text.insert("end", name, name_tags)
        insert_linked_text(self.text, translated, color_tag)
        if original:
            self.text.insert("end", "\n  ", "original")
            insert_linked_text(self.text, original, "original")

    def add_message(self, record, translated, source="Live", error=False):
        if self._destroyed:
            return
        message = make_overlay_message(record, translated, source, error)
        evicted = self.history.messages[0] if len(self.history.messages) == self.history.messages.maxlen else None
        visible = channel_is_visible(message.channel, self.settings)
        remove_visible = evicted is not None and channel_is_visible(evicted.channel, self.settings)
        follow = not self.history.messages or self.text.yview()[1] >= 0.995
        self.history.append(message)
        if not visible and not remove_visible:
            return
        self.text.mark_set("overlay_view", self.text.index("@0,0"))
        self.text.configure(state="normal")
        if visible:
            self._write_message(message)
        # History includes hidden messages; marks and reply tags include only
        # visible rows. Evict a displayed row only when that row left history.
        if remove_visible:
            removed = self._marks.popleft()
            self.text.delete("1.0", self._marks[0] if self._marks else "end")
            self._forget_message(removed)
        self.text.configure(state="disabled")
        if follow:
            self._latest()
        else:
            self.text.yview("overlay_view")
            if visible:
                self._unseen += 1
                self.latest.configure(text=tr("{count} new · Latest ↓", count=self._unseen))
        self._schedule_layout()

    def _render_all(self):
        follow = not self.history.messages or self.text.yview()[1] >= 0.995
        position = self.text.yview()[0]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for mark in self._marks:
            self._forget_message(mark)
        self._marks.clear()
        for message in self.history.messages:
            if channel_is_visible(message.channel, self.settings):
                self._write_message(message)
        self.text.configure(state="disabled")
        if follow:
            self._latest()
        else:
            self.text.yview_moveto(position)
        self._schedule_layout()

    def clear(self):
        self.history.clear()
        self._render_all()
        self._unseen = 0
        self.latest.configure(text="")

    def show(self):
        if self._destroyed:
            return
        self.settings["enabled"] = True
        self.window.deiconify()
        self._place_from_settings()
        self.window.attributes("-topmost", True)
        if self._native:
            self._native.style(self.settings["locked"], self.settings["opacity"])
        self._sync_vars()
        self._emit()

    def hide(self):
        if self._destroyed:
            return
        self._hide_drawers()
        self._remember_geometry()
        self.settings["enabled"] = False
        self.window.withdraw()
        self._sync_vars()
        self._emit()

    def toggle(self):
        self.hide() if self.settings["enabled"] else self.show()

    def _start_drag(self, event):
        if not self.settings["locked"]:
            x, y, width, height = self._rect()
            self._drag_origin = event.x_root, event.y_root, x, y, width, height

    def _drag(self, event):
        if self._drag_origin and not self.settings["locked"]:
            start_x, start_y, x, y, width, height = self._drag_origin
            prefs = dict(self.settings, x=x+event.x_root-start_x, y=y+event.y_root-start_y,
                         width=width, height=height)
            self._place(*clamp_placement(prefs, self._bounds()))

    def _start_resize(self, event):
        if not self.settings["locked"]:
            self._resize_origin = event.x_root, event.y_root, *self._rect()

    def _resize(self, event):
        if self._resize_origin and not self.settings["locked"]:
            start_x, start_y, x, y, width, height = self._resize_origin
            self._place(*resize_from_top_right((x, y, width, height),
                        event.x_root - start_x, event.y_root - start_y, self._bounds()))

    def _end_geometry(self, _event=None):
        self._drag_origin = self._resize_origin = None
        self._remember_geometry()

    def _configured(self, event):
        if event.widget is not self.window or self._destroyed:
            return
        self._reposition_tray()
        if self._geometry_timer:
            self.root.after_cancel(self._geometry_timer)
        self._geometry_timer = self.root.after(GEOMETRY_SAVE_DELAY_MS, self._remember_geometry)

    def _remember_geometry(self):
        if self._geometry_timer:
            try:
                self.root.after_cancel(self._geometry_timer)
            except tk.TclError:
                pass
        self._geometry_timer = None
        if self._destroyed or self.window.state() == "withdrawn":
            return
        x, y, width, height = self._rect()
        self.settings.update(x=x, y=y, width=width, height=height)
        self._emit()

    def _reset_placement(self):
        self.settings.update(x=None, y=None, width=540, height=230, locked=False)
        self._place_from_settings()
        self._apply_appearance()
        self.show()
        self._sync_vars()

    def _sync_vars(self):
        for key, variable in self._vars.items():
            variable.set(round(self.settings[key] * 100) if key == "opacity" else self.settings[key])
        for key, variable in self._channel_vars.items():
            variable.set(key in self.settings["channels"])
        if getattr(self, "_tray", None) is not None:
            self._tray.sync(self.settings["channels"], self.settings["opacity"])
        if getattr(self, "_settings_drawer", None) is not None:
            self._settings_drawer.set_opacity(self.settings["opacity"])

    def _channels_changed(self):
        self._change("channels", [key for key, variable in self._channel_vars.items() if variable.get()])

    def open_settings(self):
        # Launcher access must also work with a hidden or click-through overlay.
        self._request_drawer("settings")

    def _build_settings_content(self, content):
        self._settings_content = content
        heading = tk.Frame(content, bg=HEADER)
        heading.pack(fill="x", pady=(0, 10))
        self._button(heading, text="×", command=self._close_settings, bg=HEADER, fg=GOLD,
                     relief="flat", bd=0, cursor="hand2").pack(side="right")
        self._label(heading, text="Settings", bg=HEADER, fg=GOLD,
                 font=("Tahoma", 10, "bold")).pack(side="left")
        self._label(content, text="Drag the header to move. Drag the top-right corner to resize.",
                 bg=HEADER, fg="#dedbcf", font=("Tahoma", 9), wraplength=550, justify="left").pack(anchor="w", pady=(0, 12))
        self._vars = {}
        def checkbox(key, label, command=None):
            variable = tk.BooleanVar(value=self.settings[key])
            self._vars[key] = variable
            widget = self._checkbutton(content, text=label, variable=variable, bg=HEADER,
                fg="#dedbcf", activebackground=HEADER, activeforeground=GOLD,
                selectcolor=BACKGROUND, anchor="w", font=("Tahoma", 10),
                command=command or (lambda: self._change(key, variable.get())))
            widget.pack(anchor="w", fill="x", pady=3)
            return widget
        checkbox("enabled", "Show overlay", lambda: self.show() if self._vars["enabled"].get() else self.hide())
        lock = checkbox("locked", "Lock placement and pass mouse clicks through")
        if self._native is None:
            lock.configure(state="disabled")
        self._label(content, text="To unlock, return to the main translator → Overlay settings.",
                 bg=HEADER, fg=MUTED, font=("Tahoma", 9), wraplength=526, justify="left").pack(anchor="w", padx=(24, 0), pady=(0, 10))
        for key, label, lower, upper in (("opacity", "Opacity", *(round(value * 100) for value in OPACITY_RANGE)),
                                        ("font_size", "Text size", *FONT_SIZE_RANGE)):
            row = tk.Frame(content, bg=HEADER)
            row.pack(fill="x", pady=4)
            self._label(row, text=label, anchor="w", bg=HEADER, fg="#dedbcf", font=("Tahoma", 10)).pack(anchor="w")
            variable = tk.IntVar(value=round(self.settings[key] * 100) if key == "opacity" else self.settings[key])
            self._vars[key] = variable
            scale = tk.Scale(row, from_=lower, to=upper, orient="horizontal", variable=variable,
                             bg=HEADER, fg=GOLD, troughcolor=BACKGROUND, highlightthickness=0,
                             activebackground=BORDER, length=120, sliderlength=18,
                             command=lambda value, option=key: self._change(option, float(value)/100 if option == "opacity" else int(value)))
            scale.pack(fill="x")
        checkbox("show_original", "Show original text below translations")
        channels = tk.Frame(content, bg=HEADER)
        channels.pack(fill="x", pady=(12, 0))
        heading = tk.Frame(channels, bg=HEADER)
        heading.pack(fill="x", pady=(0, 4))
        self._settings_channel_actions = []
        for label, selection in (("None", []), ("All", list(ALL_CHANNEL_FILTERS))):
            button = self._button(heading, text=label, command=lambda chosen=selection: self._change("channels", chosen),
                                  bg=BORDER, fg="#fff8e5", relief="flat", padx=8, pady=2)
            button.pack(side="right", padx=(5, 0))
            self._settings_channel_actions.append(button)
        self._settings_channel_heading = self._label(heading, text="Channels shown in overlay", bg=HEADER, fg=GOLD,
                                                    font=("Tahoma", 10, "bold"))
        self._settings_channel_heading.pack(side="left", fill="x", expand=True)
        choices = tk.Frame(channels, bg=HEADER)
        choices.pack(fill="x")
        self._channel_vars = {}
        self._settings_choices = []
        for key, label, _ids in CHANNEL_FILTERS:
            variable = tk.BooleanVar(value=key in self.settings["channels"])
            self._channel_vars[key] = variable
            color = CHANNEL_FILTER_COLORS[key]
            widget = self._checkbutton(choices, text=label, variable=variable, command=self._channels_changed,
                           bg=HEADER, fg=color, activebackground=HEADER, activeforeground=color,
                           selectcolor=BACKGROUND, anchor="w", font=("Tahoma", 9))
            self._settings_choices.append(widget)
        self._label(channels, text="Changes apply immediately and are saved.\nSystem notices also need Include notices in launcher Settings.",
                 bg=HEADER, fg=MUTED, justify="left", font=("Tahoma", 9), wraplength=550).pack(anchor="w", pady=(5, 0))
        actions = tk.Frame(content, bg=HEADER)
        actions.pack(fill="x", pady=(14, 0))
        self._settings_actions = [
            self._button(actions, text="Reset placement & unlock", command=self._reset_placement,
                         bg=BORDER, fg="#fff8e5", relief="flat", padx=10, pady=7),
            self._button(actions, text="Done", command=self._close_settings, bg=BORDER, fg="#fff8e5",
                         relief="flat", padx=18, pady=7)]

    def _layout_settings_content(self, width):
        # Every control stays reachable at the overlay's minimum width, including
        # longer translated labels. The drawer scrolls when the monitor is short.
        for widget, _source in self._localized_widgets[self._settings_text_start:]:
            widget.configure(wraplength=max(1, width - 28), justify="left")
        self._settings_content.update_idletasks()
        action_width = sum(widget.winfo_reqwidth() + 5 for widget in self._settings_channel_actions)
        self._settings_channel_heading.configure(wraplength=max(1, width - action_width))
        choice_width = max((widget.winfo_reqwidth() for widget in self._settings_choices), default=1)
        columns = choice_columns(width, choice_width)
        for index, widget in enumerate(self._settings_choices):
            widget.grid(row=index // columns, column=index % columns,
                        sticky="w", padx=(0, 6), pady=2)
        stacked = sum(widget.winfo_reqwidth() for widget in self._settings_actions) + 8 > width
        for index, widget in enumerate(self._settings_actions):
            widget.pack_forget()
            widget.pack(side="top" if stacked else ("left" if index == 0 else "right"),
                        fill="x" if stacked else "none", pady=2)

    def _close_settings(self):
        self._pending_drawer = None
        if self._settings_drawer is not None:
            self._settings_drawer.close()

    def _destroy_settings(self):
        if getattr(self, "_settings_drawer", None) is not None:
            self._settings_drawer.destroy()
            if hasattr(self, "_settings_text_start"):
                del self._localized_widgets[self._settings_text_start:]
        self._settings_drawer = None
        self._settings_window = None
        self._vars = {}
        self._channel_vars = {}

    def destroy(self):
        if self._destroyed:
            return
        self._remember_geometry()
        self._pending_drawer = None
        self._destroyed = True
        if self._geometry_timer:
            self.root.after_cancel(self._geometry_timer)
            self._geometry_timer = None
        if self._layout_timer:
            self.root.after_cancel(self._layout_timer)
            self._layout_timer = None
        self._destroy_settings()
        if getattr(self, "_tray", None) is not None:
            self._tray.destroy()
        self._reply_records.clear()
        self.window.destroy()
