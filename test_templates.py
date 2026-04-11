import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from routine.models import RoutineTemplate
from routine.serializers import RoutineTemplateSerializer
from users.models import CustomUser
from django.test import RequestFactory

factory = RequestFactory()
request = factory.get('/api/routine/templates/')
user = CustomUser.objects.first()
request.user = user

templates = RoutineTemplate.objects.all()
serializer = RoutineTemplateSerializer(templates, many=True, context={'request': request})
data = serializer.data
print("First serialization: OK")

data2 = serializer.data
print("Second serialization: OK")
