import os, sys, django
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # THIS tree: a hardcoded checkout made a worktree run the main tree's code
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
os.environ['DEBUG']='False'
django.setup()
from django.conf import settings
settings.DEBUG = False           # what production actually runs with
import importlib, training_platform.urls as u
importlib.reload(u)
pats=[str(p.pattern) for p in u.urlpatterns]
media=[p for p in pats if 'media' in p.lower()]
print("DEBUG:",settings.DEBUG)
print("MEDIA_URL:",settings.MEDIA_URL)
print("media url patterns:", media if media else "NONE  *** every uploaded file 404s ***")
print("whitenoise middleware:",[m for m in settings.MIDDLEWARE if 'hitenoise' in m])
print("WHITENOISE_ROOT:",getattr(settings,'WHITENOISE_ROOT','<unset>'))
print("USE_EXTERNAL_MEDIA_STORAGE:",getattr(settings,'USE_EXTERNAL_MEDIA_STORAGE','<unset>'))
