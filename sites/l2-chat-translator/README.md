# Lineage 2 Chat Translator download site

Buildless static homepage and verified Windows installers. Public files are in
`dist/`; the homepage source is `index.template.html`. No JavaScript dependencies
or external assets are required.

From the parent translator project, `packaging/build.py` builds/tests a release
and calls `packaging/release_site.py` to generate this Site from the verified
release manifest. The same source version controls the installer, page version,
all download links, file size, checksum, signing status and default model details.
Review feature descriptions in the template when the application's behavior changes.

Add a new entry to `changelog.json` for every application release: each entry has
`version` and `changes` (nonempty plain-text notes). Keep entries newest first,
preserve older notes, and match the newest version to `app_version.py`. The build
and Site preparation reject missing notes, duplicates and incorrect ordering.
The generator escapes note text and inserts the history at `{{CHANGELOG}}`.

Keep the page practical and concise. The preview is an HTML/CSS illustration
with fictional messages, not a captured application screenshot or interactive
translator. Update it when relevant UI behavior changes. Do not restore the
visible checksum/release-metadata links; those files remain available for release
verification without appearing in the page's download area.

An unchanged release can be re-prepared with `packaging/release_site.py`.
Run `packaging/release_site.py --check` immediately before publishing. Preserve
older versioned installers; their matching local release manifests are needed
for verification. Never replace an installer with different bytes under the same
version. A new application release needs a new version.

The primary host is `https://40.160.140.227/`. Export with the parent project's
`packaging/export_main_server.py` and follow `packaging/HOSTING.md` for verified
atomic publication. The exporter changes only the page's canonical/og:url origin;
the installer bytes are identical. Homepage and downloads publish together.

This existing ChatGPT Site remains a separate public mirror. When updating it,
preserve its project identity and public audience and deploy homepage/downloads
in one archive. Main-server publication does not automatically update the mirror.
Local preparation does not publish to either host. Verify each live version and
actual installer checksum after deployment.

Do not add credentials, user settings, game logs, review examples, model weights,
or local validation reports to this source tree. Ollama and models are downloaded
separately by setup.
