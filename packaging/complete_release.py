"""Verify the published release, then automatically prune local build leftovers.

This is the final release-workflow step, after both website deployments. It does
not publish, execute an installer, start services, or access model directories.
Any verification failure occurs before cleanup and preserves retry artifacts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from build import SOURCES, ASSETS
from build_environment import windows_directories
from export_main_server import DEFAULT_BASE_URL, rewrite_homepage, validated_base_url
import release_site
import source_release
from release_cleanup import cleanup_release, plan_cleanup
from release_lock import release_lock
from payload_layout import retirement_plan

MIRROR_URL = "https://lineage-2-chat-translator.vargas87706341.chatgpt.site"
REQUEST_SECONDS = 90
CONNECT_SECONDS = 15
MAX_PAGE_BYTES = 1024 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_PROVIDER_SCRIPT_BYTES = 16 * 1024
MAX_INSTALLER_BYTES = 256 * 1024 * 1024
require = release_site.require


def fetch_https(url: str, limit: int) -> bytes:
    """Use Windows' TLS client with no curl config, proxy, redirect or execution."""
    require(url.startswith("https://") and 0 < limit <= MAX_INSTALLER_BYTES, "Invalid release verification request.")
    _, system = windows_directories()
    curl = system / "curl.exe"
    require(curl.is_file(), "Windows HTTPS client is unavailable; cleanup has not run.")
    try:
        result = subprocess.run(
            [str(curl), "--disable", "--noproxy", "*", "--proto", "=https", "--fail", "--silent", "--show-error",
             "--connect-timeout", str(CONNECT_SECONDS), "--max-time", str(REQUEST_SECONDS),
             "--max-filesize", str(limit), url],
            capture_output=True, check=True, timeout=REQUEST_SECONDS + 5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # External errors may contain response content; show only the public URL.
        raise ValueError(f"Could not verify {url}; cleanup has not run.") from exc
    require(len(result.stdout) <= limit, "Public response exceeded its expected size.")
    return result.stdout


def page_matches(actual: bytes, expected: bytes, *, allow_provider_script: bool) -> bool:
    if actual == expected:
        return True
    if not allow_provider_script or expected.count(b"</body>") != 1:
        return False
    # Sites may inject its own final browser challenge. Every authored byte must
    # still match, including the version, download link and complete changelog.
    before, after = expected.rsplit(b"</body>", 1)
    closing = b"</body>" + after
    if not actual.startswith(before) or not actual.endswith(closing):
        return False
    extra = actual[len(before):-len(closing)]
    return (0 < len(extra) < MAX_PROVIDER_SCRIPT_BYTES and extra.startswith(b"<script>")
            and extra.endswith(b"</script>") and extra.count(b"<script>") == 1
            and extra.count(b"</script>") == 1 and b"/cdn-cgi/challenge-platform/" in extra)


def verify_host(base: str, expected_page: bytes, metadata: bytes, signature: bytes,
                installer: dict, *, mirror: bool) -> dict:
    """Read and authenticate actual public bytes; never trust a success receipt."""
    base = validated_base_url(base)
    published = fetch_https(base + "/downloads/release.json", release_site.MAX_METADATA_BYTES)
    proof = fetch_https(base + "/downloads/release.sig", release_site.SIGNATURE_BYTES)
    release_site.verify_signature(published, proof)
    require(published == metadata and proof == signature, f"{base} is serving a different release; cleanup has not run.")
    require(re.fullmatch(release_site.INSTALLER_PATTERN, installer["file"])
            and isinstance(installer["bytes"], int) and 0 < installer["bytes"] <= MAX_INSTALLER_BYTES,
            "Invalid installer identity.")
    binary = fetch_https(base + "/downloads/" + installer["file"], installer["bytes"])
    require(len(binary) == installer["bytes"] and hashlib.sha256(binary).hexdigest() == installer["sha256"],
            f"Public installer differs at {base}; cleanup has not run.")
    checksums = fetch_https(base + "/downloads/SHA256SUMS.txt", MAX_CHECKSUM_BYTES).decode("ascii")
    require(f"{installer['sha256']}  {installer['file']}" in checksums.splitlines(), "Published checksum file differs.")
    page = fetch_https(base + "/", MAX_PAGE_BYTES)
    require(page_matches(page, expected_page, allow_provider_script=mirror),
            f"Homepage differs at {base}; wait for publication to finish before retrying.")
    return {"url": base, "signature_verified": True, "installer_verified": True, "homepage_verified": True}


def verify_public_source(root: Path) -> dict:
    """Check current GitHub main and the immutable tag's application identity."""
    receipt = source_release._state(root)
    source_release.verify_source_current(root, receipt)
    files, document = source_release.snapshot(root)
    commit = receipt.get("commit", "")
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "Published source commit is missing.")
    source_release._verify_public(commit, files)
    repo = source_release.safe_path(root, source_release.CLONE, file=False)
    require(source_release._git(repo, "remote", "get-url", "--all", "origin") == source_release.REMOTE,
            "Unexpected source repository remote.")
    tag = "v" + document["version"]
    local_tag = source_release._git(repo, "rev-parse", "refs/tags/" + tag)
    remote_tag = source_release._git(repo, "ls-remote", "origin", "refs/tags/" + tag)
    require(remote_tag == local_tag + "\trefs/tags/" + tag, "Published version tag differs.")
    tagged = json.loads(source_release._git(repo, "show", tag + ":" + source_release.MANIFEST))
    require(source_release.release_identity(tagged) == source_release.release_identity(document),
            "Version tag does not match the released application and installer.")
    return receipt


def verify_retained_inputs(root: Path, version: str) -> dict:
    """Require reusable backend/tools and intact historical installers first."""
    inventory = json.loads((root / "assets/secure-ollama-manifest.json").read_text(encoding="utf-8"))
    expected = next(row for row in inventory["files"] if row["path"] == "ollama.exe")
    backend = root / ".build-tools/secure-ollama/output/ollama.exe"
    require(backend.is_file() and not backend.is_symlink() and backend.stat().st_size == expected["bytes"]
            and release_site.digest(backend) == expected["sha256"],
            "Keep a verified compiled backend before deleting its build cache.")
    for name in (".build-tools/venv/Scripts/python.exe", ".build-tools/inno-portable/tools/ISCC.exe"):
        require((root / name).is_file(), "A required release tool is missing; caches have been preserved: " + name)
    manifest = json.loads((root / "release" / version / "release-manifest.json").read_text(encoding="utf-8"))
    major, minor, patch = map(int, version.split("."))
    history = retirement_plan(manifest["payload"], root / "release", f"{major}.{minor}.{patch + 1}")
    return {"backend_sha256": expected["sha256"], "historical_installers_verified": True,
            "future_obsolete_file_paths": len(history["obsolete_files"])}


def complete_release(root: Path = ROOT, *, main_url: str = DEFAULT_BASE_URL,
                     mirror_url: str = MIRROR_URL, dry_run: bool = False) -> dict:
    root = Path(root).absolute()
    with release_lock(root):
        site = root / "sites/l2-chat-translator"
        prepared = release_site.prepare_site(root, site, SOURCES, ASSETS, check=True)
        version = prepared["version"]
        retained = verify_retained_inputs(root, version)
        folder = root / "release" / version
        metadata = (folder / "release.json").read_bytes()
        signature = (folder / "release.sig").read_bytes()
        local_page = (site / "dist/index.html").read_bytes()
        # Check every gate before cleanup is even invoked. Old saved reports and
        # an exporter/build success are not proof that a release is live.
        source = verify_public_source(root)
        hosts = [verify_host(main_url, rewrite_homepage(local_page, main_url), metadata, signature,
                             prepared["installer"], mirror=False),
                 verify_host(mirror_url, local_page, metadata, signature, prepared["installer"], mirror=True)]
        require(release_site.prepare_site(root, site, SOURCES, ASSETS, check=True) == prepared,
                "Local release changed during verification; cleanup has not run.")
        source_release.verify_source_current(root, source)
        require(verify_retained_inputs(root, version) == retained, "Required release inputs changed; cleanup has not run.")
        latest = json.loads((root / ".build/latest-build.json").read_text(encoding="utf-8"))
        latest_build = Path(latest["build"])
        cleanup = ({"dry_run": True, "candidates": plan_cleanup(root, version, latest_build)} if dry_run
                   else cleanup_release(root, version, latest_build))
        report = {"version": version, "verified_at_utc": datetime.now(timezone.utc).isoformat(),
                  "source_commit": source["commit"], "hosts": hosts, "cleanup": cleanup,
                  "retained_inputs": retained,
                  "models_touched": False, "apps_or_services_started": False}
        # The cleanup helper holds its checked workspace boundaries and records
        # deletion progress itself. This small completion record is informational.
        destination = root / ".build" / f"release-completion-{version}.local.json"
        require(not destination.is_symlink() and not destination.is_junction(), "Linked completion report refused.")
        destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Verify both live hosts and source, then list cleanup without deleting.")
    args = parser.parse_args()
    try:
        print(json.dumps(complete_release(dry_run=args.dry_run), indent=2))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
