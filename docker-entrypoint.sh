#!/bin/sh
# Make the upload spool writable, then drop to the unprivileged user.
#
# FILE_UPLOAD_TEMP_DIR is where Django buffers a multipart body before any view runs.
# If the app user cannot write it, every upload fails before a validator sees it, which
# looks like a server fault rather than a rejected file. It is made HERE, at container
# start and as root, rather than in the image: a platform may mount a fresh tmpfs over
# /tmp, and a mount hides whatever the image put at that path. (The same trap, with the
# Fly volume that used to be mounted for media, is why no upload could be written on the
# first deploy; there is no volume any more — media is object storage in production.)
#
# It must never exec the app as root: check the exec lines below before changing them.
set -e

APP_USER=appuser

if [ "$(id -u)" = "0" ]; then
    spool="${FILE_UPLOAD_TEMP_DIR:-}"
    case "$spool" in
        # Unset, or a shared system directory: nothing to create, and chowning /tmp
        # itself to the app user would take it away from everything else.
        ""|/|/tmp|/tmp/) ;;
        *)
            mkdir -p "$spool"
            chown "$APP_USER:$APP_USER" "$spool" || true
            chmod 700 "$spool" || true
            ;;
    esac

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
