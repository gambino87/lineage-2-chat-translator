# Lineage 2 Chat Translator publication

Primary homepage: https://40.160.140.227/

Installed launchers from v1.7.0 check `/downloads/release.json` when Settings opens.
Preserve this metadata contract: product `Lineage 2 Chat Translator`, platform
`windows-x64`, numeric three-part version, exact versioned installer filename,
byte count, SHA-256, and `validation.packaged_self_test_passed: true`. The feed
must not redirect. Publishing the homepage, installer and metadata in one atomic
release makes that version available to the updater. Never publish metadata
before its verified installer. From v1.7.12, also publish `/downloads/release.sig`
with the exact signed `release.json` bytes. Never reformat that JSON on the server
or mirror. Site preparation signs locally and reuses the verified signature;
check-only preparation does not sign. The server receives no signing key.
The first signature-aware release also requires the exact `/downloads/release.sig`
location in `main-server/https.conf`; the default download rule intentionally
returns 404 for unlisted files. For that one-time change, preserve the active
configuration, run `sudo nginx -t` and reload only Nginx. Future signed releases
use the same route without configuration changes. Verify the live signature as
well as the installer before considering publication complete.
Keep silent upgrade support and `--self-test`
compatible in subsequent releases; updates retain model files and preferences.

The homepage and installer are one static release on the existing OVH Ubuntu
server. Nginx serves only verified public files; there is no application backend.
The game, login and database services are separate and must not be changed or
restarted as part of translator website work.

## Release together

1. Increase `app_version.py`, add matching notes at the top of
   `sites/l2-chat-translator/changelog.json`, and update relevant feature copy.
   Run `.build-tools/venv/Scripts/python.exe packaging/build.py` from the project
   root. It tests/builds the installer, checks the frozen app and prepares the page.
2. Run `packaging/release_site.py --check`, then `packaging/export_main_server.py`
   with that Python. Before creating the upload bundle, the exporter publishes
   and verifies the matching GitHub source snapshot and version tag. A failed
   source update stops the release; retry after resolving it. See
   `PUBLIC-SOURCE.md` for the source-only allowlist and privacy checks.
   The exporter validates source/asset hashes, installer bytes,
   the public allowlist and changelog. It changes only the exported page's
   canonical/og:url origin to the main server; the Sites source remains intact.
3. Upload the resulting ZIP and `.manifest.json` from `release/`, plus
   `packaging/main-server/deploy_release.py` and
   `packaging/main-server/discord_release.py`, into `/home/ubuntu/l2ct-publish/`.
   Connect as `ubuntu@40.160.140.227` using the existing identity
   `<path-to-your-private-SSH-key>`, BatchMode and StrictHostKeyChecking.
   Never copy or print key contents.
4. Run the helper on the server with `sudo -n python3`, supplying `--archive`
   and `--manifest` with their full uploaded paths. It validates every ZIP
   member against the manifest before extraction, rejects extra/private files,
   preserves old installer names/bytes, and verifies the staged files before
   atomically switching `/srv/l2-chat-translator/current`.
5. Fetch the public homepage, metadata and complete installer over trusted HTTPS.
   Check the release/version/download link and compare the downloaded bytes with
   both the manifest and `downloads/SHA256SUMS.txt`. Never execute the installer
   as part of download verification. Check that HTTP redirects to HTTPS.

The deployment helper also invokes the configured Discord release bot after
publication. It independently verifies the public release before announcing the
current version in `#updates-and-announcements`. The returned `discord` object
reports delivery separately from publication; inspect it instead of assuming a
post succeeded. A Discord failure does not roll back the website. Follow
`DISCORD.md` for private setup and safe retry handling. Never announce development
progress or repost every historical changelog entry.

Publish the homepage, changelog, metadata and installer as one bundle. A local
build is only prepared, not published. Retain previous releases; never overwrite
an installer with different bytes under the same version. Routine deployments
need no Nginx reload and no game/service restart. Tooling or website-only changes
do not require an app version bump or installer rebuild.

Server layout:

- `/srv/l2-chat-translator/releases/<release_id>/`: root-owned public files
- `/srv/l2-chat-translator/current`: atomic symlink to the active release
- `/srv/l2-chat-translator/manifests/`: deployment evidence outside the public root
- `/home/ubuntu/l2ct-publish/`: upload staging, also outside the public root
- `/etc/nginx/sites-available/l2-chat-translator`: active web configuration
- `/etc/nginx/snippets/l2ct-security-headers.conf`: response security headers

Rollback: validate the chosen previous release/manifest first, then atomically
replace `current` with a symlink to that release. Preserve any newer versioned
download URLs when rolling back homepage content. Do not delete releases or use
an unverified directory as the target.

## HTTPS and automatic renewal

The IP certificate uses Let's Encrypt's `shortlived` profile. Certbot snap 5.9.0
was installed for initial deployment; snap keeps it updated. The certificate
name is `l2-chat-translator-ip`; it covers IP address `40.160.140.227`.

- Certificate: `/etc/letsencrypt/live/l2-chat-translator-ip/fullchain.pem`
- Private key: `/etc/letsencrypt/live/l2-chat-translator-ip/privkey.pem` (root only)
- Webroot challenge directory: `/var/lib/l2ct-acme/.well-known/acme-challenge/`
- Timer: `snap.certbot.renew.timer`, enabled for twice-daily renewal checks
- Hook: `/etc/letsencrypt/renewal-hooks/deploy/l2ct-nginx.sh`

The hook checks Nginx configuration and reloads only Nginx after renewal. HTTP
port 80 must stay reachable for certificate validation; other HTTP requests
redirect to HTTPS. Port 443 serves the website. UFW retains the original SSH and
game rules. The short certificate lifetime makes working renewal essential.
Initial registration uses no email address; expiry emails are not configured.
Inspect the renewal timer/journal during server maintenance.

Test renewal without replacing the live certificate:

```sh
sudo /snap/bin/certbot renew --cert-name l2-chat-translator-ip --dry-run --run-deploy-hooks --no-random-sleep-on-renew
systemctl list-timers --all snap.certbot.renew.timer
sudo journalctl -u snap.certbot.renew.service --since '7 days ago'
```

Saved installation configurations are in `packaging/main-server/`. The bootstrap
configuration is only for initial HTTP validation. Never deploy it over the live
HTTPS configuration for a routine release. Validate any web config change with
`sudo nginx -t` before reloading Nginx. No other service needs restarting.

## Existing mirror and public content

The original public mirror remains at
https://lineage-2-chat-translator.vargas87706341.chatgpt.site
with its existing identity in `sites/l2-chat-translator/.openai/hosting.json`.
Publishing to the main server does not update that separate mirror. Use the
connected Sites workflow when updating it; preserve public access and project ID.

Source template and changelog remain in `sites/l2-chat-translator/`. Public files
are allowlisted, with no external assets or JavaScript dependencies. The page
hides technical checksum links; release metadata/checksums remain available.
Never upload settings, logs, review examples, credentials, model weights or local
validation reports. The protected Ollama frontend is bundled; native libraries and model weights download from upstream during setup.

The installer has no Windows Authenticode publisher signature. The separate
updater signature authenticates the release metadata and its installer hash to
v1.7.12+ clients independently of the download host. See UPDATE-SIGNING.md for
key custody and the first trusted upgrade. If Authenticode is added later,
sign before final manifest, checksum and updater-signature generation.
Headless checks do not replace a clean Windows install,
upgrade or uninstall test; request permission before interactive testing.
