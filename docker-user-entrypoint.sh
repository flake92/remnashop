#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    for writable_path in \
        /opt/remnashop/assets \
        /opt/remnashop/backups \
        /opt/remnashop/logs \
        /opt/remnashop/tmp
    do
        mkdir -p "$writable_path"
        # This root path is used only by the one-shot storage initializer.
        # Check nested entries too: the volume root can be correctly owned while
        # a restored file below it is still root-owned.
        find "$writable_path" \
            \( ! -user remnashop -o ! -group remnashop \) \
            -exec chown -h remnashop:remnashop {} +
    done
    exec su-exec remnashop "$@"
fi

exec "$@"
