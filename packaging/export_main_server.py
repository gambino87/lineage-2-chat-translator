"""Export a verified public release for the main server; never connects or deploys."""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://40.160.140.227"


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        "l2_export_" + name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_site = _sibling("release_site")
_build = _sibling("build")
SOURCES, ASSETS = _build.SOURCES, _build.ASSETS
require = release_site.require


def validated_base_url(value: str) -> str:
    """Accept only an HTTPS origin, so root-relative site links remain correct."""
    require(isinstance(value, str) and not re.search(r"[\s\x00-\x1f\x7f]", value),
            "Base URL must be an HTTPS origin without whitespace.")
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
            and "?" not in value and "#" not in value,
            "Base URL must be an HTTPS origin without credentials, path, query or fragment.")
    # Accessing port also rejects malformed/out-of-range port numbers.
    require(parsed.port is None or parsed.port > 0, "Invalid HTTPS port.")
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        require(len(host) <= 253 and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
            for part in host.rstrip(".").split(".")), "Invalid HTTPS host.")
    require(not parsed.netloc.endswith(":"), "Invalid HTTPS port.")
    return "https://" + parsed.netloc


_TAG = re.compile(r"<(?:link|meta)\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r'''(?P<name>[\w:-]+)\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)''',
                        re.DOTALL)


def rewrite_homepage(page: bytes, base_url: str) -> bytes:
    """Change canonical/og:url origins only, preserving every other byte."""
    base_url = validated_base_url(base_url)
    text = page.decode("utf-8")
    found = {"canonical": 0, "og:url": 0}

    def replace_tag(match):
        tag = match.group(0)
        attributes = {item.group("name").lower(): item for item in _ATTRIBUTE.finditer(tag)}
        values = {name: html.unescape(item.group("value")) for name, item in attributes.items()}
        kind, url_attribute = None, None
        if tag[:5].lower() == "<link" and "canonical" in values.get("rel", "").lower().split():
            kind, url_attribute = "canonical", "href"
        elif tag[:5].lower() == "<meta" and values.get("property", "").lower() == "og:url":
            kind, url_attribute = "og:url", "content"
        if kind is None:
            return tag
        require(url_attribute in attributes, f"Missing {kind} URL attribute.")
        attribute = attributes[url_attribute]
        original = attribute.group("value")
        require(re.match(r"https?://[^/?#]+", original, re.IGNORECASE) is not None,
                f"Expected absolute {kind} URL.")
        updated = re.sub(r"^https?://[^/?#]+", lambda _: html.escape(base_url, quote=True),
                         original, count=1, flags=re.IGNORECASE)
        found[kind] += 1
        start, end = attribute.span("value")
        return tag[:start] + updated + tag[end:]

    rewritten = _TAG.sub(replace_tag, text)
    require(found == {"canonical": 1, "og:url": 1},
            "Homepage must contain exactly one canonical link and one og:url.")
    return rewritten.encode("utf-8")


def _canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def verify_archive(archive: Path, manifest: dict) -> None:
    """Verify all ZIP members, installer identity and the content-based release ID."""
    identity = {key: manifest[key] for key in ("schema_version", "version", "base_url", "installer", "files")}
    expected_id = manifest["version"] + "-" + hashlib.sha256(_canonical_json(identity)).hexdigest()[:12]
    require(manifest.get("release_id") == expected_id, "Release ID does not match its manifest.")
    info = manifest["archive"]
    require(archive.stat().st_size == info["bytes"] and release_site.digest(archive) == info["sha256"],
            "Archive checksum/size differs from its manifest.")
    records = manifest["files"]
    names = [row["path"] for row in records]
    require(len(names) == len(set(names)), "Duplicate manifest paths.")
    for name in names:
        require(not name.startswith("/") and "\\" not in name
                and all(part not in ("", ".", "..") for part in name.split("/")), "Unsafe archive path.")
    with zipfile.ZipFile(archive) as bundle:
        require(bundle.namelist() == names, "Archive members differ from its manifest.")
        for row in records:
            member = bundle.getinfo(row["path"])
            with bundle.open(member) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            require(member.file_size == row["bytes"] and digest == row["sha256"],
                    f"Archive content mismatch: {row['path']}")
    installer = manifest["installer"]
    require({"path": "downloads/" + installer["file"], "bytes": installer["bytes"],
             "sha256": installer["sha256"]} in records, "Installer metadata mismatch.")


def export_release(root: Path = ROOT, site: Path | None = None,
                   output_dir: Path | None = None, base_url: str = DEFAULT_BASE_URL) -> dict:
    root = Path(root).resolve()
    site = Path(site).resolve() if site is not None else root / "sites/l2-chat-translator"
    output_dir = Path(output_dir) if output_dir is not None else root / "release"
    base_url = validated_base_url(base_url)
    prepared = release_site.prepare_site(root, site, SOURCES, ASSETS, check=True)
    dist = site / "dist"
    require(not output_dir.is_symlink() and not output_dir.is_junction(), "Linked export directory is not allowed.")
    output_dir = output_dir.resolve()
    require(not output_dir.is_relative_to(dist.resolve()), "Export output must be outside public dist.")
    output_dir.mkdir(parents=True, exist_ok=True)
    version = prepared["version"]
    stem = f"Lineage-2-Chat-Translator-{version}-main-server"
    archive_path = output_dir / (stem + ".zip")
    manifest_path = output_dir / (stem + ".manifest.json")
    for target in (archive_path, manifest_path):
        require(not target.is_symlink() and not target.is_junction(), "Linked export output is not allowed.")
    sources = []
    files = []
    with tempfile.TemporaryDirectory(prefix=".main-server-", dir=output_dir) as temporary:
        scratch = Path(temporary)
        staged_archive = scratch / archive_path.name
        staged_manifest = scratch / manifest_path.name
        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(dist.rglob("*")):
                require(not path.is_symlink() and not path.is_junction()
                        and path.resolve().is_relative_to(dist.resolve()), "Unsafe public file.")
                if not path.is_file():
                    continue
                name = path.relative_to(dist).as_posix()
                content = path.read_bytes()
                sources.append((path, hashlib.sha256(content).hexdigest()))
                if name == "index.html":
                    content = rewrite_homepage(content, base_url)
                files.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
                member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(member, content, compresslevel=6)
        manifest = {"schema_version": 1, "version": version, "base_url": base_url,
                    "installer": prepared["installer"], "files": files}
        manifest["release_id"] = version + "-" + hashlib.sha256(_canonical_json(manifest)).hexdigest()[:12]
        manifest["archive"] = {"file": archive_path.name, "bytes": staged_archive.stat().st_size,
                               "sha256": release_site.digest(staged_archive)}
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        # Recheck the allowlist and source release after reading; never replace a valid
        # previous export with stale, private or partially copied content.
        current = release_site.prepare_site(root, site, SOURCES, ASSETS, check=True)
        require(current == prepared and all(release_site.digest(path) == sha for path, sha in sources),
                "Public release changed during export.")
        verify_archive(staged_archive, json.loads(staged_manifest.read_text(encoding="utf-8")))
        staged_archive.replace(archive_path)
        staged_manifest.replace(manifest_path)
    return {"version": version, "release_id": manifest["release_id"], "base_url": base_url,
            "archive": str(archive_path), "manifest": str(manifest_path),
            "installer": manifest["installer"], "files": len(files), "deployed": False}


def publish_and_export(*, output_dir: Path | None = None,
                       base_url: str = DEFAULT_BASE_URL) -> dict:
    """Keep the public source current before producing a deployable release bundle.

    Validate the website first so a bad local release cannot publish source.
    Source publication verifies its public commit and immutable version tag;
    any failure stops the export. The low-level exporter remains offline for
    isolated tests, while this is the required production entry point.
    """
    base_url = validated_base_url(base_url)
    release_site.prepare_site(ROOT, ROOT / "sites/l2-chat-translator", SOURCES, ASSETS, check=True)
    publisher = _sibling("source_release")
    source = publisher.publish_source(ROOT)
    result = export_release(output_dir=output_dir, base_url=base_url)
    require(source.get("version") == result["version"]
            and source.get("installer") == result["installer"],
            "Release changed after source publication. Do not deploy this bundle; rerun the release export.")
    publisher.verify_source_current(ROOT, source)
    result["source"] = source
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="HTTPS origin, without a path.")
    parser.add_argument("--output-dir", type=Path, help="Defaults to the project's release directory.")
    args = parser.parse_args()
    try:
        print(json.dumps(publish_and_export(output_dir=args.output_dir, base_url=args.base_url), indent=2))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
