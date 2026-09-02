import os, sys, django, logging, json, decimal
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from routine.models import Exercise, Routine
from subscription.models import SubscriptionPlan
u=CustomUser.objects.create_user(email='fk@x.com',username='fk',password='Xx!23456'); u.is_active=True; u.save()
tr=CustomUser.objects.create_user(email='fktr@x.com',username='fktr',password='Xx!23456'); tr.user_type='trainer'; tr.is_active=True; tr.save()
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
ct=Client(); ct.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(tr).access_token}'
ex=Exercise.objects.create(name='e', created_by=tr, is_global=True)
rt=Routine.objects.create(name='r', created_by=tr); rt.assigned_to.add(u)
plan=SubscriptionPlan.objects.create(name='P', description='d', price=decimal.Decimal('10'))
CASES=[
 (c,  '/api/routine/routine-progress/',        {'day':1,'status':'completed','routine':rt.id}),
 (ct, '/api/routine/routine-exercises/',       {'exercise':ex.id,'routine':rt.id,'day':1,'sets':3,'reps':10}),
 (c,  '/api/subscription/v1/subscriptions/',   {'plan':plan.id}),
 (c,  '/api/social/follows/',                  {'following':tr.id}),
]
print(f"{'endpoint':42} {'status':>7}   outcome")
for cli,path,body in CASES:
    resp=cli.post(path, json.dumps(body), content_type='application/json')
    out = '*** 500 — endpoint can never succeed ***' if resp.status_code>=500 else (
          'created' if 200<=resp.status_code<300 else f'{resp.content.decode()[:70]}')
    print(f"{path:42} {resp.status_code:>7}   {out}")
r.teardown_databases(old)
