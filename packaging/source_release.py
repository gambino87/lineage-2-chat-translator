"""Publish an allowlisted, deidentified source snapshot before a public release.

Uses the normal Git credential helper. It never copies the workspace wholesale,
force-pushes, rewrites a version tag, or prints credential-helper/server errors.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/gambino87/lineage-2-chat-translator"
REMOTE = REPOSITORY + ".git"
CLONE = "public-source/lineage-2-chat-translator"
CONFIG = "packaging/public-source/allowlist.json"
STATE = ".build/source-publication.local.json"
MANIFEST = "SOURCE-MANIFEST.json"
TEMPLATES = {"README.md": "README.template.md", "LICENSE-NOTICE.md": "LICENSE-NOTICE.md",
             ".gitignore": "gitignore.txt", ".gitattributes": "gitattributes.txt"}
VERSION = r"(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})"
MAX_SOURCE_BYTES = 32 * 1024 * 1024
FORBIDDEN_PARTS = {".git", ".build", ".build-tools", "release", "dist", "review_tools",
                   "logs", "captures", "models", "__pycache__", "venv", ".venv"}
TEXT_SUFFIXES = {".py", ".go", ".ps1", ".cmd", ".iss", ".isl", ".txt", ".md", ".html", ".conf", ".sh", ".json"}


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def relative_name(name: str) -> str:
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name
            and not re.search(r"[\x00-\x1f\x7f]", name), "Unsafe source path.")
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(part not in ("", ".", "..") for part in name.split("/")),
            "Unsafe source path.")
    require(not any(part.casefold() in FORBIDDEN_PARTS for part in path.parts),
            "Private/generated directory is not a source input.")
    require(path.name.casefold() not in {"settings.json", "runtime.json", "agents.md"}
            and not path.name.casefold().endswith((".local.json", ".log", ".bak", ".old")),
            "Private/local file is not a source input.")
    require(path.suffix.lower() in TEXT_SUFFIXES or name in TEMPLATES
            or name in {"assets/L2CT.png", "assets/L2CT.ico"}, "Unsupported public source file type.")
    return name


def safe_path(root: Path, name: str, *, file: bool = True) -> Path:
    """Reject links in every component, including ancestor junctions on Windows."""
    path = root
    for part in PurePosixPath(name).parts:
        require(part not in ("", ".", "..") and ":" not in part and "\\" not in part,
                "Unsafe publication path.")
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode)
                    and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "Linked publication paths are not allowed.")
    require(path.resolve().is_relative_to(root.resolve()), "Publication path escapes its root.")
    if file:
        require(path.is_file() and path.stat().st_size <= MAX_SOURCE_BYTES,
                "Required source file is missing or too large: " + name)
    return path


def read_source(root: Path, name: str) -> bytes:
    return safe_path(root, relative_name(name)).read_bytes()


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid source publication metadata.") from exc


def _build_inputs(root: Path) -> dict:
    tree = ast.parse(read_source(root, "packaging/build.py"))
    result = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            name = getattr(statement.targets[0], "id", "")
            if name in {"SOURCES", "ASSETS"}:
                values = ast.literal_eval(statement.value)
                require(isinstance(values, tuple) and values and len(values) == len(set(values)),
                        "Build source allowlist is invalid.")
                result[name.lower()] = [relative_name(value) for value in values]
    require(set(result) == {"sources", "assets"}, "Build source allowlists are missing.")
    return result


def verified_release(root: Path) -> dict:
    """Bind the public snapshot to the actual installer, not an arbitrary source tree."""
    match = re.search(rf"\bVERSION\s*=\s*['\"]({VERSION})['\"]", read_source(root, "app_version.py").decode())
    require(match is not None, "Missing application version.")
    version = match[1]
    folder = f"release/{version}/"
    manifest = read_json_file(safe_path(root, folder + "release-manifest.json"))
    require(manifest.get("product") == "Lineage 2 Chat Translator"
            and manifest.get("platform") == "windows-x64" and manifest.get("version") == version,
            "Release identity does not match the current application.")
    report = manifest.get("packaged_self_test", {})
    require(report.get("ok") is True and report.get("frozen") is True and report.get("version") == version,
            "The release has no matching successful packaged self-test.")
    for key, paths in _build_inputs(root).items():
        rows = manifest.get(key, [])
        require(len(rows) == len(paths) and {row.get("path") for row in rows} == set(paths),
                "Release sources do not match the build allowlist.")
        for row in rows:
            require(set(row) == {"path", "sha256"}
                    and re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", "")) is not None
                    and sha256(read_source(root, row["path"])) == row["sha256"],
                    "Application input changed; build a new version before publishing source.")
    artifact = manifest.get("installer", {})
    name = f"Lineage-2-Chat-Translator-{version}-Setup.exe"
    require(set(artifact) == {"file", "bytes", "sha256"} and artifact.get("file") == name,
            "Release installer name mismatch.")
    installer = safe_path(root, folder + name)
    require(installer.stat().st_size == artifact.get("bytes")
            and sha256(installer.read_bytes()) == artifact.get("sha256"), "Release installer checksum mismatch.")
    require(safe_path(root, folder + "SHA256SUMS.txt").read_text(encoding="ascii").strip()
            == f"{artifact['sha256']}  {name}", "Release checksum file mismatch.")
    return manifest


def deidentify(name: str, content: bytes, version: str) -> bytes:
    """Replace known identifying regression fixtures; never alter compiled app inputs."""
    if name.startswith("tests/"):
        for old, new in ((b"J" + b"OACO", b"ExamplePlayer"), (b"Bu" + b"lus", b"ExampleTrader"),
                         (b"hd" + b"lavitz", b"examplechannel")):
            content = content.replace(old, new)
    if name == "packaging/USER-README.txt":
        content = content.replace(b"Time" + b"ToDie", b"ExamplePlayer")
        content = re.sub(rb"\ALINEAGE 2 CHAT TRANSLATOR [0-9]+\.[0-9]+\.[0-9]+",
                         b"LINEAGE 2 CHAT TRANSLATOR " + version.encode(), content)
    if name == "packaging/HOSTING.md":
        content = re.sub(rb"C:\\Users\\[^\\`\r\n]+\\\.ssh\\[^`\r\n]+",
                         b"<path-to-your-private-SSH-key>", content)
    return content


def scan_content(name: str, content: bytes) -> None:
    if name in {"assets/L2CT.png", "assets/L2CT.ico"}:
        return
    scanned = content
    if name == "tests/test_main_server_export.py":
        # This exact literal deliberately tests rejection of credential-bearing URLs.
        scanned = scanned.replace(b"https://user" + b":pass@host", b"synthetic-credential-url")
    patterns = [rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                rb"github_pat_[A-Za-z0-9_]{20,}", rb"gh[pousr]_[A-Za-z0-9]{30,}",
                rb"[MN][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}",
                rb"(?i)(?:https?|ssh)://[^\s/:]+:[^\s/@]+@",
                rb"(?i)C:[\\/]+Users[\\/]+" + re.escape(os.environ.get("USERNAME", "").encode()) + rb"[\\/]"
                if os.environ.get("USERNAME") else rb"(?!)"]
    patterns.extend(re.escape(value) for value in (b"J" + b"OACO", b"Bu" + b"lus", b"hd" + b"lavitz", b"Time" + b"ToDie"))
    require(not any(re.search(pattern, scanned) for pattern in patterns),
            "Source privacy review required for " + name + "; matched content is not logged.")
    try:
        content.decode("utf-8-sig")
        if name.endswith(".py"):
            ast.parse(content, filename=name)
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError("Invalid public text/source: " + name) from exc


def snapshot(root: Path) -> tuple[dict[str, bytes], dict]:
    """Return a complete in-memory candidate before touching the public clone."""
    release = verified_release(root)
    inputs = release["sources"] + release["assets"]
    support = json.loads(read_source(root, CONFIG).decode("utf-8"))
    require(isinstance(support, list) and len(support) == len(set(support)), "Invalid support source allowlist.")
    names = {relative_name(name) for name in support} | {row["path"] for row in inputs}
    require(CONFIG in names and not names.intersection({*TEMPLATES, MANIFEST}), "Invalid generated source input.")
    # New build helpers/tests require an explicit privacy review; never silently
    # omit them from the public source or auto-add arbitrary workspace files.
    for folder in ("packaging", "tests", "tools"):
        for directory, children, filenames in os.walk(root / folder, followlinks=False):
            children[:] = [name for name in children if name.casefold() not in FORBIDDEN_PARTS]
            for child in children:
                safe_path(root, (Path(directory) / child).relative_to(root).as_posix(), file=False)
            for filename in filenames:
                path = Path(directory) / filename
                if path.suffix.lower() not in TEXT_SUFFIXES or filename.casefold().endswith((".local.json", ".bak", ".old")):
                    continue
                if filename.casefold() in {"settings.json", "runtime.json", "agents.md"}:
                    continue
                relative = path.relative_to(root).as_posix()
                require(relative in names, "New source file requires public allowlist review: " + relative)
    files, adapted = {}, []
    for name in sorted(names):
        original = read_source(root, name)
        content = deidentify(name, original, release["version"])
        if original != content:
            adapted.append(name)
        files[name] = content
    for name, template in TEMPLATES.items():
        content = read_source(root, "packaging/public-source/" + template)
        content = content.replace(b"{{VERSION}}", release["version"].encode())
        files[name] = content
    for name, content in files.items():
        scan_content(name, content)
    for row in inputs:
        require(sha256(files[row["path"]]) == row["sha256"], "Publication must not alter an application input.")
    toolchain = release.get("toolchain", {})
    require(set(toolchain).issubset({"python", "pyinstaller", "inno_setup"})
            and all(isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,50}", value)
                    for value in toolchain.values()), "Unexpected toolchain metadata.")
    document = {
        "schema_version": 1, "product": release["product"], "version": release["version"],
        "purpose": "Source published for transparency; not a claim of byte-for-byte binary reproducibility.",
        "published_installer": {**release["installer"], "url": "https://40.160.140.227/downloads/" + release["installer"]["file"]},
        "release_toolchain": toolchain, "application_sources": release["sources"],
        "application_assets": release["assets"], "publication_adapted_files": adapted,
        "publication_adaptations": "Deidentify known player/example aliases, a streamer path and operator SSH-key location; normalize the guide's version heading. Compiled application inputs remain unchanged.",
        "validation": {"release_packaged_self_test_passed": True},
        "files": [{"path": name, "sha256": sha256(content)} for name, content in sorted(files.items())],
    }
    files[MANIFEST] = (json.dumps(document, indent=2) + "\n").encode()
    scan_content(MANIFEST, files[MANIFEST])
    return files, document


def _git(repo: Path, *args: str, allow_failure: bool = False) -> str | None:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    for name in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(name, None)
    try:
        result = subprocess.run(["git", "-c", "credential.interactive=never", "-c", "commit.gpgsign=false",
                                 "-c", "tag.gpgSign=false", "-c", "core.hooksPath=.git/source-release-no-hooks",
                                 "-c", "user.name=gambino87", "-c", "user.email=39010252+gambino87@users.noreply.github.com",
                                 *args], cwd=repo, env=env, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Source Git operation failed; no credentials or command output were logged.") from exc
    if result.returncode:
        if allow_failure:
            return None
        raise ValueError("Source Git operation failed. Check the existing GitHub sign-in and retry; no force push was attempted.")
    return result.stdout.decode("utf-8").rstrip("\r\n")


def release_identity(document: dict) -> dict:
    """Tags pin app/asset/installer identity while main may receive tooling-only changes."""
    try:
        return {"version": document["version"], "sources": document["application_sources"],
                "assets": document["application_assets"], "installer": document["published_installer"]}
    except KeyError as exc:
        raise ValueError("Existing source tag lacks release identity.") from exc


def snapshot_digest(files: dict[str, bytes]) -> str:
    inventory = {name: sha256(content) for name, content in sorted(files.items())}
    return sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode())


def verify_source_current(root: Path, receipt: dict) -> None:
    """Local end-of-export guard; no Git mutation or additional network requests."""
    files, document = snapshot(Path(root).resolve())
    installer = {key: document["published_installer"][key] for key in ("file", "bytes", "sha256")}
    require(receipt.get("public_verified") is True and receipt.get("repository") == REPOSITORY
            and receipt.get("version") == document["version"] and receipt.get("installer") == installer
            and receipt.get("snapshot_sha256") == snapshot_digest(files),
            "Published source no longer matches this release. Retry publication before deployment.")


def _tracked_files(repo: Path) -> dict[str, bytes]:
    rows = _git(repo, "ls-files", "--stage", "-z").strip("\0").split("\0")
    files = {}
    for row in rows:
        require(bool(row), "Public clone has no tracked source.")
        metadata, name = row.split("\t", 1)
        mode, blob, stage = metadata.split()
        require(mode == "100644" and stage == "0", "Linked/unmerged public source is not allowed.")
        content = read_source(repo, relative_name(name))
        expected_blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        require(blob == expected_blob, "Git index bytes differ from the audited source files.")
        files[name] = content
    document = json.loads(files[MANIFEST])
    inventory = {row["path"]: row["sha256"] for row in document["files"]}
    require(set(inventory) | {MANIFEST} == set(files) and len(inventory) == len(document["files"]),
            "Unexpected tracked file in the public clone; review before publishing.")
    require(all(sha256(files[name]) == expected for name, expected in inventory.items()),
            "Public snapshot inventory differs from its tracked files.")
    return files


def _state(root: Path, value: dict | None = None) -> dict:
    path = safe_path(root, STATE, file=False)
    if value is None:
        return read_json_file(path) if path.is_file() else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_path(root, STATE + ".tmp", file=False)
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return value


def _verify_public(commit: str, files: dict[str, bytes]) -> None:
    """Verify unauthenticated GitHub access and every published Git blob."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        url = "https://api.github.com/repos/gambino87/lineage-2-chat-translator"
        def get(path):
            request = urllib.request.Request(url + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "L2CT-source-release"})
            with opener.open(request, timeout=30) as response:
                return json.loads(response.read(4 * 1024 * 1024))
        repository = get("")
        require(repository.get("private") is False, "Source repository is not public.")
        published = get("/commits/main")
        require(published.get("sha") == commit, "Public source branch differs from the release commit.")
        tree = get(f"/git/trees/{commit}?recursive=1")
        require(not tree.get("truncated"), "Public source tree verification was incomplete.")
        actual = {row["path"]: row["sha"] for row in tree["tree"] if row["type"] == "blob"}
        expected = {name: hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                    for name, content in files.items()}
        require(actual == expected, "Public source files do not match the verified release snapshot.")
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        raise ValueError("Source may have pushed, but public verification failed. Retry publication before releasing the website.") from exc


def publish_source(root: Path = ROOT) -> dict:
    root = Path(root).resolve()
    files, document = snapshot(root)
    repo = safe_path(root, CLONE, file=False)
    require(repo.is_dir(), "Source clone is missing. Clone the public GitHub repository into " + CLONE + " before publishing.")
    require(safe_path(repo, ".git", file=False).is_dir(), "Expected an independent, unlinked source Git repository.")
    require(_git(repo, "rev-parse", "--show-toplevel") == str(repo).replace("\\", "/"), "Unexpected source Git root.")
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    require((common if common.is_absolute() else repo / common).resolve() == (repo / ".git").resolve(),
            "Shared Git metadata is not allowed for source publication.")
    require(_git(repo, "remote", "get-url", "--all", "origin") == REMOTE
            and _git(repo, "remote", "get-url", "--push", "--all", "origin") == REMOTE, "Unexpected source Git remote.")
    require(_git(repo, "branch", "--show-current") == "main", "Source publication requires the main branch.")
    require(not _git(repo, "status", "--porcelain", "--untracked-files=all"),
            "Public source clone has local changes. Review them before release publication.")
    previous_files = _tracked_files(repo)
    _git(repo, "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
    remote_head = _git(repo, "rev-parse", "refs/remotes/origin/main")
    head = _git(repo, "rev-parse", "HEAD")
    previous_state = _state(root)
    if head != remote_head:
        require(previous_state.get("pending_commit") == head and previous_state.get("remote_base") == remote_head
                and previous_state.get("repository") == REPOSITORY
                and _git(repo, "rev-parse", "HEAD^") == remote_head
                and previous_files == files,
                "Source history differs from the published branch. Review it before publishing; unrelated commits are never pushed.")
    version = document["version"]
    tag = "v" + version
    remote_tag = _git(repo, "ls-remote", "origin", "refs/tags/" + tag)
    if remote_tag:
        _git(repo, "fetch", "--no-tags", "origin", "refs/tags/" + tag + ":refs/tags/" + tag)
    tag_commit = _git(repo, "rev-parse", "--verify", "refs/tags/" + tag + "^{commit}", allow_failure=True)
    if tag_commit:
        prior = _git(repo, "show", tag_commit + ":" + MANIFEST)
        require(release_identity(json.loads(prior)) == release_identity(document),
                "Existing version tag has different application/installer inputs. Increase the version; tags are immutable.")
        if not remote_tag:
            require(tag_commit == head and previous_files == files,
                    "Unpublished local version tag is not the exact audited snapshot. Review it before publishing.")
            require(previous_state.get("pending_commit") == head
                    and previous_state.get("pending_tag") == tag
                    and previous_state.get("pending_tag_oid") == _git(repo, "rev-parse", "refs/tags/" + tag)
                    and previous_state.get("repository") == REPOSITORY,
                    "Unpublished version tag is not a recorded publication retry. Review it before publishing.")
    for name in sorted(set(previous_files) - set(files)):
        safe_path(repo, relative_name(name)).unlink()
    for name, content in files.items():
        path = safe_path(repo, name, file=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(repo, "add", "-A", "--", *sorted(set(previous_files) | set(files)))
    staged = _tracked_files(repo)
    require(staged == files, "Staged public sources differ from the approved candidate.")
    if _git(repo, "diff", "--cached", "--name-only"):
        _git(repo, "commit", "-m", "chore: publish source v" + version)
        head = _git(repo, "rev-parse", "HEAD")
    receipt = {"repository": REPOSITORY, "version": version, "pending_commit": head,
               "remote_base": remote_head, "source_files": len(files)}
    _state(root, receipt)
    if not tag_commit:
        _git(repo, "tag", "-a", tag, "-m", "Source snapshot for v" + version)
    receipt.update({"pending_tag": tag, "pending_tag_oid": _git(repo, "rev-parse", "refs/tags/" + tag)})
    _state(root, receipt)
    _git(repo, "push", "--atomic", "origin", "HEAD:refs/heads/main", "refs/tags/" + tag + ":refs/tags/" + tag)
    _verify_public(head, files)
    # Re-check release inputs after network work; never export a changed installer.
    require(snapshot(root)[0] == files, "Source changed during publication. Rerun before releasing the website.")
    result = {"repository": REPOSITORY, "version": version, "commit": head, "tag": tag,
              "source_files": len(files), "public_verified": True,
              "installer": {key: document["published_installer"][key] for key in ("file", "bytes", "sha256")},
              "snapshot_sha256": snapshot_digest(files),
              "release_inputs": len(document["application_sources"]) + len(document["application_assets"])}
    _state(root, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        print(json.dumps(publish_source(), indent=2))
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    from release_lock import release_lock
    with release_lock(ROOT):
        main()
