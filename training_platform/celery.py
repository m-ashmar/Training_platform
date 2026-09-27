# training_platform/celery.py
import os
from celery import Celery
from celery.signals import beat_init, task_success, worker_init

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings_production')

app = Celery('training_platform')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


# The worker and beat are a separate process group (see fly.toml) and do NOT go through
# wsgi.py/asgi.py, so they start with none of the production safety invariants checked —
# a misconfigured broker, a live WALLET_DEV_MODE or a missing encryption key would be
# caught on the web machine and silently accepted here, in the process that sends
# notifications, generates plans and runs the scheduled jobs.
#
# On a SIGNAL, not at import. This module is imported by `training_platform/__init__.py`,
# which Django must execute before it can import `training_platform.settings_production`
# — so `django.setup()` here ran INSIDE Django's own bootstrap. `populate()` is not
# reentrant: the real error from any AppConfig.ready() was swallowed into
# `settings_exception` and the operator was handed "populate() isn't reentrant" instead,
# naming Django's app registry rather than the variable that was wrong. That is the only
# log a failed release_command produces. By the time these signals fire, the Celery
# Django fixup has set Django up exactly once.
def _enforce_production_safety(**_kwargs):
    from django.conf import settings

    if getattr(settings, "SETTINGS_MODULE", "") != "training_platform.settings_production":
        return
    from training_platform.settings_production import enforce_production_safety

    enforce_production_safety()


worker_init.connect(_enforce_production_safety)
beat_init.connect(_enforce_production_safety)


# The last success of each watched beat job (settings.BACKGROUND_WATCHED_JOBS), for
# GET /api/auth/health/background/. A signal rather than code in each task, so a money
# task needs no monitoring line of its own and a new one is watched by adding its entry
# to the setting. The import is inside the receiver for the same reason as above: this
# module is imported before Django is set up.
def _record_background_success(sender=None, **_kwargs):
    from training_platform.background_health import record_success

    record_success(getattr(sender, "name", None))


task_success.connect(_record_background_success, weak=False)
# The beat schedule lives in settings (CELERY_BEAT_SCHEDULE) and is applied by
# config_from_object above. Assigning app.conf.beat_schedule here does nothing: that
# config resolves lazily, so settings overwrite the assignment on first access.