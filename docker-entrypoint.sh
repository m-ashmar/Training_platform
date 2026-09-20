#!/bin/sh
# Make the mounted volume writable, then drop to the unprivileged user.
#
# THE FAILURE THIS FIXES: no upload could be written on the deployed app at all.
# The Dockerfile created /data and chowned it to appuser at BUILD time, and then Fly
# mounted the volume over that path at RUN time. A mount replaces the directory: what
# the process sees is the volume's own filesystem root, which is root:root mode 0755,
# created by Fly and never touched by the image. `USER appuser` therefore met a
# directory it could not write, so
#   * every upload failed with EACCES on MEDIA_ROOT=/data/media, and
#   * FILE_UPLOAD_TEMP_DIR=/data/tmp meant the multipart parser could not even buffer
#     the request body — the failure happened before any view or validator ran, which
#     is why it looked like a server fault rather than a rejected file.
# The chown has to happen AFTER the mount, which means at container start, which means
# as root — so the image no longer sets USER and this script drops privileges itself.
# It must never exec the app as root: check the exec lines below before changing them.
set -e

APP_USER=appuser

if [ "$(id -u)" = "0" ]; then
    # Only the mount point and the two directories the app writes. NOT `chown -R` over
    # the whole volume: that walks every stored file on every boot, and the files that
    # are already there were written by this same user.
    for dir in "${MEDIA_ROOT:-/data/media}" "${FILE_UPLOAD_TEMP_DIR:-/data/tmp}"; do
        mkdir -p "$dir"
        chown "$APP_USER:$APP_USER" "$dir" || true
    done
    # The mount point itself, so anything else the app creates directly under it works.
    chown "$APP_USER:$APP_USER" /data 2>/dev/null || true

    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups -- "$@"
    fi
    # Fallback for an image without util-linux. `su` re-parses the command through a
    # shell, so arguments containing spaces would be re-split — every command this
    # image runs (daphne, celery, manage.py) is plain words.
    exec su -s /bin/sh "$APP_USER" -c "$*"
fi

# Already unprivileged (a local `docker run --user`, or a platform that sets one).
exec "$@"
