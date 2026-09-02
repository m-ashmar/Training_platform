import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); r=DiscoverRunner(verbosity=0,interactive=False); old=r.setup_databases()
from django.test import Client
from users.models import CustomUser, TrainerClientRelation
from routine.models import Exercise, RoutineTemplate, Routine, RoutineExercise
from rest_framework_simplejwt.tokens import RefreshToken
def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="P@ssw0rd!123",user_type=t)
    x.is_active=True; x.save(); return x
trA=mk('trA','a@ex.com','trainer'); trB=mk('trB','b@ex.com','trainer'); cli=mk('cli','c@ex.com')
cli.assigned_trainer=trA; cli.save()
TrainerClientRelation.objects.create(trainer=trA, client=cli, status='approved')
def auth(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c

glob=Exercise.objects.create(name='GlobalPushup', description='g')
mine=Exercise.objects.create(name='MyTrainersLift', description='x', created_by=trA)
other=Exercise.objects.create(name='OtherTrainerLift', description='x', created_by=trB)
rt=Routine.objects.create(name='R', created_by=trA, days=1); rt.assigned_to.add(cli)
RoutineExercise.objects.create(routine=rt, exercise=other, day=1)  # assigned via routine
tpl=RoutineTemplate.objects.create(name='A-TPL', created_by=trA, is_public=True, goal='hypertrophy')

def chk(label, cond): print(f"  {'PASS' if cond else '*** REGRESSION ***'}  {label}")
body=auth(cli).get('/api/routine/exercises/').content.decode()
chk("client sees GLOBAL exercise", 'GlobalPushup' in body)
chk("client sees THEIR trainer's exercise", 'MyTrainersLift' in body)
chk("client sees exercise in THEIR assigned routine", 'OtherTrainerLift' in body)

rb=auth(trB).get(f'/api/routine/templates/{tpl.id}/')
chk("trainerB can READ A's public template", rb.status_code==200)
ra=auth(trA).patch(f'/api/routine/templates/{tpl.id}/', data=json.dumps({'name':'A-RENAMED'}), content_type='application/json')
chk("trainerA can EDIT their OWN template", ra.status_code==200)
rc=auth(trB).post(f'/api/routine/templates/{tpl.id}/copy/')
chk(f"trainerB can COPY A's public template (got {rc.status_code})", rc.status_code in (200,201))
rd=auth(trA).delete(f'/api/routine/templates/{tpl.id}/')
chk("trainerA can DELETE their OWN template", rd.status_code in (200,204))
r.teardown_databases(old)
