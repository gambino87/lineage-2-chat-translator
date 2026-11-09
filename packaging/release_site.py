"""Prepare one verified installer/homepage release. Never publishes or runs apps."""
from __future__ import annotations

import argparse
import ast
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "sites/l2-chat-translator"
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+"
INSTALLER_PATTERN = rf"Lineage-2-Chat-Translator-({VERSION_PATTERN})-Setup\.exe"
SIGNATURE_BYTES = 384  # RSA-3072 detached signature; cryptographic validation is shared with the app.
MAX_METADATA_BYTES = 64 * 1024


def verify_signature(metadata: bytes, signature: bytes) -> None:
    from update_trust import verify_release_signature
    verify_release_signature(metadata, signature)


def sign_metadata(metadata: bytes) -> bytes:
    # Resolve the publisher-only helper by file, not the unrelated packaging package.
    spec = importlib.util.spec_from_file_location(
        "l2_release_signer", Path(__file__).with_name("sign_release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sign_metadata(metadata)


def signed_metadata(root: Path, version: str, public: dict, *, check: bool) -> tuple[bytes, bytes]:
    """Keep each release's exact signed bytes immutable, including during page-only updates."""
    metadata = (json.dumps(public, indent=2) + "\n").encode("utf-8")
    require(len(metadata) <= MAX_METADATA_BYTES, "Release metadata is too large to sign.")
    folder = root / "release" / version
    for path in (root / "release", folder):
        require(not path.is_symlink() and not path.is_junction()
                and path.resolve().is_relative_to(root.resolve()), "Linked signing output is not allowed.")
    document, signature_file = folder / "release.json", folder / "release.sig"
    for path in (document, signature_file):
        require(not path.is_symlink() and not path.is_junction(), "Linked signing output is not allowed.")
    if document.exists() or signature_file.exists():
        require(document.is_file() and signature_file.is_file(),
                "Incomplete signed release. Do not replace it; use a new release version.")
        require(document.stat().st_size <= MAX_METADATA_BYTES
                and signature_file.stat().st_size == SIGNATURE_BYTES,
                "Signed release size is invalid.")
        require(document.read_bytes() == metadata,
                "Signed release metadata changed. Increase the application version before signing again.")
        signature = signature_file.read_bytes()
        verify_signature(metadata, signature)
        return metadata, signature
    require(not check, "Signed release is missing. Prepare and sign it before checking or exporting.")
    signature = sign_metadata(metadata)
    require(isinstance(signature, bytes) and len(signature) == SIGNATURE_BYTES,
            "The release signer returned an invalid signature.")
    verify_signature(metadata, signature)
    # Exclusive creation never silently replaces a previous signed release. A partial
    # pair after interruption fails closed on the next run rather than being re-signed.
    with document.open("xb") as output:
        output.write(metadata)
    with signature_file.open("xb") as output:
        output.write(signature)
    return metadata, signature


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def current_version(root: Path) -> str:
    match = re.search(rf"\bVERSION\s*=\s*['\"]({VERSION_PATTERN})['\"]",
                      (root / "app_version.py").read_text(encoding="utf-8"))
    require(match is not None, "app_version.py must define major.minor.patch VERSION.")
    return match.group(1)


def render_changelog(site: Path, version: str) -> str:
    """Render plain-text release notes, requiring an entry for the installer."""
    entries = json.loads((site / "changelog.json").read_text(encoding="utf-8"))
    require(isinstance(entries, list) and bool(entries), "Changelog must contain release entries.")
    versions = []
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {"version", "changes"},
                "Each changelog entry needs only version and changes.")
        tag = entry["version"]
        require(isinstance(tag, str) and re.fullmatch(VERSION_PATTERN, tag) is not None,
                "Invalid changelog version.")
        changes = entry["changes"]
        require(isinstance(changes, list) and bool(changes)
                and all(isinstance(item, str) and bool(item.strip()) for item in changes),
                "Changelog changes must be nonempty plain-text notes.")
        versions.append(tuple(map(int, tag.split("."))))
    require(len(set(versions)) == len(versions) and versions == sorted(versions, reverse=True),
            "Changelog versions must be unique and newest first.")
    require(entries[0]["version"] == version,
            f"Add changelog notes for version {version} before preparing its release.")

    articles = []
    for index, entry in enumerate(entries):
        tag = entry["version"]
        current = ' <span class="current-release">Current</span>' if index == 0 else ""
        notes = "".join(f"<li>{html.escape(item.strip(), quote=True)}</li>" for item in entry["changes"])
        articles.append(f'<article class="release-entry" aria-labelledby="release-{tag}">'
                        f'<h3 id="release-{tag}">v{tag}{current}</h3><ul>{notes}</ul></article>')
    visible = "\n".join(articles[:2])
    if len(articles) > 2:
        visible += ('\n<details class="earlier-releases"><summary>Earlier versions</summary>\n'
                    + "\n".join(articles[2:]) + "\n</details>")
    return visible


def read_model_catalog(root: Path) -> dict:
    # Read only literal catalog data; importing the app or starting Ollama is unnecessary.
    tree = ast.parse((root / "installer_runtime.py").read_text(encoding="utf-8-sig"))
    values = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            name = getattr(statement.targets[0], "id", "")
            if name == "DEFAULT_MODEL":
                values[name] = ast.literal_eval(statement.value)
            elif name == "MODEL_CATALOG":
                for node in ast.walk(statement.value):
                    if isinstance(node, ast.Dict):
                        node.keys = [ast.Constant(values["DEFAULT_MODEL"])
                                     if isinstance(key, ast.Name) and key.id == "DEFAULT_MODEL"
                                     else key for key in node.keys]
                values[name] = ast.literal_eval(statement.value)
    return values


def model_details(root: Path, tag: str) -> dict:
    values = read_model_catalog(root)
    require(values.get("DEFAULT_MODEL") == tag, "Manifest model differs from the current application.")
    model = values["MODEL_CATALOG"][tag]
    return {"name": model["label"].split(" — ")[0], "download_gb": model["download_gb"],
            "tested": model["tested"]}


def verified_release(root: Path, version: str, sources: tuple, assets: tuple) -> tuple[dict, Path]:
    require(re.fullmatch(VERSION_PATTERN, version) is not None, "Invalid release version.")
    require(version == current_version(root), "Release version differs from the current application.")
    release = root / "release" / version
    manifest = read_json(release / "release-manifest.json")
    require(manifest.get("version") == version, "Release manifest version mismatch.")
    self_test = manifest.get("packaged_self_test", {})
    require(self_test.get("ok") is True and self_test.get("frozen") is True
            and self_test.get("version") == version, "Packaged self-test failed or does not match this release.")
    for key, expected in (("sources", sources), ("assets", assets)):
        records = manifest.get(key, [])
        require(len(records) == len(expected) and {row["path"] for row in records} == set(expected),
                f"Manifest {key} must match the build allowlist.")
        for row in records:
            path = root / row["path"]
            require(path.resolve().is_relative_to(root.resolve()) and not path.is_symlink(),
                    "Source must remain inside the project.")
            require(path.is_file() and digest(path) == row["sha256"],
                    f"Stale installer: {row['path']} changed. Build a new version first.")
    artifact = manifest.get("installer", {})
    name = f"Lineage-2-Chat-Translator-{version}-Setup.exe"
    require(artifact.get("file") == name, "Unexpected installer filename.")
    installer = release / name
    require(installer.is_file() and not installer.is_symlink(), "Installer missing or linked.")
    require(installer.stat().st_size == artifact.get("bytes") and digest(installer) == artifact.get("sha256"),
            "Installer size/checksum differs from its verified manifest.")
    require((release / "SHA256SUMS.txt").read_text(encoding="ascii").strip()
            == f"{artifact['sha256']}  {name}", "Release checksum file mismatch.")
    require(manifest.get("product") == "Lineage 2 Chat Translator"
            and manifest.get("platform") == "windows-x64", "Unexpected product/platform.")
    require(manifest.get("models_bundled") is False and
            (manifest.get("runtime_bundled") is False or
             (manifest.get("runtime_bundled") is True
              and manifest.get("bundled_runtime_components") == ["authenticated_frontend"]
              and manifest.get("runtime_libraries_bundled") is False)),
            "Bundled runtime/model claims need homepage review.")
    require(type(manifest.get("authenticode_signed")) is bool, "Signing status is missing.")
    public = {key: manifest[key] for key in (
        "product", "version", "built_at_utc", "platform", "authenticode_signed",
        "default_model", "models_bundled", "runtime_bundled")}
    public["installer"] = {key: artifact[key] for key in ("file", "bytes", "sha256")}
    if manifest.get("runtime_bundled"):
        public["bundled_runtime_components"] = ["authenticated_frontend"]
        public["runtime_libraries_bundled"] = False
    public["validation"] = {"packaged_self_test_passed": True}
    public["model"] = model_details(root, manifest["default_model"])
    return public, installer


def prepare_site(root: Path, site: Path, sources: tuple, assets: tuple, *, check: bool = False) -> dict:
    """Validate first, stage a complete allowlisted tree, then replace local dist."""
    root, site = root.resolve(), site.resolve()
    require(site.is_relative_to(root), "Site must be inside the project.")
    version = current_version(root)
    public, installer = verified_release(root, version, sources, assets)
    changelog = render_changelog(site, version)
    hosting = read_json(site / ".openai/hosting.json")
    require(bool(hosting.get("project_id")) and hosting.get("static", {}).get("directory") == "dist",
            "Existing Site identity and static dist configuration are required.")
    dist = site / "dist"
    require(not dist.is_symlink() and not dist.is_junction() and dist.resolve().is_relative_to(site), "Invalid public output path.")
    history = {}
    fixed = {"index.html", "_headers", "robots.txt", "assets/L2CT.ico", "assets/L2CT.png",
             "downloads/SHA256SUMS.txt", "downloads/release.json", "downloads/release.sig"}
    for path in dist.rglob("*"):
        require(not path.is_symlink() and not path.is_junction(), "Links are not allowed in public output.")
        if not path.is_file():
            continue
        relative = path.relative_to(dist).as_posix()
        match = re.fullmatch(rf"downloads/({INSTALLER_PATTERN})", relative)
        require(relative in fixed or match is not None, f"Unexpected public file: {relative}")
        if match:
            # Verify retained downloads against their original local release manifests.
            old = read_json(root / "release" / match.group(2) / "release-manifest.json")["installer"]
            require(old["file"] == path.name and old["bytes"] == path.stat().st_size
                    and old["sha256"] == digest(path), "Retained installer differs from its release manifest.")
            history[path.name] = path
    if installer.name in history:
        require(digest(history[installer.name]) == public["installer"]["sha256"],
                "This version already has different installer bytes. Increase the application version.")
    history[installer.name] = installer
    model = public["model"]
    tokens = {
        "VERSION": version, "INSTALLER": installer.name,
        "SIZE_MB": f"{public['installer']['bytes'] / 1_000_000:.1f}",
        "CHANGELOG": changelog, "MODEL_NAME": model["name"],
        "MODEL_DOWNLOAD_GB": f"{model['download_gb']:.1f}",
        "MODEL_STATUS": "the current tested choice" if model["tested"] else "an experimental option",
        "SIGNING_NOTICE": "This build is signed. Windows may still show a security prompt."
            if public["authenticode_signed"] else "This build is unsigned, so Windows may show an unknown-publisher prompt.",
    }
    page = (site / "index.template.html").read_text(encoding="utf-8")
    require(set(re.findall(r"{{([A-Z0-9_]+)}}", page)) == set(tokens), "Homepage template release tokens do not match.")
    for key, value in tokens.items():
        # Only our renderer produces markup; its release-note text is escaped above.
        page = page.replace("{{" + key + "}}", value if key == "CHANGELOG" else html.escape(value, quote=True))
    require("{{" not in page, "Unresolved homepage template token.")
    links = re.findall(r'href="(/downloads/[^"?#]+\.exe)"', page)
    require(bool(links) and set(links) == {"/downloads/" + installer.name}, "Homepage download links do not match.")
    headers = "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n"
    for name in sorted(history):
        headers += (f"/downloads/{name}\n  Content-Type: application/octet-stream\n"
                    f'  Content-Disposition: attachment; filename="{name}"\n'
                    "  Cache-Control: public, max-age=31536000, immutable\n")
    for name, content_type in (("SHA256SUMS.txt", "text/plain"), ("release.json", "application/json")):
        headers += f"/downloads/{name}\n  Content-Type: {content_type}; charset=utf-8\n  Cache-Control: no-cache\n"
    headers += "/downloads/release.sig\n  Content-Type: application/octet-stream\n  Cache-Control: no-cache\n"
    metadata, signature = signed_metadata(root, version, public, check=check)
    # Staging and backups stay under a checked, ignored directory in this Site.
    scratch = site / ".sites-artifacts"
    require(not scratch.is_symlink() and not scratch.is_junction() and scratch.resolve().is_relative_to(site), "Invalid staging directory.")
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=scratch) as temp:
        temp_path = Path(temp).resolve()
        require(temp_path.is_relative_to(scratch.resolve()), "Unsafe temporary cleanup path.")
        staged = temp_path / "dist"
        (staged / "downloads").mkdir(parents=True)
        (staged / "assets").mkdir()
        (staged / "index.html").write_text(page, encoding="utf-8")
        (staged / "_headers").write_text(headers, encoding="utf-8")
        (staged / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="ascii")
        for name in ("L2CT.ico", "L2CT.png"):
            shutil.copyfile(root / "assets" / name, staged / "assets" / name)
            require(digest(staged / "assets" / name) == digest(root / "assets" / name), "Icon changed while staging.")
        for name, source in history.items():
            shutil.copyfile(source, staged / "downloads" / name)
            require(digest(staged / "downloads" / name) == digest(source), "Installer changed while staging.")
        checksum_text = "".join(f"{digest(staged / 'downloads' / name)}  {name}\n" for name in sorted(history))
        (staged / "downloads/SHA256SUMS.txt").write_text(checksum_text, encoding="ascii")
        # Writing bytes avoids newline conversion after signing on Windows.
        (staged / "downloads/release.json").write_bytes(metadata)
        (staged / "downloads/release.sig").write_bytes(signature)
        # Catch source/artifact edits made while assembling the release.
        require(verified_release(root, version, sources, assets)[0] == public, "Release changed during preparation.")
        require(signed_metadata(root, version, public, check=True) == (metadata, signature),
                "Signed release changed during preparation.")
        require(digest(staged / "downloads" / installer.name) == public["installer"]["sha256"],
                "Staged installer checksum mismatch.")
        if check:
            for path in staged.rglob("*"):
                if path.is_file():
                    actual = dist / path.relative_to(staged)
                    require(actual.is_file() and digest(actual) == digest(path),
                            f"Site needs preparation: {path.relative_to(staged)}")
        else:
            # Outside the temporary tree so failed Windows rollback cannot erase it.
            backup = scratch / (temp_path.name + "-previous")
            if dist.exists():
                dist.rename(backup)
            try:
                staged.rename(dist)
            except OSError:
                if backup.exists():
                    try:
                        backup.rename(dist)
                    except OSError as exc:
                        raise OSError(f"Cannot restore dist; previous files are retained at {backup}") from exc
                raise
            if backup.exists():
                require(backup.resolve().is_relative_to(scratch.resolve())
                        and not backup.is_symlink() and not backup.is_junction(), "Unsafe backup cleanup path.")
                shutil.rmtree(backup)
    return {"version": version, "installer": public["installer"], "project_id": hosting["project_id"],
            "retained_installers": len(history), "site": str(site), "checked_only": check,
            "published": False}


def main() -> None:
    from build import ASSETS, SOURCES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify prepared site without changing it.")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_site(ROOT, SITE, SOURCES, ASSETS, check=args.check), indent=2))
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
