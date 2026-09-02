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
from wallet.models import Wallet
u=CustomUser.objects.create_user(email='esc@x.com',username='esc',password='Xx!23456')
u.user_type='client'; u.is_active=True; u.save()
w,_=Wallet.objects.get_or_create(owner=u); w.balance=decimal.Decimal('5.00'); w.save()
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'

PROFILE='/api/auth/user/update/'
ATTACKS=[
  ("become admin",            {'user_type':'admin'}),
  ("become staff",            {'is_staff':True}),
  ("become superuser",        {'is_superuser':True}),
  ("self-verify as trainer",  {'trainer_is_verified':True}),
  ("set own hourly rate",     {'trainer_hourly_rate':'999.00'}),
  ("assign myself a trainer", {'assigned_trainer':1}),
  ("reactivate/deactivate",   {'is_active':False}),
  ("mark onboarding done",    {'is_onboarding_completed':True}),
]
print(f"{'attack':30} {'status':>7}   result")
for label,body in ATTACKS:
    resp=c.post(PROFILE, json.dumps(body), content_type="application/json")
    u.refresh_from_db()
    got={'user_type':u.user_type,'is_staff':u.is_staff,'is_superuser':u.is_superuser,
         'trainer_is_verified':getattr(u,'trainer_is_verified',None),
         'trainer_hourly_rate':str(getattr(u,'trainer_hourly_rate',None)),
         'assigned_trainer':u.assigned_trainer_id,'is_active':u.is_active}
    key=list(body)[0]
    applied = str(got.get(key)).lower()==str(body[key]).lower()
    print(f"{label:30} {resp.status_code:>7}   {'*** APPLIED — ESCALATION ***' if applied else 'rejected/ignored'}")

print("\n--- wallet balance writable through any API? ---")
for path,body in [('/api/wallet/me/', {'balance':'999999.00'}),
                  ('/api/auth/user/update/', {'wallet':{'balance':'999999.00'}})]:
    resp=c.patch(path, json.dumps(body), content_type='application/json')
    w.refresh_from_db()
    print(f"  PATCH {path:32} -> {resp.status_code}  balance now {w.balance}")
r.teardown_databases(old)
