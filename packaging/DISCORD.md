# Discord release announcements

The project bot posts **published releases only** in **L2 Chat Translator** /
**#updates-and-announcements**. It posts a self-contained text write-up with the
version and corresponding public patch notes only after the release is verified
live. Omit availability boilerplate and update instructions. Notes appear directly in the message with no
generated links or link-card embeds. There is no game integration and no
continuously running bot process.

The bot is connected on the main server. Application ID: `1569200661868978177`;
announcement channel ID: `1569188825673244844`. The initial v1.7.10 announcement
was accepted by Discord as message `1569203053406457988`; an immediate repeat
returned `already_posted` without sending a duplicate. Credentials remain only
in the private server configuration below; these IDs are not secrets.

## One-time Discord setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application named **L2 Translator Releases**. Its bot user supplies
   the posting identity. The project icon is available as `assets/L2CT.png`.
2. Install it into the project server using the **bot** scope and only
   **View Channels**, **Send Messages**, and **Embed Links** permissions. No
   administrator permission or privileged gateway intents are needed. Limit its
   channel access to `#updates-and-announcements` through the server's permissions.
   `configure_discord.py --invite APPLICATION_ID` prints a link restricted to the
   project server with those permissions. The project guild ID was checked using
   the public `V4ph4Ww5m` invite: `1569092047321829577`.
3. Upload `configure_discord.py` and `discord_release.py` from `main-server/` to
   `/home/ubuntu/l2ct-publish/`. Connect using an interactive SSH terminal (`ssh -t`)
   so token entry can stay hidden, then run:

   ```sh
   sudo python3 /home/ubuntu/l2ct-publish/configure_discord.py
   ```

   Paste the bot token at the hidden prompt. Do not send it in chat or put it in
   a command argument. Setup identifies the application and resolves the exact
   channel name in the fixed project guild, refusing ambiguous matches. If a hidden
   prompt is unavailable, setup refuses to read the token.
4. Configuration is saved to `/etc/l2ct-discord/config.json` with root-only access.
   It is outside the public web root, workspace and installer. Setup itself does
   not post. Then verify/preview the current release:

   ```sh
   sudo python3 /home/ubuntu/l2ct-publish/discord_release.py --current --preview
   ```

   To post that current release once:

   ```sh
   sudo python3 /home/ubuntu/l2ct-publish/discord_release.py --current
   ```

## Release workflow

Upload `discord_release.py` alongside `deploy_release.py` on future releases.
Successful publication invokes the announcement automatically. The bot verifies
the current deployment and the public HTTPS homepage, release metadata and full
installer checksum before posting. It reads the current release notes from the
verified homepage, so the website and announcement use the same changes.

The bot checks the configured application and target channel identity, disables
mentions and persists duplicate protection privately under `/var/lib/l2ct-discord`.
It posts each channel/version once; page-only edits do not reannounce an unchanged
version. No previous releases are caught up automatically. Bot authentication is
sent only to the fixed Discord API origin, never to the download host.

When the user requests a format correction, run `discord_release.py --current
--edit-existing` with sudo. It re-verifies the release and edits only the existing
message recorded as sent for that channel/version, clearing the old embed. It
never creates a replacement if that message is missing or an edit fails. Ordinary
publication still returns `already_posted` for an unchanged version. The full
write-up must fit Discord's 2000-character limit; oversized notes are rejected
without silently dropping changes.

A failed Discord notification is reported separately from a successful website
publication. Keep the installed release live and resolve the notification issue.
An ambiguous send may already have reached Discord; its pending marker prevents
blind resending. Inspect the channel before resolving such a marker. Do not delete
delivery state or rotate the token merely to force another announcement.

Discord permission and API references:
[bot authorization](https://docs.discord.com/developers/topics/oauth2#bot-authorization-flow),
[permissions](https://docs.discord.com/developers/topics/permissions),
[message creation](https://docs.discord.com/developers/resources/message#create-message).
