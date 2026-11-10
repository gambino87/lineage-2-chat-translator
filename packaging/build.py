"""Build a strict-source Windows application and its per-user Inno installer.

Run with .build-tools/venv/Scripts/python.exe packaging/build.py.
The build never launches the GUI or installs the resulting application.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PACKAGING))
from installation_cleanup import CLEANUP_PLAN_NAME
from payload_layout import LICENSE_FILES, copy_licenses, payload_records, retirement_plan, write_installer_file_list
from build_environment import clean_environment, windows_directories, write_spec, verify_bundled_binaries

SOURCES = (
    "installer_main.py", "installer_runtime.py", "app_paths.py", "app_version.py", "app_icon.py",
    "translator_app.py", "translator_core.py", "game_slang.py", "item_catalog.py",
    "game_log_setup.py", "chat_overlay.py", "overlay_tray.py", "reply_composer.py", "uninstall_data.py",
    "app_updater.py", "update_trust.py", "update_controls.py", "installation_cleanup.py",
    "local_service.py", "runtime_integrity.py", "secure_storage.py", "update_network.py",
    "windows_tray.py", "tray_controls.py",
)
ASSETS = ("assets/L2CT.ico", "assets/L2CT.png", "assets/item_catalog.json", "assets/ITEM-CATALOG-NOTICE.txt",
          "assets/apply-update.ps1", "assets/cleanup-program-files.ps1", "assets/protect-install-directory.ps1",
          "assets/secure-ollama-manifest.json")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args: list[str], **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iscc", type=Path, default=ROOT / ".build-tools/inno-portable/tools/ISCC.exe")
    parser.add_argument("--skip-installer", action="store_true")
    options = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Build on Windows x64 with Python 3.12 x64.")
    if sys.maxsize < 2**32:
        raise SystemExit("A 64-bit Python interpreter is required.")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("This build is pinned to Python 3.12 x64.")
    for name in (*SOURCES, *ASSETS):
        if not (ROOT / name).is_file():
            raise SystemExit(f"Required source has not been created: {name}")
    input_hashes = {name: digest(ROOT / name) for name in (*SOURCES, *ASSETS)}
    # This is a generated dependency, not source and not model weights. Its
    # public build recipe and trusted hash are part of the source snapshot.
    from runtime_integrity import verify_bundled_frontend, load_manifest
    protected_frontend = verify_bundled_frontend()
    runtime_inventory = load_manifest()
    backend_sources = PACKAGING / "secure-ollama"
    for name, expected in runtime_inventory.get("patch_inputs", {}).items():
        path = backend_sources / name
        if (not path.resolve().is_relative_to(backend_sources.resolve())
                or not path.is_file() or digest(path) != expected):
            raise SystemExit("Protected backend source changed. Rebuild it and review its inventory before packaging.")
    text = (ROOT / "app_version.py").read_text(encoding="utf-8")
    match = re.search(r'\bVERSION\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)[\'"]', text)
    if not match:
        raise SystemExit("app_version.py must define VERSION as major.minor.patch")
    version = match.group(1)
    if not options.skip_installer:
        published = ROOT / "sites/l2-chat-translator/dist/downloads" / f"Lineage-2-Chat-Translator-{version}-Setup.exe"
        if published.exists():
            raise SystemExit("This version already has a prepared/public installer. Increase app_version.py before rebuilding; use packaging/release_site.py to re-prepare identical bytes.")
        if any((ROOT / "release" / version / name).exists() for name in ("release.json", "release.sig")):
            raise SystemExit("This version already has signed release metadata. Increase app_version.py before rebuilding; signed releases are immutable.")
        from release_site import render_changelog
        try:
            render_changelog(ROOT / "sites/l2-chat-translator", version)
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
    # A failed regression suite must not produce a release or update the homepage.
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    build = ROOT / ".build" / f"{version}-{stamp}"
    build.mkdir(parents=True, exist_ok=False)
    source = build / "source"
    source.mkdir()
    # Nothing else from the workspace is copied or exposed to Analysis.
    for name in input_hashes:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, source / name)
    if any(digest(source / name) != expected or digest(ROOT / name) != expected for name, expected in input_hashes.items()):
        raise SystemExit("Application source changed while staging. Rerun the build for a consistent snapshot.")
    licenses = PACKAGING / "licenses"
    if not all((licenses / name).is_file() for name in LICENSE_FILES):
        raise SystemExit("Run packaging/bootstrap-tools.ps1 to collect required licenses.")
    file_version = version.replace(".", ", ") + ", 0"
    version_file = build / "version-info.txt"
    version_file.write_text(
        "VSVersionInfo(ffi=FixedFileInfo(filevers=(" + file_version + "), prodvers=(" + file_version + "), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)), "
        "kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName','Lineage 2 Chat Translator'), StringStruct('FileDescription','Lineage 2 Chat Translator'), StringStruct('FileVersion','" + version + "'), StringStruct('ProductName','Lineage 2 Chat Translator'), StringStruct('ProductVersion','" + version + "'), StringStruct('OriginalFilename','Lineage2ChatTranslator.exe')])]), VarFileInfo([VarStruct('Translation',[1033,1200])])])",
        encoding="utf-8",
    )
    appdist = build / "dist"
    windows, system = windows_directories()
    build_env = clean_environment(os.environ, windows=windows, system=system,
                                  python_root=Path(sys.base_prefix), interpreter=Path(sys.executable),
                                  cache=build / "pyinstaller-config")
    test_env = clean_environment(os.environ, windows=windows, system=system)
    audit_path = build / "native-dependencies.json"
    spec = build / "Lineage2ChatTranslator.spec"
    write_spec(spec, source=source, version_file=version_file, policy_directory=PACKAGING,
               approved_roots=[Path(sys.base_prefix), Path(sys.prefix), system], audit_path=audit_path)
    run([
        sys.executable, "-I", "-m", "PyInstaller", "--noconfirm",
        "--distpath", str(appdist), "--workpath", str(build / "pyinstaller"), str(spec),
    ], cwd=source, env=build_env)
    app = appdist / "Lineage2ChatTranslator"
    native_summary = verify_bundled_binaries(app, json.loads(audit_path.read_text(encoding="utf-8")))
    backend_directory = app / "_internal" / "secure-runtime"
    backend_directory.mkdir()
    shutil.copy2(protected_frontend, backend_directory / "ollama.exe")
    backend_record = next(row for row in runtime_inventory["files"] if row["path"] == "ollama.exe")
    if digest(backend_directory / "ollama.exe") != backend_record["sha256"]:
        raise SystemExit("Protected backend changed during the build.")
    for name in ("USER-README.txt", "THIRD-PARTY-NOTICES.txt"):
        shutil.copy2(PACKAGING / name, app / name)
    copy_licenses(licenses, app / "licenses")
    # Only retirement metadata is carried forward. Historical binaries and
    # development data never enter this fresh current-version payload.
    cleanup_plan = retirement_plan(payload_records(app, ASSETS), ROOT / "release", version)
    cleanup_path = app / "_internal/assets" / CLEANUP_PLAN_NAME
    cleanup_path.write_text(json.dumps(cleanup_plan, indent=2) + "\n", encoding="utf-8")
    frozen_files = payload_records(app, ASSETS)
    file_list = build / "installer-files.iss"
    write_installer_file_list(file_list, frozen_files)
    # Frozen windowed builds have no stdout. The app writes this explicit report.
    selftest = build / "self-test.json"
    neutral = build / "empty-working-directory"
    neutral.mkdir()
    run([str(app / "Lineage2ChatTranslator.exe"), "--self-test", "--output", str(selftest)],
        cwd=neutral, env=test_env, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not selftest.is_file():
        raise SystemExit("Packaged headless self-test did not write a report.")
    report = json.loads(selftest.read_text(encoding="utf-8"))
    if report.get("ok") is not True or report.get("frozen") is not True or report.get("version") != version:
        raise SystemExit(f"Packaged self-test failed: {report}")
    if any(digest(ROOT / name) != expected for name, expected in input_hashes.items()):
        raise SystemExit("Application source changed during the frozen build. Rerun before compiling the installer.")
    release = ROOT / "release" / version
    release.mkdir(parents=True, exist_ok=True)
    if payload_records(app, ASSETS) != frozen_files:
        raise SystemExit("Frozen payload changed during validation.")
    # Publish outcomes without machine-specific preference/app paths.
    public_report = {key: value for key, value in report.items() if not key.endswith("_path")}
    from release_site import read_model_catalog
    manifest = {
        "product": "Lineage 2 Chat Translator", "version": version,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": "windows-x64", "authenticode_signed": False,
        "toolchain": {"python": sys.version.split()[0], "pyinstaller": package_version("pyinstaller"), "inno_setup": "6.7.3"},
        "default_model": read_model_catalog(source)["DEFAULT_MODEL"], "models_bundled": False,
        "runtime_bundled": True,
        "bundled_runtime_components": ["authenticated_frontend"],
        "runtime_libraries_bundled": False,
        "sources": [{"path": name, "sha256": input_hashes[name]} for name in SOURCES],
        "assets": [{"path": name, "sha256": input_hashes[name]} for name in ASSETS],
        "payload": frozen_files,
        "native_dependencies": native_summary,
        "obsolete_file_cleanup": {"plan": "_internal/assets/" + CLEANUP_PLAN_NAME,
                                  "obsolete_paths": len(cleanup_plan["obsolete_files"]),
                                  "hash_verified_only": True},
        "packaged_self_test": public_report,
    }
    if not options.skip_installer:
        compiler = options.iscc.resolve()
        if not compiler.is_file():
            raise SystemExit(f"Inno compiler missing: {compiler}. Run bootstrap-tools.ps1.")
        run([str(compiler), "/Qp", f"/DAppVersion={version}", f"/DAppSource={app}",
             f"/DAppIcon={source / 'assets/L2CT.ico'}", f"/DAppFileList={file_list}",
             f"/DReleaseDir={release}", str(PACKAGING / "installer.iss")], cwd=PACKAGING)
        installer = release / f"Lineage-2-Chat-Translator-{version}-Setup.exe"
        if not installer.is_file():
            raise SystemExit("Compiler did not produce the expected installer.")
        if any(digest(ROOT / name) != expected for name, expected in input_hashes.items()):
            raise SystemExit("Application source changed during installer compilation. Rerun the release build.")
        if payload_records(app, ASSETS) != frozen_files:
            raise SystemExit("Frozen payload changed during installer compilation.")
        manifest["installer"] = {"file": installer.name, "bytes": installer.stat().st_size, "sha256": digest(installer)}
        (release / "SHA256SUMS.txt").write_text(f"{digest(installer)}  {installer.name}\n", encoding="ascii")
        template = (PACKAGING / "download-page.html").read_text(encoding="utf-8")
        for key, value in {"VERSION": version, "INSTALLER": installer.name, "SIZE": f"{installer.stat().st_size / 1_000_000:.1f}", "SHA256": digest(installer)}.items():
            template = template.replace("{{" + key + "}}", html.escape(value))
        (release / "index.html").write_text(template, encoding="utf-8")
    (release / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (release / "packaged-self-test.json").write_text(json.dumps(public_report, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(PACKAGING / "HOSTING.md", release / "HOSTING.md")
    site = None
    if not options.skip_installer:
        from release_site import prepare_site
        site = prepare_site(ROOT, ROOT / "sites/l2-chat-translator", SOURCES, ASSETS)
    if not options.skip_installer:
        bundle_base = ROOT / "release" / f"Lineage-2-Chat-Translator-{version}-hosting"
        bundle = Path(shutil.make_archive(str(bundle_base), "zip", root_dir=release))
        bundle.with_suffix(".zip.sha256").write_text(f"{digest(bundle)}  {bundle.name}\n", encoding="ascii")
    (ROOT / ".build/latest-build.json").write_text(json.dumps({"build": str(build), "app": str(app), "release": str(release)}, indent=2), encoding="utf-8")
    print(json.dumps({"app": str(app), "release": str(release), "self_test": report, "homepage": site,
                      "publication": "Prepared locally; publish installer and homepage together on the main server using packaging/HOSTING.md."}, indent=2))


if __name__ == "__main__":
    from release_lock import release_lock
    with release_lock(ROOT):
        main()
