import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # THIS tree: a hardcoded checkout made a worktree run the main tree's code
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); r=DiscoverRunner(verbosity=0,interactive=False); old=r.setup_databases()
from django.test import Client
from users.models import CustomUser
from routine.models import Exercise, ExerciseMedia
from rest_framework_simplejwt.tokens import RefreshToken
def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="P@ssw0rd!123",user_type=t); x.is_active=True; x.save(); return x
cli=mk('cli','c@ex.com'); adm=mk('adm','a@ex.com','admin'); tr=mk('tr','t@ex.com','trainer')
def auth(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c
gl=Exercise.objects.create(name='GlobalSquat', description='x', is_global=True)
own=Exercise.objects.create(name='TrainerLift', description='x', created_by=tr)
P={'media_items':[{'media_type':'photo','content':'http://x/img.png','title':'t'}]}
def post(u,ex): return auth(u).post(f'/api/routine/exercises/{ex.id}/add-media/', data=json.dumps(P), content_type='application/json')

r1=post(cli,gl);  print(f"  [{r1.status_code}] CLIENT add-media on GLOBAL   -> {'*** ALLOWED ***' if r1.status_code in (200,201,207) and 'permission' not in r1.content.decode() else 'BLOCKED'}")
r2=post(adm,gl);  print(f"  [{r2.status_code}] ADMIN  add-media on GLOBAL   -> {'allowed (correct)' if r2.status_code in (200,201,207) else 'BLOCKED (regression!)'}")
print(f"        media rows after admin: {ExerciseMedia.objects.filter(exercise=gl).count()}  errors={json.loads(r2.content).get('errors')}")
r3=post(tr,own);  print(f"  [{r3.status_code}] TRAINER add-media on OWN     -> {'allowed (correct)' if r3.status_code in (200,201,207) else 'BLOCKED (regression!)'}")
print(f"        media rows on own: {ExerciseMedia.objects.filter(exercise=own).count()}")
# 404 as well as 403 since 2026-09-20: `cli` has no relation with `tr`, so the exercise is
# not visible to them at all and the endpoint answers exactly as it does for an id that
# does not exist — a 403 here would confirm the row exists. Deliberate contract change,
# covered by tests/test_exercise_media_visibility.py.
r4=post(cli,own); print(f"  [{r4.status_code}] CLIENT add-media on OTHERS'  -> {'BLOCKED (correct)' if r4.status_code in (403,404) else '*** ALLOWED ***'}")
r.teardown_databases(old)
