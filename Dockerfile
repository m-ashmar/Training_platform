# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System dependencies for psycopg2, Pillow, python-magic, and libmagic — and ffmpeg
# (2026-09-27), which the WORKER uses to cut a video's poster frame and measure it
# (`mediakit.tasks`, `chat.tasks`). The web process never runs it: nothing on a request
# decodes video. `--no-install-recommends` keeps it to the binaries and their codec
# libraries; the cost is the size of the image, roughly +250 MB uncompressed (the
# estimate DEPLOY_CHECKLIST.md carried; not measured here). Without it the workers still
# run: every video uploads and plays, with no poster (logged once per worker).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libmagic1 \
    libmagic-dev \
    libjpeg-dev \
    zlib1g-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Add daphne explicitly (ASGI server)
RUN pip install --no-cache-dir daphne

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
# There is deliberately NO `USER appuser` here. The entrypoint starts as root, creates
# the multipart spool (FILE_UPLOAD_TEMP_DIR, under /tmp) for appuser at START — a
# platform may mount a fresh tmpfs over /tmp, which would hide a directory made here at
# build time, exactly as the media volume that used to be mounted here hid its own —
# and then drops to appuser for the actual process, so nothing the app does runs as root.
# There is no volume any more: media is object storage in production (fly.toml).
RUN useradd -m appuser && chown -R appuser /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Run migrations then start Daphne (ASGI for WebSockets). fly.toml's [processes]
# replaces this CMD per process group; the ENTRYPOINT above still runs for all of them.
CMD ["sh", "-c", "python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 training_platform.asgi:application"]
