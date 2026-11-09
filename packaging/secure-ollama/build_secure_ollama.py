"""Build the private Windows frontend without launching Ollama or any model.

All archives are pinned; Go dependencies use the upstream committed go.sum.
Native GPU/CPU binaries are taken from the verified official runtime ZIP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import urllib.request
import zipfile

from patch_source import apply

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
LOCK = json.loads((HERE / "build-lock.json").read_text(encoding="utf-8"))
CACHE = WORKSPACE / ".build-tools" / "secure-ollama"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(name: str) -> Path:
    entry = LOCK["downloads"][name]
    target = CACHE / f"{name}.zip"
    if not target.exists():
        temporary = target.with_suffix(".partial")
        with urllib.request.urlopen(entry["url"], timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        temporary.replace(target)
    if digest(target) != entry["sha256"]:
        raise RuntimeError(f"Pinned archive checksum mismatch: {name}")
    return target


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts or "\\" in item.filename or ":" in item.filename:
                raise RuntimeError("Unsafe archive member")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Archive symlinks are not supported")
        bundle.extractall(destination)


def reset_work() -> Path:
    work = CACHE / "work"
    # Never recursively delete an unverified computed path or junction.
    if work.exists():
        if work.is_symlink() or work.is_junction() or work.resolve().parent != CACHE.resolve():
            raise RuntimeError("Build work directory must be a normal child of the cache")
        shutil.rmtree(work)
    work.mkdir()
    return work


def native_manifest(archive: Path) -> dict:
    files = []
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if item.is_dir():
                continue
            with bundle.open(item) as source:
                sha = hashlib.file_digest(source, "sha256").hexdigest()
            files.append({"path": item.filename, "sha256": sha, "bytes": item.file_size})
    # A separately committed member manifest makes upstream ZIP contents reviewable.
    expected = json.loads((HERE / "upstream-runtime-manifest.json").read_text(encoding="utf-8"))
    normalized = [{"path": f["path"], "sha256": f["sha256"], "bytes": f["size"]} for f in expected["files"]]
    if files != normalized:
        raise RuntimeError("Official runtime member manifest differs from the reviewed list")
    return {"files": files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-only", action="store_true", help="compile/run security tests without building a distributable")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This pinned build requires Windows x64")
    CACHE.mkdir(parents=True, exist_ok=True)
    archives = {name: download(name) for name in LOCK["downloads"]}
    for name in ("go", "llvm"):
        destination = CACHE / name
        if not destination.exists():
            extract(archives[name], destination)
    work = reset_work()
    extract(archives["source"], work)
    source = work / ("ollama-" + LOCK["source_commit"])
    apply(source)
    go = CACHE / "go" / "go" / "bin" / "go.exe"
    compiler_root = CACHE / "llvm" / "llvm-mingw-20261104-ucrt-x86_64" / "bin"
    env = os.environ.copy()
    env.update({
        "GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "1", "GOTOOLCHAIN": "local",
        # cgo splits CC as command text; resolve the pinned compiler through PATH
        # rather than putting an unquoted workspace path with spaces in CC.
        "CC": "x86_64-w64-mingw32-gcc.exe",
        "CXX": "x86_64-w64-mingw32-g++.exe",
        "GOPATH": str(CACHE / "gopath"), "GOCACHE": str(CACHE / "gocache"),
        "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org",
        "PATH": str(compiler_root) + os.pathsep + str(go.parent) + os.pathsep + env.get("PATH", ""),
    })
    # Do not allow caller-selected flags/toolchain files to alter the verified build.
    for name in ("GOFLAGS", "GOENV", "GOROOT", "CGO_CFLAGS", "CGO_CPPFLAGS", "CGO_CXXFLAGS", "CGO_LDFLAGS"):
        env.pop(name, None)
    env["GOENV"] = "off"
    changed = [source / p for p in ("cmd/cmd.go", "server/routes.go", "llm/llama_server.go", "mlxrunner/client.go", "mlxrunner/server.go", "discover/llama_server.go")]
    changed += list((source / "l2ctsecurity").glob("*.go"))
    subprocess.run([str(go.parent / "gofmt.exe"), "-w", *map(str, changed)], cwd=source, env=env, check=True)
    subprocess.run([str(go), "test", "-mod=readonly", "-count=1", "-timeout=120s", "./l2ctsecurity"], cwd=source, env=env, check=True)
    subprocess.run([str(go), "mod", "verify"], cwd=source, env=env, check=True)
    if args.tests_only:
        return
    output = CACHE / "output"
    output.mkdir(exist_ok=True)
    executable = output / "ollama.exe"
    flags = f"-s -w -X=github.com/ollama/ollama/version.Version={LOCK['version']} -X=github.com/ollama/ollama/server.mode=release"
    subprocess.run([str(go), "build", "-mod=readonly", "-trimpath", "-buildvcs=false", "-ldflags", flags, "-o", str(executable), "."], cwd=source, env=env, check=True)
    manifest = native_manifest(archives["ollama-windows-amd64"])
    old_executable = next(f for f in manifest["files"] if f["path"] == "ollama.exe")
    upstream_executable_sha256 = old_executable["sha256"]
    old_executable.update(sha256=digest(executable), bytes=executable.stat().st_size)
    manifest.update(version=LOCK["version"], upstream_version=LOCK["upstream_version"],
                    archive_sha256=LOCK["downloads"]["ollama-windows-amd64"]["sha256"],
                    upstream_executable_sha256=upstream_executable_sha256,
                    source_commit=LOCK["source_commit"],
                    source_archive_sha256=LOCK["downloads"]["source"]["sha256"],
                    patch_inputs={p.relative_to(HERE).as_posix(): digest(p) for p in
                                  sorted([HERE / "patch_source.py", HERE / "build-lock.json", *HERE.glob("overlay/**/*.go")])})
    (output / "secure-ollama-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"executable": str(executable), "sha256": digest(executable), "bytes": executable.stat().st_size,
                      "manifest": str(output / "secure-ollama-manifest.json")}), flush=True)


if __name__ == "__main__":
    main()
