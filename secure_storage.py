"""Private app-owned storage and non-following, handle-based Windows cleanup.

The owner retains full control, including ordinary Explorer edits and deletion.
Only a dedicated application tree may be passed here. Ancestors are inspected
and held against replacement, never repaired. These protections separate Windows
accounts; they cannot defend against another process running as the same user.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import base64
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import shutil
import stat
import threading
import json


class StorageError(OSError):
    """Storage cannot safely be used without changing unrelated permissions."""


_REPARSE = 0x400
_DIRECTORY = 0x10
_FULL = 0x1F01FF
_ANCESTOR_DANGER = 0x500D0040  # delete/delete-child, change owner/DACL, generic write/all
_MAX_ENTRIES = 100000
_MAX_DEPTH = 64
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_held_roots = threading.local()
_MAX_RECORD_BYTES = 256 * 1024
_MAX_PROOF_BYTES = 512 * 1024
_IDENTITY_UNSET = object()


class _Blob(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("data", ctypes.c_void_p)]


def _record_bytes(value):
    result = json.dumps({key: item for key, item in value.items() if key != "owner_proof"},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(result) > _MAX_RECORD_BYTES:
        raise StorageError("The cleanup ownership record is too large.")
    return result


def _dpapi(content: bytes, *, decrypt=False) -> bytes:
    """Bind cleanup authorization to this Windows account, without hiding JSON."""
    if os.name != "nt":
        raise StorageError("Windows account protection is required for cleanup ownership records.")
    if len(content) > (_MAX_PROOF_BYTES if decrypt else _MAX_RECORD_BYTES):
        raise StorageError("The cleanup ownership proof is too large.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.restype = w.BOOL
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p if decrypt else w.LPCWSTR,
                         ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(_Blob)]
    buffer = ctypes.create_string_buffer(content)
    entropy_buffer = ctypes.create_string_buffer(b"Lineage2ChatTranslator cleanup ownership v2")
    source = _Blob(len(content), ctypes.cast(buffer, ctypes.c_void_p))
    entropy = _Blob(len(entropy_buffer.raw) - 1, ctypes.cast(entropy_buffer, ctypes.c_void_p))
    output = _Blob()
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        raise StorageError("The cleanup ownership record could not be verified for this Windows account. Review the selected folder in Configure.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def seal_record(value: dict) -> dict:
    """Keep readable fields plus a current-user proof that must match exactly."""
    result = {key: item for key, item in value.items() if key != "owner_proof"}
    result["owner_proof"] = base64.b64encode(_dpapi(_record_bytes(result))).decode("ascii")
    return result


def verify_record(value: dict) -> bool:
    try:
        proof = value.get("owner_proof")
        if not isinstance(proof, str) or len(proof) > (_MAX_PROOF_BYTES * 4 // 3 + 4):
            return False
        return _dpapi(base64.b64decode(proof, validate=True), decrypt=True) == _record_bytes(value)
    except (StorageError, ValueError, TypeError):
        return False


def path_identity(path: str | Path) -> list[int]:
    """Return the exact directory identity; a same-path replacement differs."""
    root = _canonical(path)
    if os.name != "nt":
        info = root.lstat()
        return [info.st_dev, info.st_ino]
    api = _Windows()
    with ExitStack() as held:
        _hold_ancestors(api, held, root, check_permissions=False)
        records = getattr(_held_roots, "handles", {}).get(root, [])
        if records:
            info = api.inspect(records[-1][1], root)
        else:
            _, info = _open_checked(api, held, root, ancestor=True)
        return [info.volume, info.index_high, info.index_low]


def private_directory(path: str | Path) -> bool:
    """Check a control-record container without repairing its permissions."""
    if os.name != "nt":
        return True
    root = _canonical(path)
    api = _Windows()
    try:
        with ExitStack() as held:
            _hold_ancestors(api, held, root, check_permissions=False)
            records = getattr(_held_roots, "handles", {}).get(root, [])
            handle = records[-1][1] if records else _open_checked(api, held, root, ancestor=True)[0]
            return api.private(api.security(handle), True, root=True)
    except OSError:
        return False


def _canonical(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() or ".." in value.parts:
        raise StorageError("Choose an absolute, dedicated application folder without traversal components.")
    value = Path(os.path.abspath(value))
    if value == Path(value.anchor) or value == Path.home():
        raise StorageError("A drive root or your home folder cannot be application storage.")
    if os.name == "nt" and (str(value).startswith("\\\\") or ":" in str(value)[2:]):
        raise StorageError("Use a dedicated folder on a local disk.")
    reserved = [Path(os.environ[key]) for key in
                ("LOCALAPPDATA", "APPDATA", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)")
                if os.environ.get(key)]
    if value in reserved:
        raise StorageError("Choose a separate application folder, not a shared system or user folder.")
    return value


def _ancestor_paths(path: Path) -> list[Path]:
    return list(reversed(path.parents))


class _Info(ctypes.Structure):
    _fields_ = [(name, w.DWORD) for name in (
        "attributes", "creation_low", "creation_high", "access_low", "access_high",
        "write_low", "write_high", "volume", "size_high", "size_low", "links", "index_high", "index_low")]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


class _Acl(ctypes.Structure):
    _fields_ = [("revision", w.BYTE), ("reserved", w.BYTE), ("size", w.WORD),
                ("count", w.WORD), ("reserved2", w.WORD)]


class _Ace(ctypes.Structure):
    _fields_ = [("type", w.BYTE), ("flags", w.BYTE), ("size", w.WORD), ("mask", w.DWORD)]


class _Windows:
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self._declare(self.k, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE)
        self._declare(self.k, "CloseHandle", w.BOOL, w.HANDLE)
        self._declare(self.k, "GetFileInformationByHandle", w.BOOL, w.HANDLE, ctypes.POINTER(_Info))
        self._declare(self.k, "GetFinalPathNameByHandleW", w.DWORD, w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD)
        self._declare(self.k, "CreateDirectoryW", w.BOOL, w.LPCWSTR, ctypes.POINTER(_SecurityAttributes))
        self._declare(self.k, "SetFileInformationByHandle", w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
        self._declare(self.k, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
        self._declare(self.k, "GetCurrentProcess", w.HANDLE)
        self._declare(self.a, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        self._declare(self.a, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD))
        self._declare(self.a, "ConvertSidToStringSidW", w.BOOL, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self._declare(self.a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        self._declare(self.a, "GetSecurityDescriptorDacl", w.BOOL, ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL))
        self._declare(self.a, "GetSecurityDescriptorControl", w.BOOL, ctypes.c_void_p, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD))
        self._declare(self.a, "GetSecurityInfo", w.DWORD, w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self._declare(self.a, "SetSecurityInfo", w.DWORD, w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        self._declare(self.a, "GetAce", w.BOOL, ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p))
        self.trust = tuple(dict.fromkeys((self.current_sid(), "S-1-5-18", "S-1-5-32-544")))

    @staticmethod
    def _declare(library, name, result, *args):
        function = getattr(library, name)
        function.restype, function.argtypes = result, args

    @staticmethod
    def fail(message, code=None):
        code = ctypes.get_last_error() if code is None else code
        raise StorageError(f"{message} (Windows error {code}).")

    def sid(self, pointer):
        text = ctypes.c_void_p()
        if not self.a.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
            self.fail("Cannot inspect storage ownership")
        try:
            return ctypes.wstring_at(text)
        finally:
            self.k.LocalFree(text)

    def current_sid(self):
        token = w.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, ctypes.byref(token)):
            self.fail("Cannot identify the current Windows account")
        try:
            size = w.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                self.fail("Cannot identify the current Windows account")
            return self.sid(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            self.k.CloseHandle(token)

    def open(self, path, *, ancestor=False, repair=False, delete=False, repair_directory=True):
        # Read data enforces writer sharing checks. Never share deletion. Using
        # MAXIMUM_ALLOWED suppresses automatic child ACL propagation, so only
        # individually preflighted handles can have their permissions changed.
        # Files have no children to propagate to. Avoid MAXIMUM_ALLOWED there:
        # its DELETE/write rights would prevent ownership callbacks from opening
        # the already-pinned marker for an ordinary read.
        access = 0x20001 | (0x40000 if repair else 0) | (0x02000000 if repair and repair_directory else 0) | (0x10000 if delete else 0)
        handle = self.k.CreateFileW(str(path), access, 3 if ancestor else 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            if code in (2, 3):
                raise FileNotFoundError(str(path))
            self.fail("Storage is in use or cannot be opened safely; close it and retry", code)
        return handle

    def inspect(self, handle, path):
        info = _Info()
        if not self.k.GetFileInformationByHandle(handle, ctypes.byref(info)):
            self.fail("Cannot inspect storage")
        buffer = ctypes.create_unicode_buffer(32768)
        length = self.k.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            self.fail("Cannot resolve storage safely")
        actual = buffer.value.removeprefix("\\\\?\\")
        if os.path.normcase(actual.rstrip("\\")) != os.path.normcase(str(path).rstrip("\\")):
            raise StorageError("A storage path resolves to another location.")
        if info.attributes & _REPARSE:
            raise StorageError("Storage will not follow a linked or redirected path.")
        if not info.attributes & _DIRECTORY and info.links != 1:
            raise StorageError("Hard-linked storage files are not supported.")
        return info

    def security(self, handle):
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.a.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        if error:
            self.fail("Cannot inspect storage permissions", error)
        try:
            if not owner or not acl:
                raise StorageError("Storage has unrestricted or unknown ownership permissions.")
            control, revision = w.WORD(), w.DWORD()
            if not self.a.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                self.fail("Cannot inspect storage permissions")
            entries = []
            for index in range(ctypes.cast(acl, ctypes.POINTER(_Acl)).contents.count):
                pointer = ctypes.c_void_p()
                if not self.a.GetAce(acl, index, ctypes.byref(pointer)):
                    self.fail("Cannot inspect storage access rules")
                ace = ctypes.cast(pointer, ctypes.POINTER(_Ace)).contents
                if ace.type not in (0, 1):
                    raise StorageError("Storage uses unsupported Windows access rules.")
                entries.append((ace.type, ace.flags, ace.mask, self.sid(pointer.value + 8)))
            return self.sid(owner), control.value, entries
        finally:
            self.k.LocalFree(descriptor)

    def verify_owner(self, security, *, ancestor=False):
        trusted = (*self.trust, _TRUSTED_INSTALLER) if ancestor else self.trust
        if security[0] not in trusted:
            raise StorageError("Storage is owned by another account; choose a private folder. Parent permissions were not changed.")

    def verify_ancestor(self, handle):
        security = self.security(handle)
        self.verify_owner(security, ancestor=True)
        for kind, flags, mask, sid in security[2]:
            if flags & 8 or kind == 1:
                continue
            if sid not in (*self.trust, _TRUSTED_INSTALLER, "S-1-3-4") and mask & _ANCESTOR_DANGER:
                raise StorageError("Another account can replace a storage parent. Choose a private folder; parent permissions were not changed.")

    def private(self, security, directory, *, root=False):
        self.verify_owner(security)
        entries = security[2]
        expected = 3 if directory else 0
        return ((not root or bool(security[1] & 0x1000)) and len(entries) == len(self.trust)
                and {entry[3] for entry in entries} == set(self.trust)
                and all(kind == 0 and flags & ~16 == expected and mask == _FULL
                        for kind, flags, mask, _ in entries))

    @contextmanager
    def descriptor(self, directory=True):
        flags = "OICI" if directory else ""
        sddl = "D:P" + "".join(f"(A;{flags};FA;;;{sid})" for sid in self.trust)
        descriptor = ctypes.c_void_p()
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            self.fail("Cannot create private storage permissions")
        try:
            yield descriptor
        finally:
            self.k.LocalFree(descriptor)

    def protect(self, handle, directory):
        with self.descriptor(directory) as descriptor:
            present, defaulted, acl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
            if not self.a.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
                self.fail("Cannot prepare private storage permissions")
            error = self.a.SetSecurityInfo(handle, 1, 0x80000004, None, None, acl, None)
            if error:
                self.fail("Cannot protect application storage", error)

    def create(self, path):
        with self.descriptor() as descriptor:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            if not self.k.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                self.fail("Cannot create private application storage")

    def delete(self, handle):
        disposition = w.BOOL(True)
        if not self.k.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            self.fail("Cannot remove application data; close files in use and retry")


def _open_checked(api, stack, path, **options):
    handle = api.open(path, **options)
    stack.callback(api.k.CloseHandle, handle)
    return handle, api.inspect(handle, path)


def _hold_ancestors(api, stack, path, *, check_permissions):
    for parent in _ancestor_paths(path):
        records = getattr(_held_roots, "handles", {}).get(parent, [])
        borrowed = records[-1][1] if records else None
        if borrowed is None:
            handle, info = _open_checked(api, stack, parent, ancestor=True)
        else:
            handle, info = borrowed, api.inspect(borrowed, parent)
        if not info.attributes & _DIRECTORY:
            raise StorageError("A storage parent is not a directory.")
        if check_permissions:
            api.verify_ancestor(handle)
        else:
            api.verify_owner(api.security(handle), ancestor=True)


@contextmanager
def _remember_root(root, handle):
    """Borrow handles only in their entry thread, even if exit runs elsewhere."""
    mapping = getattr(_held_roots, "handles", None)
    if mapping is None:
        mapping = {}
        _held_roots.handles = mapping
    record = (object(), handle)
    records = mapping.setdefault(root, [])
    records.append(record)
    try:
        yield
    finally:
        # Mutate the captured entry-thread mapping, not the closing thread's
        # thread-local value. Never restore an older, already-closed handle.
        records.remove(record)
        if not records and mapping.get(root) is records:
            del mapping[root]


def _collect(api, stack, path, *, repair=False, delete=False):
    entries = []
    def walk(current, depth):
        if depth > _MAX_DEPTH or len(entries) >= _MAX_ENTRIES:
            raise StorageError("The application storage tree is unexpectedly large or deeply nested.")
        expected_directory = current.is_dir()
        handle, info = _open_checked(api, stack, current, repair=repair, delete=delete,
                                     repair_directory=expected_directory)
        directory = bool(info.attributes & _DIRECTORY)
        if directory != expected_directory:
            raise StorageError("The storage entry changed type while being checked; retry safely.")
        entries.append((current, handle, directory))
        if directory:
            for name in os.listdir(current):
                walk(current / name, depth + 1)
    walk(path, 0)
    return entries


def _verify_inventory(entries):
    """Detect an inserted unpreflighted child before trusting a repaired tree."""
    children = {path: set() for path, _, directory in entries if directory}
    for path, _, _ in entries[1:]:
        children[path.parent].add(path.name)
    for directory, names in children.items():
        if set(os.listdir(directory)) != names:
            raise StorageError("Application storage changed while being checked; retry safely.")


@contextmanager
def guard_private_tree(path: str | Path, *, create=False, authorize_existing=None, allow_locked_ancestors=False,
                       expected_identity=_IDENTITY_UNSET):
    """Verify/protect a dedicated tree, holding its ancestry stable until exit.

    Existing nonprivate trees are repaired only if ``authorize_existing(root)``
    explicitly returns True after validating application ownership records. The
    callback runs with all existing entries held against writes/replacement.
    Callers must not authorize arbitrary folders. Descendant handles are released
    after validation so the owner can update files within the protected root.
    ``allow_locked_ancestors`` permits broader ancestor ACLs only while all those
    components remain pinned. The root must then be owned by the current user.
    Callers using this mode must retain the guard throughout sensitive use and
    separately authenticate executable bytes before running them.
    """
    root = _canonical(path)
    if os.name != "nt":
        # Windows is the shipped platform; portable tests must not silently
        # pretend POSIX permissions provide the Windows guarantees.
        raise StorageError("Protected application storage requires Windows.")
    api = _Windows()
    with ExitStack() as ancestors:
        _hold_ancestors(api, ancestors, root, check_permissions=not allow_locked_ancestors)
        created = False
        try:
            root.lstat()
            if expected_identity is None:
                raise StorageError("The selected folder appeared after confirmation. Review the folder in Configure again.")
        except FileNotFoundError:
            if expected_identity is not _IDENTITY_UNSET and expected_identity is not None:
                raise StorageError("The selected folder disappeared after confirmation. Review it in Configure again.")
            if not create:
                raise
            api.create(root)
            created = True
        with ExitStack() as tree:
            entries = _collect(api, tree, root, repair=True)
            if not entries[0][2]:
                raise StorageError("Application storage must be a directory.")
            if expected_identity is not _IDENTITY_UNSET and expected_identity is not None:
                info = api.inspect(entries[0][1], root)
                if [info.volume, info.index_high, info.index_low] != list(expected_identity):
                    raise StorageError("The selected folder changed after confirmation. Review the folder in Configure again.")
            private = []
            for entry, handle, directory in entries:
                security = api.security(handle)
                if allow_locked_ancestors and entry == root and security[0] != api.trust[0]:
                    raise StorageError("Locked application storage must be owned by the current Windows account.")
                private.append(api.private(security, directory, root=entry == root))
            _verify_inventory(entries)
            if not all(private):
                authorized = created
                if not created and authorize_existing is not None:
                    # Ownership callbacks may themselves use locked_file. Borrow
                    # the pinned directories rather than reopening repair
                    # handles with an incompatible Windows sharing mode.
                    with ExitStack() as preflight_bindings:
                        for entry, handle, directory in entries:
                            if directory:
                                preflight_bindings.enter_context(_remember_root(entry, handle))
                        authorized = authorize_existing(root) is True
                if not authorized:
                    raise StorageError("Existing application storage is not private. Its ownership must be verified before permissions can be repaired.")
                for (_, handle, directory), already_private in zip(entries, private):
                    if not already_private:
                        api.protect(handle, directory)
                for entry, handle, directory in entries:
                    if not api.private(api.security(handle), directory, root=entry == root):
                        raise StorageError("Application storage permissions could not be verified.")
            _verify_inventory(entries)
            # Downgrade the repair handle so ordinary child rename/replace can
            # proceed. MAXIMUM_ALLOWED includes DELETE, so its no-share-delete
            # handle cannot coexist with the narrower long-lived handle. Keep
            # every descendant and ancestor pinned, reopen, and require the
            # exact volume/file ID before use. A root swapped during this small
            # interval is rejected; its contents/ACL are never used or changed.
            old_info = api.inspect(entries[0][1], root)
            tree.pop_all()
            api.k.CloseHandle(entries[0][1])
            try:
                root_handle, new_info = _open_checked(api, ancestors, root, ancestor=True)
                if ((old_info.volume, old_info.index_high, old_info.index_low) !=
                        (new_info.volume, new_info.index_high, new_info.index_low)):
                    raise StorageError("The application storage folder changed during validation; retry safely.")
            finally:
                for _, handle, _ in entries[1:]:
                    api.k.CloseHandle(handle)
        with _remember_root(root, root_handle):
            yield root


@contextmanager
def locked_file(path: str | Path):
    """Yield a binary reader while excluding writers/deletion of a plain file.

    The stream owns the native file handle. Ancestors remain pinned until exit;
    reparse points and hard links are refused before any bytes are read.
    """
    root = _canonical(path)
    if os.name != "nt":
        raise StorageError("Locked executable storage requires Windows.")
    import msvcrt
    api = _Windows()
    with ExitStack() as ancestors:
        _hold_ancestors(api, ancestors, root, check_permissions=False)
        handle = api.open(root)
        try:
            info = api.inspect(handle, root)
            if info.attributes & _DIRECTORY:
                raise StorageError("Expected an application file, not a directory.")
            api.verify_owner(api.security(handle))
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            api.k.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            yield stream


@contextmanager
def locked_directory(path: str | Path):
    """Pin an owned directory and ancestry without changing any permissions."""
    root = _canonical(path)
    if os.name != "nt":
        yield root
        return
    api = _Windows()
    with ExitStack() as held:
        _hold_ancestors(api, held, root, check_permissions=False)
        handle, info = _open_checked(api, held, root, ancestor=True)
        if not info.attributes & _DIRECTORY:
            raise StorageError("Expected an application directory.")
        api.verify_owner(api.security(handle))
        with _remember_root(root, handle):
            yield root


def safe_remove_tree(path: str | Path, *, missing_ok=True, expected_identity=None) -> bool:
    """Remove one caller-scoped owned file/tree without following redirections.

    All objects are opened before deletion. Windows deletes the checked objects
    by handle, bottom up; competing rename/junction swaps either fail while the
    handles are held or are rejected when opened. No parent ACL is ever changed.
    """
    root = _canonical(path)
    if os.name != "nt":
        # Python's descriptor-relative rmtree is safe on supporting POSIX hosts.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise StorageError("Safe recursive deletion is unavailable on this platform.")
        try:
            info = root.lstat()
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        if stat.S_ISLNK(info.st_mode) or (not stat.S_ISDIR(info.st_mode) and info.st_nlink != 1):
            raise StorageError("Cleanup will not follow linked files or folders.")
        if expected_identity is not None and list(expected_identity) != [info.st_dev, info.st_ino]:
            raise StorageError("Cleanup target changed identity; the original path was kept.")
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(root)
        else:
            root.unlink()
        return True
    api = _Windows()
    with ExitStack() as ancestors:
        try:
            _hold_ancestors(api, ancestors, root, check_permissions=False)
            # Separate per-object stacks let deleted child handles close before
            # deleting the parent, while every still-live path remains locked.
            with ExitStack() as tree:
                entries = _collect(api, tree, root, delete=True)
                if expected_identity is not None:
                    root_info = api.inspect(entries[0][1], root)
                    if list(expected_identity) != [root_info.volume, root_info.index_high, root_info.index_low]:
                        raise StorageError("Cleanup target changed identity; the original path was kept.")
                # Pop ownership of callbacks; close each exactly once below.
                handles = tree.pop_all()
                try:
                    for index in range(len(entries) - 1, -1, -1):
                        _, handle, _ = entries[index]
                        api.delete(handle)
                        api.k.CloseHandle(handle)
                        entries[index] = (None, None, None)
                finally:
                    # callbacks cannot be used after individually closing handles
                    # (Windows may reuse numeric handles); close remaining only.
                    for _, handle, _ in entries:
                        if handle is not None:
                            api.k.CloseHandle(handle)
                    handles.pop_all()
        except FileNotFoundError:
            if not root.exists():
                if missing_ok:
                    return False
                raise
            raise StorageError("Application storage changed during cleanup; retry safely.")
    return True
