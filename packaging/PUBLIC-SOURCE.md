# Publish source with every release

Public repository: https://github.com/gambino87/lineage-2-chat-translator

The source repository is published for transparency. It contains the application,
installer and build scripts, tests, assets, licenses and website source. It does
not contain models, compiled installers, settings, credentials, game logs or
saved translation reviews. Existing third-party licenses remain in effect;
no additional open-source license is selected for the original application.

## Normal release

1. Update and test the application, version and changelog; build the installer.
2. Prepare the website and run `packaging/release_site.py --check`.
3. Run `packaging/export_main_server.py` with the build environment's Python.
   The command validates the prepared release, calls `source_release.py`, then
   creates the server upload bundle only after source publication is verified.
4. Publish and verify the website/installer on the user's OVH server using
   the existing hosting workflow. Confirm the returned GitHub commit and tag.
5. Run `packaging/complete_release.py` to recheck the live server release and GitHub,
   then automatically prune approved old local build caches/packages. A stale
   or failed publication leaves retry artifacts intact. Canonical installers,
   manifests, source and required build inputs are always retained.

`source_release.py` uses a separate source-only Git checkout at
`public-source/lineage-2-chat-translator`. It verifies the application and asset
hashes against the release manifest and creates a new source manifest from an
approved file list. Test/example identities are deidentified. Public README and
license text come from `packaging/public-source/`, not the private development
diary. Review new fixtures and files for private content before each release;
automated scans cannot recognize every possible kind of sensitive information.
When adding build helpers, tools or tests, review and update
`packaging/public-source/allowlist.json`. Unlisted source files in those folders
stop publication so a release cannot silently omit its new build inputs.

The helper uses the operator's existing Git sign-in, without storing credentials
in source. A source update failure is a release blocker, not permission to export
through the offline low-level function. Fix the cause and rerun the production
export command. Unrelated changes in the source checkout must be reviewed first.

New app versions receive an immutable `v<version>` tag. Page/tooling-only changes
update `main` without moving an existing tag, provided the application inputs and
installer identity still match that tag. Never force-push or change a published
version's application bytes. A failed later website deployment can be retried
without creating a duplicate source update.

## Verification limits

Source input hashes identify the files used in the recorded release. They do not
establish byte-for-byte binary reproducibility. Historical DLL-cleanup inventories
depend on verified older installers and manifests, which are intentionally absent
from the source-only repository. A fresh app-only build (`--skip-installer`) can
run without those artifacts, but will have a different historical cleanup
inventory. Official installer/site release preparation also requires the
publisher's private signing identity; it is intentionally absent from GitHub.
