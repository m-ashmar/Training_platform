import sys; sys.path.insert(0,"/Users/mac/Desktop/Git/t2/Training_platform")
import os, django, json
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import setup_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); r=DiscoverRunner(verbosity=0,interactive=False); old=r.setup_databases()
from django.test import Client
from users.models import CustomUser
from routine.models import Exercise, RoutineTemplate
from rest_framework_simplejwt.tokens import RefreshToken

def mk(u,e,t='client'):
    x=CustomUser.objects.create_user(email=e,username=u,password="P@ssw0rd!123",user_type=t)
    x.is_active=True; x.save(); return x
trA=mk('trA','a@ex.com','trainer'); trB=mk('trB','b@ex.com','trainer'); cli=mk('cli','c@ex.com')
def auth(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c

# trainer A's PRIVATE custom exercise
Exercise.objects.create(name='TRAINER-A-SECRET-LIFT', description='private', created_by=trA)
Exercise.objects.create(name='GlobalPushup', description='global')   # created_by = NULL

# trainer A's PUBLIC template
tpl = RoutineTemplate.objects.create(name='A-PUBLIC-TEMPLATE', created_by=trA, is_public=True, goal='hypertrophy')

print("FINDING 1: does a CLIENT see another trainer's private exercise?")
rb = auth(cli).get('/api/routine/exercises/')
print(f"  [client]  {'*** SEES TRAINER-A-SECRET-LIFT ***' if 'TRAINER-A-SECRET-LIFT' in rb.content.decode() else 'correctly hidden'}")
rb2 = auth(trB).get('/api/routine/exercises/')
print(f"  [trainerB]{'  SEES it' if 'TRAINER-A-SECRET-LIFT' in rb2.content.decode() else '  correctly hidden'}")

print("\nFINDING 2: can trainerB modify/delete trainerA's PUBLIC template?")
rp = auth(trB).patch(f'/api/routine/templates/{tpl.id}/', data=json.dumps({'name':'HIJACKED'}), content_type='application/json')
print(f"  [{rp.status_code}] trainerB PATCH A's template -> {'*** ALLOWED ***' if rp.status_code in (200,202) else 'blocked'}")
if rp.status_code==200:
    tpl.refresh_from_db(); print(f"        template name is now: {tpl.name!r}")
rd = auth(trB).delete(f'/api/routine/templates/{tpl.id}/')
print(f"  [{rd.status_code}] trainerB DELETE A's template -> {'*** ALLOWED ***' if rd.status_code in (204,200) else 'blocked'}")
print(f"        template still exists: {RoutineTemplate.objects.filter(id=tpl.id).exists()}")
r.teardown_databases(old)
