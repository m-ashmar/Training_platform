# training_platform/celery.py
import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings_production')
app = Celery('training_platform')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
app.conf.beat_schedule = {
    'generate-daily-advice': {
        'task': 'diet.tasks.generate_daily_advice',
        'schedule': crontab(hour=6, minute=0),  # 6 AM daily
    },
}