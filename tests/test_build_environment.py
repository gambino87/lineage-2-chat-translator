"""Pinned dependency discovery and collected-byte checks using synthetic files."""
import copy
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest


_SPEC = importlib.util.spec_from_file_location(
    "l2_build_environment", Path(__file__).resolve().parents[1] / "packaging/build_environment.py"
)
environment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(environment)


class BuildEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.windows = self.root / "Windows"
        self.system = self.windows / "System32"
        self.python = self.root / "Python312"
        self.scripts = self.root / "venv" / "Scripts"
        self.foreign = self.root / "unrelated development tool"
        self.app = self.root / "current app"
        for directory in (self.system, self.python / "DLLs", self.scripts, self.foreign, self.app / "_internal"):
            directory.mkdir(parents=True)
        self.interpreter = self.scripts / "python.exe"

    def clean(self, inherited, **kwargs):
        return environment.clean_environment(inherited, windows=self.windows, system=self.system, **kwargs)

    def native(self, name, content=None, *, foreign=False):
        source = (self.foreign if foreign else self.python / "DLLs") / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content if content is not None else ("synthetic " + name).encode())
        return (name, str(source), "BINARY" if name.casefold().endswith(".dll") else "EXTENSION")

    def collected(self):
        entries = [self.native(name) for name in sorted(environment.EXPECTED_DLLS)]
        entries.append(self.native("_ssl.pyd"))
        kept, audit = environment.select_binaries(entries, [self.python])
        for name, source, _ in kept:
            shutil.copyfile(source, self.app / "_internal" / name)
        return audit

    def test_build_environment_strips_ambient_paths_and_python_tcl_bootloader_overrides(self):
        inherited = {
            "Path": str(self.foreign), "PATH": "second hostile path",
            "pYtHoNpAtH": str(self.foreign), "PYTHONHOME": str(self.foreign),
            "PythonStartup": "startup.py", "tCl_LiBrArY": str(self.foreign),
            "TK_LIBRARY": str(self.foreign), "TCLLIBPATH": str(self.foreign),
            "PYINSTALLER_CONFIG_DIR": str(self.foreign), "pyinstaller_strict_collect_mode": "0",
            "_PYI_APPLICATION_HOME_DIR": str(self.foreign), "_MEIPASS2": str(self.foreign),
            "LD_LIBRARY_PATH": str(self.foreign), "DYLD_LIBRARY_PATH": str(self.foreign),
            "VIRTUAL_ENV": str(self.foreign), "systemroot": str(self.foreign),
            "WinDir": str(self.foreign), "TEMP": str(self.root), "LANG": "en_US.UTF-8",
        }
        unchanged = inherited.copy()
        cache = self.root / "build cache"
        result = self.clean(inherited, python_root=self.python, interpreter=self.interpreter, cache=cache)
        self.assertEqual(inherited, unchanged)
        self.assertEqual(result, {
            "TEMP": str(self.root), "LANG": "en_US.UTF-8",
            "PATH": os.pathsep.join(map(str, (self.scripts, self.python, self.python / "DLLs", self.system, self.windows))),
            "SystemRoot": str(self.windows), "WINDIR": str(self.windows), "PYINSTALLER_CONFIG_DIR": str(cache),
        })

    def test_frozen_validation_path_has_no_python_or_development_tool_fallback(self):
        result = self.clean({"PATH": os.pathsep.join(map(str, (self.python, self.foreign))),
                             "PYTHONPATH": str(self.python), "TEMP": str(self.root)})
        self.assertEqual(result["PATH"].split(os.pathsep), [str(self.system), str(self.windows)])
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("VIRTUAL_ENV", result)
        self.assertEqual(result["TEMP"], str(self.root))

    def test_invalid_search_directories_or_incomplete_toolchain_are_rejected(self):
        with self.assertRaises(ValueError):
            self.clean({}, python_root=self.python)
        with self.assertRaises(ValueError):
            self.clean({}, python_root=self.root / "absent", interpreter=self.interpreter)
        with self.assertRaises(ValueError):
            self.clean({}, cache=Path("relative-cache"))
        with self.assertRaises(ValueError):
            environment.clean_environment({}, windows=Path("relative-windows"), system=self.system)

    def test_foreign_source_is_rejected_before_even_an_os_library_can_be_omitted(self):
        for name in ("ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "libcrypto-3.dll", "_ssl.pyd"):
            with self.subTest(name=name):
                entry = self.native(name, foreign=True)
                with self.assertRaisesRegex(ValueError, "outside the build toolchain"):
                    environment.select_binaries([entry], [self.python, self.system])

    def test_approved_root_prefix_does_not_admit_a_sibling_folder(self):
        sibling = self.root / "Python312-untrusted"
        sibling.mkdir()
        source = sibling / "libcrypto-3.dll"
        source.write_bytes(b"outside despite shared prefix")
        with self.assertRaises(ValueError):
            environment.select_binaries([("libcrypto-3.dll", str(source), "BINARY")], [self.python])

    def test_os_compatibility_filter_is_narrow_and_keeps_vc_runtime_and_extensions(self):
        omitted = ["ucrtbase.dll", "API-MS-WIN-CORE-console-l1-1-0.DLL", "api-ms-win-crt-runtime-l1-1-0.dll"]
        retained = ["VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "python312.dll", "api-ms-other-library.dll", "_ssl.pyd"]
        entries = [self.native(name) for name in omitted + retained]
        kept, audit = environment.select_binaries(entries, [self.python])
        self.assertEqual([row[0] for row in kept], retained)
        self.assertEqual([row["name"] for row in audit["os_provided"]], omitted)
        self.assertEqual([row["name"] for row in audit["retained"]], retained)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in audit["retained"]))
        self.assertTrue(all(Path(row["source"]).is_relative_to(self.python) for row in audit["retained"]))

    def test_unexpected_native_kind_and_missing_source_fail_instead_of_silently_skipping(self):
        entry = self.native("libffi-8.dll")
        with self.assertRaises(ValueError):
            environment.select_binaries([(entry[0], entry[1], "DATA")], [self.python])
        with self.assertRaises(FileNotFoundError):
            environment.select_binaries([("missing.dll", str(self.python / "missing.dll"), "BINARY")], [self.python])

    def test_collected_inventory_and_all_native_hashes_are_verified(self):
        audit = self.collected()
        result = environment.verify_bundled_binaries(self.app, audit)
        self.assertEqual(result["dll_count"], 9)
        self.assertEqual(result["dlls"], sorted(environment.EXPECTED_DLLS))
        self.assertTrue(result["native_origins_verified"])
        self.assertFalse(result["windows_runtime_bundled"])

    def test_changed_collected_dll_and_extension_bytes_are_rejected(self):
        audit = self.collected()
        for name in ("libssl-3.dll", "_ssl.pyd"):
            with self.subTest(name=name):
                path = self.app / "_internal" / name
                original = path.read_bytes()
                path.write_bytes(b"X" * len(original))
                with self.assertRaises(ValueError):
                    environment.verify_bundled_binaries(self.app, audit)
                path.write_bytes(original)

    def test_missing_current_dll_or_returned_compatibility_copy_fails_inventory(self):
        audit = self.collected()
        existing = self.app / "_internal/libffi-8.dll"
        contents = existing.read_bytes()
        existing.unlink()
        with self.assertRaises(ValueError):
            environment.verify_bundled_binaries(self.app, audit)
        existing.write_bytes(contents)
        (self.app / "_internal/ucrtbase.dll").write_bytes(b"must not return")
        with self.assertRaises(ValueError):
            environment.verify_bundled_binaries(self.app, audit)

    def test_empty_audit_cannot_claim_nine_unaudited_dlls_have_verified_origins(self):
        self.collected()
        with self.assertRaises(ValueError):
            environment.verify_bundled_binaries(self.app, {"retained": [], "os_provided": []})

    def test_unlisted_native_extension_fails_even_when_expected_dll_names_match(self):
        audit = self.collected()
        (self.app / "_internal/_unexpected.pyd").write_bytes(b"no approved origin")
        with self.assertRaises(ValueError):
            environment.verify_bundled_binaries(self.app, audit)

    def test_duplicate_audit_destinations_do_not_replace_inventory_coverage(self):
        audit = self.collected()
        invalid = copy.deepcopy(audit)
        invalid["retained"].append(copy.deepcopy(invalid["retained"][0]))
        with self.assertRaises(ValueError):
            environment.verify_bundled_binaries(self.app, invalid)

    def test_generated_spec_handles_space_and_quote_paths_without_executing_them(self):
        source = self.root / "source's files"
        source.mkdir()
        spec = self.root / "build.spec"
        environment.write_spec(spec, source=source, version_file=self.root / "version info.txt",
                               policy_directory=self.root / "build policy", approved_roots=[self.python],
                               audit_path=self.root / "native audit.json")
        text = spec.read_text(encoding="utf-8")
        compile(text, str(spec), "exec")
        self.assertIn("select_binaries(a.binaries", text)
        self.assertIn("contents_directory='_internal'", text)
        self.assertIn("console=False", text)


if __name__ == "__main__":
    unittest.main()
