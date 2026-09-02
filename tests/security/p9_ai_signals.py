import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
settings.DEBUG=True
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from django.db import connection, reset_queries
from django.utils import timezone
from users.models import CustomUser
from routine.models import Routine, WorkoutSession, Exercise, UserExerciseProgress, ExerciseSetLog
from ai_assistant.models import UserBehaviorEvent
u=CustomUser.objects.create_user(email='sg@x.com',username='sg',password='Xx!23456'); u.is_active=True; u.save()
tr=CustomUser.objects.create_user(email='sgt@x.com',username='sgt',password='Xx!23456'); tr.user_type='trainer'; tr.save()
rt=Routine.objects.create(name='r', created_by=tr); rt.assigned_to.add(u)
ex=Exercise.objects.create(name='Bench', created_by=tr, is_global=True)

print("### 1. does re-saving a completed session duplicate the behaviour event? ###")
ws=WorkoutSession.objects.create(user=u, routine=rt, status='in_progress')
ws.status='completed'; ws.end_time=timezone.now(); ws.save()
n1=UserBehaviorEvent.objects.filter(event_type='workout_completed').count()
ws.notes='edited once'; ws.save()
ws.notes='edited twice'; ws.save()
n2=UserBehaviorEvent.objects.filter(event_type='workout_completed').count()
print(f"   after completing        : {n1} event(s)")
print(f"   after 2 unrelated PATCHes: {n2} event(s)")
print("   *** DUPLICATED — analytics inflate on every save ***" if n2>n1 else "   ok (guarded)")

print("\n### 2. query cost of logging sets (AI signal fires per set) ###")
prog=UserExerciseProgress.objects.create(user=u, exercise=ex, date=timezone.localdate())
def cost(n):
    reset_queries()
    for i in range(n):
        ExerciseSetLog.objects.create(user_exercise_progress=prog, set_number=i+1, weight=50, reps=10)
    return len(connection.queries)
q5=cost(5); q20=cost(20)
print(f"   5 sets  -> {q5} queries")
print(f"   20 sets -> {q20} queries   ({q20/20:.1f} per set)")
ev=UserBehaviorEvent.objects.filter(event_type='set_logged').count()
print(f"   behaviour events written: {ev} (one per set)")

print("\n### 3. does a failing AI signal break the user's primary write? ###")
# Make the REAL receiver's body fail (not a stand-in), so the guard is actually exercised.
from ai_assistant import models as AM
orig_create = AM.UserBehaviorEvent.objects.create
def boom_create(*a, **k): raise RuntimeError("analytics backend down")
AM.UserBehaviorEvent.objects.create = boom_create
try:
    ws2 = WorkoutSession.objects.create(user=u, routine=rt, status='in_progress')
    ws2.status='completed'; ws2.save()
    ws2.refresh_from_db()
    print(f"   user's workout saved anyway (status={ws2.status}) -> failure isolated")
except Exception as e:
    print(f"   *** {type(e).__name__}: {e}")
    print("   *** analytics failure still blocks the user's workout ***")
finally:
    AM.UserBehaviorEvent.objects.create = orig_create

print("\n### 4. where do the 5 queries per set actually go? ###")
reset_queries()
ExerciseSetLog.objects.create(user_exercise_progress=prog, set_number=99, weight=50, reps=10)
for q in connection.queries:
    print("   ", q['sql'][:104])
r.teardown_databases(old)
