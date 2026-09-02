import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); runner=DiscoverRunner(verbosity=0,interactive=False); old=runner.setup_databases()
from django.test import Client
from users.models import CustomUser
from routine.models import Routine, Exercise, RoutineExercise, WorkoutSession, UserExerciseProgress
from rest_framework_simplejwt.tokens import RefreshToken
from datetime import date

def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="Str0ngPass!x",user_type=t,phone_number="0000000000")
    x.is_active=True; x.save(); return x
victim=mk('victim','v@ex.com'); attacker=mk('attacker','a@ex.com')
trainerA=mk('trA','tra@ex.com','trainer'); trainerB=mk('trB','trb@ex.com','trainer')
victim.assigned_trainer=trainerA; victim.save()

ex=Exercise.objects.create(name='Squat',description='x')
rt=Routine.objects.create(name='R',created_by=trainerA,days=1); rt.assigned_to.add(victim)
sess=WorkoutSession.objects.create(user=victim, routine=rt, status='active')

def auth(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c

print("WRITE-SIDE PROBES  (victim's trainer = trainerA; trainerB is UNRELATED)\n")
r=auth(attacker).patch(f'/api/routine/workout-sessions/{sess.id}/',data=json.dumps({'status':'completed'}),content_type='application/json')
print(f"  [{r.status_code}] attacker(client) PATCH victim's session -> {'BLOCKED' if r.status_code in (403,404) else '*** ALLOWED ***'}")

r=auth(trainerB).patch(f'/api/routine/workout-sessions/{sess.id}/',data=json.dumps({'status':'completed'}),content_type='application/json')
print(f"  [{r.status_code}] UNRELATED trainerB PATCH victim's session -> {'BLOCKED' if r.status_code in (403,404) else '*** ALLOWED ***'}")

r=auth(trainerB).post('/api/routine/workout-sessions/',data=json.dumps({'user':victim.id,'routine':rt.id,'status':'active'}),content_type='application/json')
print(f"  [{r.status_code}] UNRELATED trainerB CREATE session for victim -> {'BLOCKED' if r.status_code in (403,404,400) else '*** ALLOWED ***'}  {r.content[:80]}")

r=auth(attacker).post('/api/routine/user-exercise-progress/',data=json.dumps({'user':victim.id,'exercise':ex.id,'date':str(date.today()),'completed_sets':1,'target_sets':1}),content_type='application/json')
owner=None
if r.status_code in (200,201):
    try: owner=UserExerciseProgress.objects.get(id=r.json()['id']).user_id
    except Exception: pass
print(f"  [{r.status_code}] attacker CREATE progress with user=victim -> stored owner={owner} {'*** WRONG OWNER ***' if owner==victim.id else '(coerced to self / blocked)'}")
runner.teardown_databases(old)
