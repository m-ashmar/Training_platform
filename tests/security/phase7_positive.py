import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); runner=DiscoverRunner(verbosity=0,interactive=False); old=runner.setup_databases()
from django.test import Client
from users.models import CustomUser, TrainerClientRelation
from routine.models import Routine, Exercise, RoutineExercise, UserExerciseProgress, ExerciseSetLog, WorkoutSession, RoutineProgress
from rest_framework_simplejwt.tokens import RefreshToken
from datetime import date

def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="P@ssw0rd!123",user_type=t,phone_number="0000000000")
    x.is_active=True; x.save(); return x
client=mk('cli','c@ex.com'); trainer=mk('tr','t@ex.com','trainer'); admin=mk('adm','ad@ex.com','admin')
client.assigned_trainer=trainer; client.save()
TrainerClientRelation.objects.create(trainer=trainer, client=client, status='approved')

ex=Exercise.objects.create(name='Squat',description='x')
rt=Routine.objects.create(name='R',created_by=trainer,days=1); rt.assigned_to.add(client)
RoutineExercise.objects.create(routine=rt, exercise=ex, day=1)
prog=UserExerciseProgress.objects.create(user=client,exercise=ex,date=date.today(),completed_sets=3,target_sets=3)
ExerciseSetLog.objects.create(user_exercise_progress=prog,set_number=1,weight=225,reps=5,date=date.today())
sess=WorkoutSession.objects.create(user=client, routine=rt, status='active')
RoutineProgress.objects.filter(user=client, routine=rt).update(status='completed')

def auth(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c
C,T,A = auth(client), auth(trainer), auth(admin)

def ok(label, resp, expect_data=None):
    good = resp.status_code in (200,201)
    if good and expect_data: good = expect_data in resp.content.decode()
    print(f"  [{resp.status_code}] {'PASS' if good else '*** REGRESSION ***'}  {label}")
    if not good: print(f"        {resp.content[:120]}")

print("LEGITIMATE ACCESS MUST STILL WORK\n")
ok("client reads OWN analytics summary", C.get('/api/routine/analytics/summary/'))
ok("client reads OWN streaks",           C.get('/api/routine/analytics/streaks/'))
ok("client reads OWN trends",            C.get('/api/routine/analytics/trends/'))
ok("client lists OWN workout-sessions",  C.get('/api/routine/workout-sessions/'), '"user":%d'%client.id)
ok("client lists OWN routine-exercises", C.get('/api/routine/routine-exercises/'), 'Squat')
ok("client PATCHes OWN session",         C.patch(f'/api/routine/workout-sessions/{sess.id}/',data=json.dumps({'status':'completed'}),content_type='application/json'))
print()
ok("TRAINER reads THEIR client analytics", T.get(f'/api/routine/analytics/summary/?user_id={client.id}'))
ok("TRAINER reads THEIR client streaks",   T.get(f'/api/routine/analytics/streaks/?user_id={client.id}'))
ok("TRAINER lists sessions (sees client)", T.get('/api/routine/workout-sessions/'), '"user":%d'%client.id)
ok("TRAINER PATCHes THEIR client session", T.patch(f'/api/routine/workout-sessions/{sess.id}/',data=json.dumps({'status':'completed'}),content_type='application/json'))
ok("TRAINER creates session for client",   T.post('/api/routine/workout-sessions/',data=json.dumps({'user':client.id,'routine':rt.id,'status':'active'}),content_type='application/json'))
print()
ok("ADMIN reads any user analytics",  A.get(f'/api/routine/analytics/summary/?user_id={client.id}'))
ok("ADMIN lists all sessions",        A.get('/api/routine/workout-sessions/'), '"user":%d'%client.id)
ok("ADMIN completion (all users)",    A.get('/api/routine/analytics/completion/'), f'"user_id":{client.id}')
runner.teardown_databases(old)
