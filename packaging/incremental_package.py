"""Build and audit content-addressed file updates from the verified frozen payload.

Only application payload bytes enter this package. The existing signed release
authenticates the manifest hash; each manifest authenticates wire and raw bytes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re

MANIFEST_PATTERN = re.compile(r"updates/([0-9]+\.[0-9]+\.[0-9]+)/files\.json\Z")
BLOB_PATTERN = re.compile(r"updates/blobs/[a-f0-9]{64}\.gz\Z")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def blob_path(row):
    return "updates/blobs/" + row["blob"]["sha256"] + ".gz"


def read_file(base: Path, name: str, maximum: int) -> bytes:
    base = Path(base).absolute()
    require(MANIFEST_PATTERN.fullmatch(name) or BLOB_PATTERN.fullmatch(name), "Unexpected update artifact path.")
    path = base / name
    for part in (path, *path.parents):
        require(not part.is_symlink() and not part.is_junction(), "Linked update artifacts are not allowed.")
    require(path.is_file() and path.stat().st_size <= maximum, "Missing or oversized update artifact: " + name)
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    require(len(content) <= maximum, "Update artifact grew while reading: " + name)
    return content


def verify_blob(content: bytes, row: dict) -> None:
    require(len(content) == row["blob"]["bytes"] and digest(content) == row["blob"]["sha256"],
            "Compressed update checksum/size differs: " + row["path"])
    from incremental_update import decode_blob
    try:
        decode_blob(content, row)
    except ValueError as exc:
        raise ValueError("Expanded update checksum/size or gzip format differs: " + row["path"]) from exc


def verify_public(read, public: dict, *, names=None, expected_payload=None) -> tuple[dict | None, set[str]]:
    """Audit an authenticated public descriptor through a bounded reader callback."""
    if "incremental" not in public:
        return None, set()
    from incremental_update import validate_descriptor, validate_manifest
    descriptor = validate_descriptor(public["incremental"], public["version"])
    record = descriptor["manifest"]
    data = read(record["file"], record["bytes"])
    require(len(data) == record["bytes"] and digest(data) == record["sha256"], "Update manifest checksum/size differs.")
    document = validate_manifest(data, public["version"])
    if expected_payload is not None:
        require([{key: row[key] for key in ("path", "bytes", "sha256")} for row in document["files"]]
                == expected_payload, "Update manifest differs from the verified installer payload.")
    used = {record["file"]}
    checked = set()
    for row in document["files"]:
        name = blob_path(row)
        used.add(name)
        identity = (name, row["bytes"], row["sha256"])
        if identity in checked:
            continue
        verify_blob(read(name, row["blob"]["bytes"]), row)
        checked.add(identity)
    if names is not None:
        require(used.issubset(names), "A referenced update artifact is missing.")
    return document, used


def verify_package(base: Path, descriptor: dict, version: str, *, expected_payload=None):
    public = {"version": version, "incremental": descriptor}
    return verify_public(lambda name, limit: read_file(base, name, limit), public,
                         expected_payload=expected_payload)


def create_package(app: Path, release: Path, version: str, records: list[dict], cleanup: dict) -> dict:
    """Write deterministic immutable artifacts, then audit every encoded payload."""
    from incremental_update import program_path, validate_manifest
    release = Path(release)
    for parent in (release, *release.parents):
        require(not parent.is_symlink() and not parent.is_junction(), "Linked package output is not allowed.")
    rows, blobs = [], {}
    for record in records:
        program_path(record["path"])
        source = Path(app) / record["path"]
        for part in (source, *source.parents):
            require(not part.is_symlink() and not part.is_junction(), "Linked payload file is not allowed.")
        require(source.is_file() and source.stat().st_size == record["bytes"]
                and 0 <= record["bytes"] <= MAX_FILE_BYTES, "Payload changed while packaging.")
        content = source.read_bytes()
        require(len(content) == record["bytes"] and digest(content) == record["sha256"], "Payload changed while packaging.")
        compressed = gzip.compress(content, compresslevel=9, mtime=0)
        blob = {"bytes": len(compressed), "sha256": digest(compressed)}
        row = {**record, "blob": blob}
        rows.append(row)
        blobs[blob_path(row)] = compressed
    document = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "platform": "windows-x64",
                "version": version, "files": rows, "cleanup": cleanup}
    data = (json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    validate_manifest(data, version)
    name = f"updates/{version}/files.json"
    descriptor = {"schema_version": 1, "manifest": {"file": name, "bytes": len(data), "sha256": digest(data)}}
    for relative, content in {**blobs, name: data}.items():
        path = release / relative
        for part in (path, *path.parents):
            require(not part.is_symlink() and not part.is_junction(), "Linked update output is not allowed.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            require(path.is_file() and path.read_bytes() == content, "Immutable update artifact differs: " + relative)
        else:
            with path.open("xb") as stream:
                stream.write(content)
    verify_package(release, descriptor, version, expected_payload=records)
    return descriptor
