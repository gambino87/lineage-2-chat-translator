"""Connect the release bot using a hidden token prompt on the private server."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import warnings
from urllib.parse import urlencode

from discord_release import DiscordClient

CONFIG_PATH = Path('/etc/l2ct-discord/config.json')
GUILD_ID = '1569092047321829577'  # L2 Chat Translator; confirmed via the project invite.
CHANNEL_NAME = 'updates-and-announcements'
BOT_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 14)  # View channel, send messages, embeds.


def snowflake(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{17,20}', value) or not 0 < int(value) < 2**64:
        raise ValueError('Discord returned an invalid application or channel ID.')
    return value


def invite_url(application_id):
    return 'https://discord.com/oauth2/authorize?' + urlencode({
        'client_id': snowflake(application_id), 'scope': 'bot',
        'permissions': BOT_PERMISSIONS, 'guild_id': GUILD_ID,
        'disable_guild_select': 'true',
    })


def save_private_config(config, path=CONFIG_PATH):
    """Atomic root-only configuration, outside the website and installer payload."""
    path = Path(path).absolute()
    parent = path.parent
    if parent.is_symlink() or parent.resolve() != parent or path.is_symlink():
        raise ValueError('Refusing a linked configuration path.')
    parent.mkdir(mode=0o700, exist_ok=True)
    if os.name == 'posix':
        if parent.stat().st_uid != os.geteuid() or (path.exists() and path.stat().st_uid != os.geteuid()):
            raise ValueError('Configuration must be owned by the account running setup.')
        parent.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=parent,
                                         prefix='.config-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            if os.name == 'posix':
                os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def configure(token, *, config_path=CONFIG_PATH, client_factory=DiscordClient):
    """Resolve the named channel in the fixed project guild; never read chat."""
    client = client_factory(token)
    app = client.get('/oauth2/applications/@me')
    if not isinstance(app, dict):
        raise ValueError('Could not identify the bot application.')
    application_id = snowflake(app.get('id'))
    channels = client.get(f'/guilds/{GUILD_ID}/channels')
    if not isinstance(channels, list):
        raise ValueError('Could not list the project server channels.')
    matches = [channel for channel in channels if isinstance(channel, dict)
               and channel.get('name') == CHANNEL_NAME and channel.get('type') in (0, 5)]
    if len(matches) != 1:
        raise ValueError('The bot must be invited and able to view exactly one #updates-and-announcements text or announcement channel.')
    channel = matches[0]
    if channel.get('guild_id', GUILD_ID) != GUILD_ID:
        raise ValueError('Discord channel does not belong to the project server.')
    channel_id = snowflake(channel.get('id'))
    config = {'bot_token': token, 'application_id': application_id,
              'guild_id': GUILD_ID, 'channel_id': channel_id, 'channel_name': CHANNEL_NAME}
    save_private_config(config, config_path)
    return {key: value for key, value in config.items() if key != 'bot_token'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invite', metavar='APPLICATION_ID', help='Print the minimal-permission invite link; no token needed.')
    args = parser.parse_args()
    if args.invite:
        print(invite_url(args.invite))
        return 0
    if os.name != 'posix' or os.geteuid() != 0:
        print('Run this setup on the main server with sudo.')
        return 1
    print('Connect L2 Translator Releases to L2 Chat Translator / #updates-and-announcements.')
    print('Paste the bot token at the hidden prompt. It will not appear in the terminal or command history.')
    try:
        # getpass otherwise falls back to echoed input when no terminal is available.
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('Bot token: ').strip()
        result = configure(token)
    except getpass.GetPassWarning:
        print('Setup requires an interactive terminal with a working hidden prompt. Connect with ssh -t and try again; no configuration was saved.')
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nSetup canceled; no configuration saved.')
        return 1
    except Exception:
        # External exceptions can carry authentication headers or response bodies.
        print('Could not connect the bot. Check its token, invitation and access to #updates-and-announcements. Existing configuration was preserved.')
        return 1
    print(json.dumps({'status': 'configured', **result}, indent=2))
    print('No message has been posted. The release publisher can now verify and announce the current release.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
