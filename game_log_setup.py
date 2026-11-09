"""Resolve a user-selected Lineage II folder without changing game files."""
from pathlib import Path


def resolve_system_log(selected: str | Path) -> Path:
    """Accept a system folder or its game root; Network.log may not exist yet."""
    if not str(selected).strip():
        raise ValueError("Choose your Lineage II system folder.")
    folder = Path(selected).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("That folder is unavailable. Choose your Lineage II system folder.")
    if folder.name.casefold() != "system":
        folder = folder / "system"
        if not folder.is_dir():
            raise ValueError("Choose the system folder inside your Lineage II installation.")
    files = {entry.name.casefold(): entry for entry in folder.iterdir() if entry.is_file()}
    if not {"l2.exe", "l2.bin", "engine.dll", "l2.ini", "network.log"}.intersection(files):
        raise ValueError("This does not look like a Lineage II system folder. Choose the folder containing L2.exe, L2.ini, or Network.log.")
    log = files.get("network.log", folder / "Network.log")
    if log.exists() and not log.is_file():
        raise ValueError("Network.log is a folder here. Choose a different Lineage II installation.")
    return log
