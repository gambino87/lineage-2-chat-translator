"""Serialize local build/export/finalization work; never starts an application."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path


class ReleaseBusyError(RuntimeError):
    """Another cooperating release operation owns this workspace."""


def mutex_name(root: Path) -> str:
    identity = os.path.normcase(str(Path(root).resolve())).encode("utf-8")
    return "Local\\L2CTRelease-" + hashlib.sha256(identity).hexdigest()[:32]


@contextmanager
def release_lock(root: Path):
    """Fail visibly instead of pruning tools in use by another release process.

    Windows mutex ownership is thread-reentrant: an exporter can call a source
    publisher while both use this guard. Every acquisition has its own release.
    A crashed owner does not leave a permanent stale lock file.
    """
    if os.name != "nt":
        raise RuntimeError("The release workflow lock requires Windows.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, mutex_name(root))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        # WAIT_ABANDONED grants ownership after a crashed release process.
        if status not in (0, 0x80):
            if status == 0x102:
                raise ReleaseBusyError("Another build or release operation is running. Finish it before retrying.")
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
