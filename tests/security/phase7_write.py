import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # THIS tree: a hardcoded checkout made a worktree run the main tree's code
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

# Two separate claims in one request, so say which one each line proves.
#
# The POST used to carry `completed_sets`/`target_sets` and check only the owner, while
# reading as though a refused write had been demonstrated. Those two fields are now
# read-only on `UserExerciseProgressSerializer` — DRF drops a read-only field silently,
# so the request is ACCEPTED and the values are ignored, and "201" proves nothing on its
# own (SIDE_BUGS S-038). The probe now reads back what was stored and asserts the values
# did not land, which is the thing that is actually true and the thing that can regress.
r=auth(attacker).post('/api/routine/user-exercise-progress/',data=json.dumps({'user':victim.id,'exercise':ex.id,'date':str(date.today()),'completed_sets':7,'target_sets':9}),content_type='application/json')
owner=None; stored=None
if r.status_code in (200,201):
    try:
        row=UserExerciseProgress.objects.get(id=r.json()['id'])
        owner=row.user_id; stored=(row.completed_sets,row.target_sets)
    except Exception: pass
print(f"  [{r.status_code}] attacker CREATE progress with user=victim -> stored owner={owner} {'*** WRONG OWNER ***' if owner==victim.id else '(coerced to self / blocked)'}")
print(f"  [{r.status_code}] attacker CREATE progress claiming completed_sets=7 target_sets=9 -> stored {stored} {'*** SELF-AUTHORED COMPLIANCE ***' if stored not in (None,(0,0)) else '(derived: ignored / blocked)'}")
runner.teardown_databases(old)
# After teardown, so a regression does not leave the probe's database behind — and as a
# real assert, so the script exits non-zero instead of printing a verdict nobody reads.
assert owner!=victim.id, "progress row was created under the victim's account"
assert stored in (None,(0,0)), f"a client authored their own compliance: {stored}"
