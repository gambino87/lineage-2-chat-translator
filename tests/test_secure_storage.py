"""Native Windows security tests touch disposable fixture trees only."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import secure_storage as storage


class StoragePathTests(unittest.TestCase):
    def test_rejects_broad_relative_and_traversal_paths(self):
        for value in (Path.home(), Path(Path.home().anchor), Path("relative"), Path.home() / "a" / ".." / "b"):
            with self.subTest(value=value), self.assertRaises(storage.StorageError):
                storage._canonical(value)


@unittest.skipUnless(os.name == "nt", "Native Windows handles and ACLs are required")
class WindowsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l2ct-storage-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.boundary = self.base / "owned-fixture"
        self.boundary.mkdir()
        self.api = storage._Windows()
        self.protect(self.boundary)
        # Machine-owned profile ACLs are never modified by a regression test.
        self.ancestors = patch.object(storage, "_ancestor_paths", lambda path: [parent for parent in reversed(path.parents) if parent == self.boundary or parent.is_relative_to(self.boundary)])
        self.ancestors.start()
        self.addCleanup(self.ancestors.stop)
        self.root = self.boundary / "app"

    def protect(self, path):
        handle = self.api.open(path, repair=True)
        try:
            self.api.protect(handle, path.is_dir())
        finally:
            self.api.k.CloseHandle(handle)

    def security(self, path):
        handle = self.api.open(path, ancestor=True)
        try:
            return self.api.security(handle)
        finally:
            self.api.k.CloseHandle(handle)

    def set_acl(self, path, sddl):
        # Test-only permission mutation, restricted to our disposable boundary.
        import ctypes
        descriptor = ctypes.c_void_p()
        self.assertTrue(path.is_relative_to(self.boundary))
        self.assertTrue(self.api.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        handle = self.api.open(path, repair=True)
        try:
            present, defaulted = storage.w.BOOL(), storage.w.BOOL()
            acl = ctypes.c_void_p()
            self.api.a.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted))
            self.assertEqual(self.api.a.SetSecurityInfo(handle, 1, 0x80000004, None, None, acl, None), 0)
        finally:
            self.api.k.CloseHandle(handle)
            self.api.k.LocalFree(descriptor)

    def private_sddl(self, extra=""):
        return "D:P" + "".join(f"(A;OICI;FA;;;{sid})" for sid in self.api.trust) + extra

    def test_atomic_private_create_keeps_owner_full_read_write_delete(self):
        before = self.security(self.boundary)
        with storage.guard_private_tree(self.root, create=True):
            payload = self.root / "user-file.txt"
            payload.write_text("editable")
            payload.write_text(payload.read_text() + " by owner")
            self.assertEqual(payload.read_text(), "editable by owner")
            payload.unlink()
        self.assertEqual(before, self.security(self.boundary))
        security = self.security(self.root)
        self.assertTrue(self.api.private(security, True, root=True))
        self.root.rmdir()  # No privilege elevation or app-mediated deletion.

    def test_existing_private_tree_verifies_and_stays_editable(self):
        with storage.guard_private_tree(self.root, create=True):
            (self.root / "nested").mkdir()
            (self.root / "nested" / "file").write_text("before")
        with storage.guard_private_tree(self.root):
            (self.root / "nested" / "file").write_text("after")
            temporary = self.root / "replacement.part"
            temporary.write_text("replaceable")
            temporary.replace(self.root / "replacement.bin")
        self.assertEqual((self.root / "nested" / "file").read_text(), "after")

    def test_root_substitution_during_lock_downgrade_is_detected(self):
        original = storage._Windows.open
        fired = False
        def racing_open(api, path, **options):
            nonlocal fired
            if path == self.root and options.get("ancestor") and not fired:
                fired = True
                self.root.rename(self.boundary / "original")
                self.root.mkdir()
                (self.root / "unrelated").write_text("preserve")
            return original(api, path, **options)
        with patch.object(storage._Windows, "open", racing_open), self.assertRaisesRegex(storage.StorageError, "changed"):
            with storage.guard_private_tree(self.root, create=True):
                self.fail("Substituted root was accepted")
        self.assertTrue(fired)
        self.assertEqual((self.root / "unrelated").read_text(), "preserve")

    def test_locked_file_is_readable_and_excludes_writes_and_delete(self):
        with storage.guard_private_tree(self.root, create=True):
            target = self.root / "runtime.exe"
            target.write_bytes(b"fixture executable")
            with storage.locked_file(target) as stream:
                self.assertEqual(stream.read(), b"fixture executable")
                with self.assertRaises(OSError):
                    target.write_bytes(b"changed")
                with self.assertRaises(OSError):
                    target.unlink()
            target.write_bytes(b"owner can edit")
            target.unlink()

    def test_legacy_repair_requires_ownership_validation_and_preserves_bytes(self):
        self.root.mkdir()
        self.set_acl(self.root, self.private_sddl("(A;OICI;0x1301ff;;;AU)"))
        child = self.root / "model.bin"
        child.write_bytes(b"model")
        with self.assertRaisesRegex(storage.StorageError, "ownership"):
            with storage.guard_private_tree(self.root):
                pass
        checked = []
        def authorize(path):
            # Real ownership callbacks must be able to read pinned records using
            # normal Python file APIs, while writes/deletion stay excluded.
            self.assertEqual(child.read_bytes(), b"model")
            with self.assertRaises(OSError):
                child.write_bytes(b"modified during preflight")
            checked.append(path)
            return True
        with storage.guard_private_tree(self.root, authorize_existing=authorize):
            self.assertEqual(child.read_bytes(), b"model")
        self.assertEqual(checked, [self.root])
        self.assertTrue(self.api.private(self.security(self.root), True, root=True))
        self.assertTrue(self.api.private(self.security(child), False))

    def test_unsafe_parent_is_rejected_without_modifying_it(self):
        self.set_acl(self.boundary, self.private_sddl("(A;;0x40;;;AU)"))
        before = self.security(self.boundary)
        with self.assertRaisesRegex(storage.StorageError, "parent"):
            with storage.guard_private_tree(self.root, create=True):
                pass
        self.assertEqual(before, self.security(self.boundary))
        self.assertFalse(self.root.exists())

    def test_parent_create_only_and_inherit_only_grants_are_not_replacement_rights(self):
        self.set_acl(self.boundary, self.private_sddl("(A;;0x4;;;AU)(A;OICIIO;GA;;;AU)"))
        with storage.guard_private_tree(self.root, create=True):
            self.assertTrue(self.root.exists())

    def test_guard_holds_root_against_rename(self):
        with storage.guard_private_tree(self.root, create=True):
            with self.assertRaises(OSError):
                self.root.rename(self.boundary / "moved")
        self.root.rename(self.boundary / "moved")

    def test_nested_guards_keep_ancestor_lock_without_share_conflicts(self):
        with storage.guard_private_tree(self.root, create=True):
            with storage.guard_private_tree(self.root / "stage", create=True):
                (self.root / "stage" / "payload").write_text("test")
            with self.assertRaises(OSError):
                self.root.rename(self.boundary / "moved")

    def test_cross_thread_exit_removes_entry_thread_borrowed_handle(self):
        guard = storage.guard_private_tree(self.root, create=True)
        errors = []
        entered = threading.Event()
        closed = threading.Event()
        def worker():
            try:
                guard.__enter__()
                entered.set()
                if not closed.wait(5):
                    raise AssertionError("Closing thread did not finish")
                self.assertNotIn(self.root, storage._held_roots.handles)
                # A new guard may acquire this path; no stale handle is borrowed.
                with storage.guard_private_tree(self.root):
                    pass
            except BaseException as exc:
                errors.append(exc)
                entered.set()
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            if not errors:
                guard.__exit__(None, None, None)
        finally:
            closed.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_locked_ancestor_mode_preserves_private_root_and_parent_acl(self):
        self.set_acl(self.boundary, self.private_sddl("(A;;0x40;;;AU)"))
        before = self.security(self.boundary)
        with storage.guard_private_tree(self.root, create=True, allow_locked_ancestors=True):
            self.assertTrue(self.root.exists())
            with self.assertRaises(OSError):
                self.boundary.rename(self.base / "replaced-boundary")
        self.assertEqual(before, self.security(self.boundary))

    def test_hardlinked_payload_rejected_without_changing_external_acl(self):
        self.root.mkdir()
        self.protect(self.root)
        outside = self.boundary / "outside.txt"
        outside.write_text("keep")
        os.link(outside, self.root / "linked")
        before = self.security(outside)
        with self.assertRaisesRegex(storage.StorageError, "Hard-linked"):
            with storage.guard_private_tree(self.root, authorize_existing=lambda _: True):
                pass
        with self.assertRaisesRegex(storage.StorageError, "Hard-linked"):
            storage.safe_remove_tree(self.root)
        self.assertEqual(before, self.security(outside))
        self.assertEqual(outside.read_text(), "keep")

    def test_safe_remove_nested_tree_and_file_preserves_siblings(self):
        self.root.mkdir()
        (self.root / "nested").mkdir()
        (self.root / "nested" / "model.bin").write_bytes(b"model")
        outside = self.boundary / "outside.txt"
        outside.write_text("keep")
        self.assertTrue(storage.safe_remove_tree(self.root))
        self.assertFalse(self.root.exists())
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue(storage.safe_remove_tree(outside))
        self.assertFalse(storage.safe_remove_tree(outside))

    def test_junction_inserted_before_child_open_is_rejected(self):
        import _winapi
        self.root.mkdir()
        child = self.root / "models"
        child.mkdir()
        outside = self.boundary / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        original = storage._Windows.open
        fired = False
        def racing_open(api, path, **options):
            nonlocal fired
            if path == child and not fired:
                fired = True
                child.rename(self.root / "original-models")
                _winapi.CreateJunction(str(outside), str(child))
            return original(api, path, **options)
        with patch.object(storage._Windows, "open", racing_open), self.assertRaises(OSError):
            storage.safe_remove_tree(self.root)
        self.assertTrue(fired)
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        if child.is_junction():
            child.rmdir()

    def test_existing_junction_is_rejected_without_deleting_outside_target(self):
        import _winapi
        self.root.mkdir()
        outside = self.boundary / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        junction = self.root / "redirected"
        _winapi.CreateJunction(str(outside), str(junction))
        try:
            with self.assertRaisesRegex(storage.StorageError, "linked|redirected"):
                storage.safe_remove_tree(self.root)
            self.assertEqual((outside / "keep.txt").read_text(), "keep")
        finally:
            junction.rmdir()

    def test_swap_after_preflight_cannot_replace_locked_child(self):
        self.root.mkdir()
        child = self.root / "models"
        child.mkdir()
        (child / "owned").write_text("owned")
        original = storage._Windows.delete
        failures = []
        def racing_delete(api, handle):
            if not failures:
                try:
                    child.rename(self.root / "moved")
                except OSError:
                    failures.append(True)
            return original(api, handle)
        with patch.object(storage._Windows, "delete", racing_delete):
            storage.safe_remove_tree(self.root)
        self.assertEqual(failures, [True])
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
