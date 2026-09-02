import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); r=DiscoverRunner(verbosity=0,interactive=False); old=r.setup_databases()
from django.test import Client
from django.utils import timezone
from users.models import CustomUser
from routine.models import *
from rest_framework_simplejwt.tokens import RefreshToken
from datetime import timedelta, date

u=CustomUser.objects.create_user(email='c@ex.com',username='cli',password="P@ssw0rd!123",user_type='client'); u.is_active=True; u.save()
tr=CustomUser.objects.create_user(email='t@ex.com',username='tr',password="P@ssw0rd!123",user_type='trainer'); tr.is_active=True; tr.save()
ex=Exercise.objects.create(name='Squat',description='x')
rt=Routine.objects.create(name='PPL', created_by=tr, days=3); rt.assigned_to.add(u)
RoutineExercise.objects.create(routine=rt, exercise=ex, day=1)
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
today=timezone.localdate()

print("=== A: repeated training over 4 weeks now keeps history ===")
RoutineProgress.objects.all().delete()
for wk in range(4):
    d = today - timedelta(days=7*wk)
    RoutineProgress.objects.update_or_create(user=u, routine=rt, day=1, date=d, defaults={'status':'completed'})
n=RoutineProgress.objects.filter(user=u,routine=rt,day=1).count()
print(f"  rows after 4 weekly sessions: {n}   {'PASS' if n==4 else '*** FAIL ***'}")

print("\n=== B: user stopped 30 days ago -> current_streak must be 0 ===")
RoutineProgress.objects.all().delete()
base=today-timedelta(days=30)
for i in range(3):
    RoutineProgress.objects.create(user=u,routine=rt,day=i+1,date=base+timedelta(days=i),status='completed')
j=c.get('/api/routine/analytics/streaks/').json()
print(f"  {j}   {'PASS' if j['current_streak']==0 and j['max_streak']==3 else '*** FAIL ***'}")

print("\n=== B2: user trained today + yesterday -> current_streak = 2 ===")
RoutineProgress.objects.all().delete()
for i in [1,0]:
    RoutineProgress.objects.create(user=u,routine=rt,day=i+1,date=today-timedelta(days=i),status='completed')
j=c.get('/api/routine/analytics/streaks/').json()
print(f"  {j}   {'PASS' if j['current_streak']==2 else '*** FAIL ***'}")

print("\n=== C: editing an old record no longer moves the workout ===")
RoutineProgress.objects.all().delete()
rp=RoutineProgress.objects.create(user=u,routine=rt,day=1,date=today-timedelta(days=100),status='completed')
before=c.get('/api/routine/analytics/streaks/').json()
rp.notes='trainer note'; rp.save()
after=c.get('/api/routine/analytics/streaks/').json()
print(f"  before={before['last_training_date']} after={after['last_training_date']}   {'PASS' if before==after else '*** FAIL ***'}")

print("\n=== D: input validation ===")
def bulk(label,payload,expect_reject=True):
    resp=c.post('/api/routine/set-logs/bulk-create/', data=json.dumps(payload), content_type='application/json')
    rejected = resp.status_code==400
    print(f"  [{resp.status_code}] {label}  {'PASS' if rejected==expect_reject else '*** FAIL ***'}")
base={'routine_id':rt.id,'day':1,'date':str(today),'sets':1,'weight':50,'reps':5}
bulk("negative weight", {**base,'weight':-500})
bulk("weight 999999",   {**base,'weight':999999})
bulk("reps 100000",     {**base,'reps':100000})
bulk("future date",     {**base,'date':'2030-01-01'})
bulk("date 1900",       {**base,'date':'1900-01-01'})
bulk("500 sets",        {**base,'sets':500})
bulk("nonexistent day", {**base,'day':99})
bulk("VALID request",   base, expect_reject=False)

print("\n=== E: corrections now apply (not silently discarded) ===")
c.post('/api/routine/set-logs/bulk-create/', data=json.dumps({**base,'weight':60}), content_type='application/json')
w=ExerciseSetLog.objects.filter(user_exercise_progress__user=u, set_number=1, date=today).first()
print(f"  stored weight after correcting 50 -> 60: {w.weight}   {'PASS' if w.weight==60 else '*** FAIL ***'}")

print("\n=== F: completing a session stamps end_time / duration ===")
sess=WorkoutSession.objects.create(user=u, routine=rt, status='active')
c.patch(f'/api/routine/workout-sessions/{sess.id}/', data=json.dumps({'status':'completed'}), content_type='application/json')
sess.refresh_from_db()
print(f"  end_time={sess.end_time is not None} duration={sess.duration}   {'PASS' if sess.end_time and sess.duration is not None else '*** FAIL ***'}")
r.teardown_databases(old)
