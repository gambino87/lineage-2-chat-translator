# Independent update signatures

From v1.7.12, updater trust comes from a public key shipped in the application,
not from the server hosting the installer. The dedicated release signing key
stays in the publisher's Windows user key store. It is not included in the
source repository, installer, site, export bundle or server deployment.

The protocol is RSA-3072 PSS with SHA-256 and a 32-byte salt, implemented by
Windows cryptography. `release.sig` is the 384-byte detached signature over the
exact UTF-8 bytes of `release.json`. Do not normalize line endings or reformat
the signed JSON. The signed metadata identifies the product, platform, version,
installer filename, byte count and SHA-256. Both the app and the bundled update
helper verify it before running an installer; missing or invalid signatures
stop the update. HTTPS and installer hash checks remain required.

An attacker controlling only the download host can withhold updates or cause
checks to fail. They cannot create a valid signature for a replacement installer.
The client rejects versions at or below its installed version. This does not
guarantee freshness: a host can hide a newer release or serve an older signed
release that is still newer than the installed application.

## Key custody and recovery

`packaging/sign-release.ps1` uses the dedicated current-user CNG key
`Lineage2ChatTranslator.UpdateSigning.v1`, with signing-only use and no private
key export. Provisioning must never overwrite an existing key. Only public
verification material belongs in `update_trust.py` and the update helper;
their trust anchors must match. The release signer verifies every produced
signature against that pinned key before accepting it.

For a new publisher identity, run the headless helper explicitly:

```powershell
powershell -NoProfile -NonInteractive -File packaging/sign-release.ps1 -Mode Provision
```

`-Mode Inspect` prints only the public key and policy of an existing identity.
Pin that public key in the app and helper before building the first release.
Normal builds use `sign_metadata()`; they do not provision or replace keys.

The signing account and build machine are trusted. Non-exportable storage does
not prevent malware running as that account from requesting signatures. Keep
server and publication credentials separate from the signing identity. No
signing service or key is installed on the public server.

This key is not portable and this setup does not create a recovery backup.
Losing the Windows key or account can prevent future signing. Do not replace
the pinned public key casually: existing installations trust the old key. A
planned rotation requires a release authenticated by the old key first; loss
or compromise may require a separately trusted manual installation. Automatic
key rotation is not implemented.

## Release and bootstrap

Normal release preparation validates the built installer and signs its public
metadata locally once. Later preparations reuse the exact verified pair.
Check-only validation never generates a signature or provisions a key.
Publish the installer, `release.json` and `release.sig` atomically on both hosts.
The public source gate and existing immutable installer rules still apply.

Versions before v1.7.12 do not contain the trust anchor. Merely placing a
signature on the existing host cannot protect those versions' first upgrade.
The first signature-aware installer must be obtained or verified through a
separately trusted channel. This establishes trust for subsequent updates.

These are updater signatures, not Windows Authenticode signatures. Windows
can still display an unknown publisher or SmartScreen prompt. The homepage
and release metadata must continue to report `authenticode_signed: false`.
Signatures do not protect an already modified application or compromised
Windows account, and do not attest third-party runtime or model downloads.
