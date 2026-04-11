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

# Collect static files at build time
ENV DJANGO_SETTINGS_MODULE=training_platform.settings_production
RUN python manage.py collectstatic --noinput || true

# Non-root user for security
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Run migrations then start Daphne (ASGI for WebSockets)
CMD ["sh", "-c", "python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 training_platform.asgi:application"]
