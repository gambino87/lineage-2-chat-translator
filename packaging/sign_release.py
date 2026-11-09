"""Publisher-only helper; the signing key never leaves the Windows user store.

Explicit one-time provisioning is separate from signing. Normal builds fail if
the key is absent or differs from the key already pinned in the application.
"""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from update_trust import MAX_METADATA_BYTES, SIGNATURE_BYTES, verify_release_signature

SIGNER = Path(__file__).resolve().with_name("sign-release.ps1")
SIGN_TIMEOUT_SECONDS = 60


def _powershell() -> Path:
    if os.name != "nt":
        raise ValueError("Release signing requires the publisher's Windows machine.")
    kernel = ctypes.WinDLL("kernel32.dll", winmode=0x00000800, use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("Cannot locate Windows PowerShell.")
    return Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe"


def sign_metadata(metadata: bytes) -> bytes:
    """Sign exact bounded metadata with the fixed publisher key and verify it."""
    if type(metadata) is not bytes or not 0 < len(metadata) <= MAX_METADATA_BYTES:
        raise ValueError("Release metadata must be between 1 and 65536 bytes.")
    with tempfile.TemporaryDirectory(prefix="l2ct-release-sign-") as temporary:
        directory = Path(temporary)
        source, target = directory / "release.json", directory / "release.sig"
        source.write_bytes(metadata)
        result = subprocess.run(
            [str(_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(SIGNER), "-Mode", "Sign", "-InputFile", str(source), "-OutputFile", str(target)],
            capture_output=True, text=True, timeout=SIGN_TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False,
        )
        if result.returncode:
            raise ValueError("Publisher signing failed; check that this Windows account has the provisioned signing key.")
        if not target.is_file() or target.stat().st_size != SIGNATURE_BYTES:
            raise ValueError("The publisher signer returned an invalid signature.")
        signature = target.read_bytes()
        verify_release_signature(metadata, signature)
        return signature


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("signature", type=Path)
    args = parser.parse_args()
    if args.metadata.stat().st_size > MAX_METADATA_BYTES:
        raise SystemExit("Release metadata is too large.")
    signature = sign_metadata(args.metadata.read_bytes())
    with args.signature.open("xb") as output:
        output.write(signature)


if __name__ == "__main__":
    main()
