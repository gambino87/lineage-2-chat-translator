"""Verify publisher release signatures with the Windows cryptography provider.

The public key is part of the installed application, never downloaded from the
release server. The dedicated private key remains in the publisher's Windows
user key store. Signatures cover the exact release.json bytes, before parsing.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import os
import struct

MAX_METADATA_BYTES = 64 * 1024
SIGNATURE_BYTES = 384
PUBLIC_KEY_BLOB_B64 = "UlNBMQAMAAADAAAAgAEAAAAAAAAAAAAAAQABqH1NyNtVCMd/9YZYLdUITlH/T8qIwWUEbZZmLRCYBeo2rC1il+aTdicUZ+YvJvn3zSuGOfil71uhGzQRlx79amfzzJIQdssaGE1PKy82d5HA1vsQ17CcZwmJYcuRYnEsqyIrhKoTTOeLtgp6Y6+hzgT+DUXwFzG1w0IIp+iI/qeXgl5UhcO10QOsfl3+bsirG39xAWu/9aD+i9lZHNJ4Q7RTzuKKJNBcR1StiKuPsh8rrLVtRZlD/ZqDMjmalumrB4ccWXPyhnxhyGHynX4Ct6f9ItKjd0G6V/yzcrig/W0C9TvtelaElJ4nX/U/bKKo6zqwSsRuWTJUTkHmJ2oqL9940eegnsZ0QfxPsJBE/SWWkMqHcPLTZcMNhWO6oBFzU2YxIoo4iOtZfWl6MLHrNYtxNt0nD7yU/qzjtIf0qPtNIkAl/a41RJGrFnz2ijLvud//ZV0MQjNCD/MVFpwzBPpKTqSU/xKi1s2eTnJG94e2B63/B70rgyxzcSptxntp"

# This public proof exercises the pinned key in packaged headless checks. Its
# signed text is deliberately not JSON and cannot authorize an installer.
SELF_TEST_MESSAGE = b"Lineage2ChatTranslator: verification self-test v1 (not release metadata)"
SELF_TEST_SIGNATURE_B64 = "WiKo5iQblUPbXY76mzIVwkqgAa29ikNBZyz6dbj7ajGfcC9HAooVSwT4SDUNVHBs6KWZion6wpIgFaCc8M8s1e2HOcAYzEdBdtmBeE3FtyS0ErmWgIyS3EYE7tjRdTUmIqwAJULkfG8sIr+2zYLH+BsWW0pKdCnU3OSbsJKopRKvMJzcfbolFSYg5IUA2lfHnd+i7HDC3oyeFSC+Esqdy8EOaRjuJ1sDml4DVhh4rHx8eH6EPViiaG0FdH8/bjP2aZ83JDjjr/zaqwQ4+xNoeziKTvaS7TWErDJR5eLjmiftEEocFTxZ+DQR+JyrJKiMOqoqenGB4rAK2g+Gtdd5Dr5M7GMf08Boym64bzGwJolN4IFh3HmGaTgipTFuB1w3HElGx+0fTl2A41f79qWgGoGoNbEuuDY/Hue/4UBVlEHt6wiZxiaNSeJUuc/PyfnoOCI6cbX4Qmss16DifeREG48NnvmSeE2ijYRFEpSTQrnWTVT6/Hb4CYIyMs3plpEi"

# Fixed Windows API values and protocol parameters, not adjustable preferences.
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_BCRYPT_RSAPUBLIC_MAGIC = 0x31415352
_BCRYPT_PAD_PSS = 0x00000008
_RSA_BITS = 3072
_SHA256_BYTES = 32
_RSA_EXPONENT = b"\x01\x00\x01"


class SignatureError(ValueError):
    """The release cannot be authenticated by the installed publisher key."""


class _PssPaddingInfo(ctypes.Structure):
    _fields_ = [("pszAlgId", ctypes.c_wchar_p), ("cbSalt", ctypes.c_ulong)]


def _public_blob(encoded: str) -> bytes:
    """Accept only the precise public-only RSA format used by this protocol."""
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error, TypeError) as exc:
        raise SignatureError("The installed update verification key is invalid.") from exc
    expected_header = (_BCRYPT_RSAPUBLIC_MAGIC, _RSA_BITS, len(_RSA_EXPONENT),
                       SIGNATURE_BYTES, 0, 0)
    if (len(blob) != 24 + len(_RSA_EXPONENT) + SIGNATURE_BYTES
            or struct.unpack("<6I", blob[:24]) != expected_header
            or blob[24:27] != _RSA_EXPONENT
            or not blob[27] & 0x80 or not blob[-1] & 1):
        raise SignatureError("The installed update verification key is invalid.")
    return blob


def _verify_with_key(metadata: bytes, signature: bytes, encoded_key: str) -> None:
    """Internal entry point for independent synthetic-key regression tests."""
    if (type(metadata) is not bytes or not 0 < len(metadata) <= MAX_METADATA_BYTES
            or type(signature) is not bytes or len(signature) != SIGNATURE_BYTES):
        raise SignatureError("The update signature or release information is invalid.")
    blob = _public_blob(encoded_key)
    if os.name != "nt":
        raise SignatureError("Windows is required to verify this update.")
    try:
        # Restrict DLL resolution to Windows, never an application/download folder.
        bcrypt = ctypes.WinDLL("bcrypt.dll", winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
        handle = ctypes.c_void_p
        ulong = ctypes.c_ulong
        status = ctypes.c_long
        bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(handle), ctypes.c_wchar_p,
                                                       ctypes.c_wchar_p, ulong]
        bcrypt.BCryptOpenAlgorithmProvider.restype = status
        bcrypt.BCryptImportKeyPair.argtypes = [handle, handle, ctypes.c_wchar_p,
                                             ctypes.POINTER(handle), ctypes.c_void_p, ulong, ulong]
        bcrypt.BCryptImportKeyPair.restype = status
        bcrypt.BCryptVerifySignature.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p,
                                                ulong, ctypes.c_void_p, ulong, ulong]
        bcrypt.BCryptVerifySignature.restype = status
        bcrypt.BCryptDestroyKey.argtypes = [handle]
        bcrypt.BCryptDestroyKey.restype = status
        bcrypt.BCryptCloseAlgorithmProvider.argtypes = [handle, ulong]
        bcrypt.BCryptCloseAlgorithmProvider.restype = status
        algorithm, key = handle(), handle()
        try:
            if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "RSA", None, 0) != 0:
                raise SignatureError("Windows could not initialize update verification.")
            blob_buffer = ctypes.create_string_buffer(blob)
            if bcrypt.BCryptImportKeyPair(algorithm, None, "RSAPUBLICBLOB", ctypes.byref(key),
                                          blob_buffer, len(blob), 0) != 0:
                raise SignatureError("Windows could not load the update verification key.")
            digest = hashlib.sha256(metadata).digest()
            digest_buffer = ctypes.create_string_buffer(digest)
            signature_buffer = ctypes.create_string_buffer(signature)
            padding = _PssPaddingInfo("SHA256", _SHA256_BYTES)
            result = bcrypt.BCryptVerifySignature(key, ctypes.byref(padding), digest_buffer,
                                                  len(digest), signature_buffer, len(signature),
                                                  _BCRYPT_PAD_PSS)
            if result != 0:
                raise SignatureError("The update publisher signature is invalid. No installer will run.")
        finally:
            if key.value:
                bcrypt.BCryptDestroyKey(key)
            if algorithm.value:
                bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)
    except (OSError, AttributeError) as exc:
        raise SignatureError("Windows could not verify the update publisher signature.") from exc


def verify_release_signature(metadata: bytes, signature: bytes) -> None:
    """Fail closed unless exact metadata bytes are signed by the pinned key."""
    _verify_with_key(metadata, signature, PUBLIC_KEY_BLOB_B64)


def verify_bundled_signing_key() -> None:
    """Verify a publisher-generated public proof without network or key access."""
    verify_release_signature(SELF_TEST_MESSAGE, base64.b64decode(SELF_TEST_SIGNATURE_B64, validate=True))
