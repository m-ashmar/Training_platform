import os, sys, django, logging, threading
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from django.db import connections
from django.utils import timezone
from users.models import CustomUser
from routine.models import Routine, Exercise, UserExerciseProgress, ExerciseSetLog
u=CustomUser.objects.create_user(email='rc@x.com',username='rc',password='Xx!23456')
tr=CustomUser.objects.create_user(email='rct@x.com',username='rct',password='Xx!23456'); tr.user_type='trainer'; tr.save()
ex=Exercise.objects.create(name='Bench', created_by=tr, is_global=True)
prog=UserExerciseProgress.objects.create(user=u, exercise=ex, date=timezone.localdate())

N=12
def log_set(i):
    try:
        ExerciseSetLog.objects.create(user_exercise_progress=prog, set_number=i+1, weight=50, reps=10)
    except Exception as e:
        print("   thread error:", type(e).__name__, str(e)[:50])
    finally:
        connections.close_all()
import sys as _s
MODE=_s.argv[1] if len(_s.argv)>1 else 'concurrent'
if MODE=='sequential':
    for i in range(N): log_set(i)
else:
    ths=[threading.Thread(target=log_set,args=(i,)) for i in range(N)]
    [t.start() for t in ths]; [t.join() for t in ths]
prog.refresh_from_db()
actual_sets=ExerciseSetLog.objects.filter(user_exercise_progress=prog).count()
print(f"### {MODE} set logging of 12 identical sets ###")
print(f"   set logs actually stored     : {actual_sets}")
for f in ('sets_count','total_sets','total_weight','total_reps'):
    if hasattr(prog,f): print(f"   progress.{f:14} = {getattr(prog,f)}")
expected_weight = 50*10*actual_sets
tw=getattr(prog,'total_weight',None)
if tw is not None:
    print(f"   expected total_weight        : {expected_weight}")
    print("   *** LOST UPDATE — recalc raced, aggregate is wrong ***" if abs((tw or 0)-expected_weight)>1e-6
          else "   aggregate consistent")
r.teardown_databases(old)
