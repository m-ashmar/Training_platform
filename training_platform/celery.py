# training_platform/celery.py
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings_production')
# The worker is a separate process group (see fly.toml) and does NOT go through
# wsgi.py/asgi.py, so it was starting with none of the production safety invariants
# checked — a misconfigured broker, a live WALLET_DEV_MODE or a missing encryption key
# would have been caught on the web machine and silently accepted here, in the process
# that sends notifications, generates plans and moves the scheduled jobs.
if os.environ.get("DJANGO_SETTINGS_MODULE") == "training_platform.settings_production":
    import django

    django.setup()
    from training_platform.settings_production import enforce_production_safety

    enforce_production_safety()

app = Celery('training_platform')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
# The beat schedule lives in settings (CELERY_BEAT_SCHEDULE) and is applied by
# config_from_object above. Assigning app.conf.beat_schedule here does nothing: that
# config resolves lazily, so settings overwrite the assignment on first access.