"""Shared icon assets for source and frozen application windows."""
from pathlib import Path
import tkinter as tk


ASSETS = Path(__file__).resolve().parent / "assets"
ICON = ASSETS / "L2CT.ico"
PNG = ASSETS / "L2CT.png"


def apply_window_icon(window):
    """Set this root and the default icon inherited by its later child windows."""
    try:
        window.iconbitmap(default=str(ICON))
    except tk.TclError:
        # PNG also supports Tk builds without Windows .ico handling.
        try:
            window._app_icon_image = tk.PhotoImage(master=window, file=str(PNG))
            window.iconphoto(True, window._app_icon_image)
        except tk.TclError:
            pass  # A damaged/missing icon must not prevent app startup or errors.
