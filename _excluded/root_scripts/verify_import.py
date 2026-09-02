import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

print("Attempting to import routine.urls...")
try:
    import routine.urls
    print("✅ routine.urls imported successfully!")
    print("URL Patterns:")
    for pat in routine.urls.urlpatterns:
        print(f" - {pat.pattern}")
except Exception as e:
    print(f"❌ Failed to import routine.urls: {e}")
    import traceback
    traceback.print_exc()
