import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from routine.models import RoutineTemplate

templates = RoutineTemplate.objects.all()
for t in templates:
    print(t.id, t.name)
