#!/bin/sh
set -eu
if [ "${RENEWED_LINEAGE:-}" = "/etc/letsencrypt/live/l2-chat-translator-ip" ]; then
    /usr/sbin/nginx -t
    /usr/bin/systemctl reload nginx
fi
