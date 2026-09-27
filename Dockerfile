# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System dependencies for psycopg2, Pillow, python-magic, and libmagic
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libmagic1 \
    libmagic-dev \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache).
#
# ONLY the lock, and in hash-checking mode: every package, transitive ones included, at
# the exact version the test suite passed on, and pip refuses a file whose hash differs
# or a dependency the lock does not name. This used to install requirements.txt (which
# had `>=` ranges and bare names) plus a second, unpinned `pip install daphne`, so every
# build resolved whatever was newest that day. Daphne is in the lock like everything
# else. To change a version, see the header of requirements.txt.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy project
COPY . .

# Collect static files at build time using a build-only settings module that
# needs no real secrets or AWS access. No `|| true` — a real failure must fail
# the build rather than silently ship an empty staticfiles/.
RUN DJANGO_SETTINGS_MODULE=training_platform.settings_build python manage.py collectstatic --noinput

# Runtime settings (used by the CMD: migrate + daphne)
ENV DJANGO_SETTINGS_MODULE=training_platform.settings_production

# Non-root user for security.
#
# There is deliberately NO `USER appuser` here, and this is the fix for "no upload can
# be written on the deployed app": /data is a Fly VOLUME, mounted over this path at run
# time, and a mount replaces the directory — the chown below applies to a directory the
# volume then hides, while what the process actually meets is the volume's own root,
# owned by root:root. Running as appuser from the image therefore meant EACCES on every
# write under /data, including the multipart spool (FILE_UPLOAD_TEMP_DIR=/data/tmp), so
# uploads failed before any view ran.
#
# The entrypoint starts as root, chowns the mount AFTER it exists, and then drops to
# appuser for the actual process — so nothing the app does runs as root.
RUN useradd -m appuser && mkdir -p /data/media /data/tmp && chown -R appuser /app /data
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Run migrations then start Daphne (ASGI for WebSockets). fly.toml's [processes]
# replaces this CMD per process group; the ENTRYPOINT above still runs for all of them.
CMD ["sh", "-c", "python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 training_platform.asgi:application"]
