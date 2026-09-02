import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment()
runner=DiscoverRunner(verbosity=0, interactive=False)
old=runner.setup_databases()

from django.test import Client
from users.models import CustomUser
from routine.models import Routine, Exercise, RoutineExercise, UserExerciseProgress, ExerciseSetLog, WorkoutSession, RoutineProgress
from rest_framework_simplejwt.tokens import RefreshToken
from datetime import date

def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="Str0ngPass!x",user_type=t,phone_number="0000000000")
    x.is_active=True; x.save(); return x

victim   = mk('victim','victim@ex.com')
attacker = mk('attacker','attacker@ex.com')
trainer  = mk('trainerX','tr@ex.com','trainer')

# --- private data belonging ONLY to victim ---
ex = Exercise.objects.create(name='SECRET-Squat', description='x')
rt = Routine.objects.create(name='VICTIM-PRIVATE-ROUTINE', created_by=trainer, days=3)
rt.assigned_to.add(victim)
RoutineExercise.objects.create(routine=rt, exercise=ex, day=1)
prog = UserExerciseProgress.objects.create(user=victim, exercise=ex, date=date.today(), completed_sets=3, target_sets=3)
ExerciseSetLog.objects.create(user_exercise_progress=prog, set_number=1, weight=225, reps=5, date=date.today())
WorkoutSession.objects.create(user=victim, routine=rt, status='completed')
RoutineProgress.objects.filter(user=victim, routine=rt).update(status='completed')

def auth(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c
A=auth(attacker)
print(f"victim={victim.id} attacker={attacker.id}  (attacker has NO relationship to victim)\n")

def probe(label,url,needles):
    r=A.get(url); b=r.content.decode()
    leaked=[n for n in needles if n in b]
    verdict="*** LEAKED: "+", ".join(leaked)+" ***" if leaked else ("no data" if r.status_code==200 else "blocked")
    print(f"  [{r.status_code}] {label}\n        {verdict}\n        {b[:150]}")

probe("GET analytics/summary/?user_id=victim",   f"/api/routine/analytics/summary/?user_id={victim.id}", ['"days_trained":1','225'])
probe("GET analytics/streaks/?user_id=victim",   f"/api/routine/analytics/streaks/?user_id={victim.id}", ['"max_streak":1','"current_streak":1'])
probe("GET analytics/trends/?user_id=victim",    f"/api/routine/analytics/trends/?user_id={victim.id}", ['225','volume'])
probe("GET analytics/completion/ (no params)",   "/api/routine/analytics/completion/", [f'"user_id": {victim.id}', f'"user_id":{victim.id}'])
probe("GET workout-sessions/ (list all)",        "/api/routine/workout-sessions/", ['VICTIM-PRIVATE-ROUTINE', f'"user":{victim.id}','completed'])
probe("GET routine-exercises/ (list all)",       "/api/routine/routine-exercises/", ['SECRET-Squat'])
probe("GET public-profile/<victim>/",            f"/api/social/users/public-profile/{victim.id}/", ['victim@ex.com'])
runner.teardown_databases(old)
