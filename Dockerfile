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
# /data is where the Fly volume gets mounted (user-uploaded media). Create it and
# hand it to appuser so uploads are writable after the volume attaches.
RUN useradd -m appuser && mkdir -p /data/media /data/tmp && chown -R appuser /app /data
USER appuser

EXPOSE 8000

# Run migrations then start Daphne (ASGI for WebSockets)
CMD ["sh", "-c", "python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 training_platform.asgi:application"]
