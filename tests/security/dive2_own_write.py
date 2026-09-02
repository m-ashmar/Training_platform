import os, sys, django, logging, json, datetime
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
def mk(u,e):
    x=CustomUser.objects.create_user(email=e,username=u,password='Xx!23456'); x.is_active=True; x.save(); return x
alice=mk('alice','a@o.com'); bob=mk('bob','b@o.com')
def cl(u):
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'; return c
cb=cl(bob)
from analytics.models import UserActivity, PerformanceMetric, UserGoal, UserSession, AnalyticsDashboard
from notifications.models import UserNotificationPreference

def req(model, **kw):
    """create an ALICE-owned row, filling required fields generically"""
    from django.db import models as M
    data=dict(kw)
    for f in model._meta.get_fields():
        if not getattr(f,'concrete',False) or f.auto_created or f.name in data: continue
        if f.has_default() or getattr(f,'auto_now',False) or getattr(f,'auto_now_add',False): continue
        if f.null or getattr(f,'blank',False): continue
        if isinstance(f,M.ForeignKey):
            data[f.name]=alice if f.related_model is CustomUser else f.related_model.objects.first()
        elif isinstance(f,(M.CharField,M.TextField)):
            data[f.name]=(f.choices[0][0] if f.choices else 'x')
        elif isinstance(f,(M.IntegerField,M.FloatField,M.DecimalField)): data[f.name]=1
        elif isinstance(f,M.BooleanField): data[f.name]=False
        elif isinstance(f,M.DateTimeField): data[f.name]=timezone.now()
        elif isinstance(f,M.DateField): data[f.name]=timezone.localdate()
        elif isinstance(f,M.JSONField): data[f.name]={}
    return model.objects.create(**data)

CASES=[('/api/analytics/activities/', UserActivity),
       ('/api/analytics/metrics/',    PerformanceMetric),
       ('/api/analytics/goals/',      UserGoal),
       ('/api/analytics/sessions/',   UserSession),
       ('/api/analytics/dashboard/',  AnalyticsDashboard),
       ('/api/notifications/preferences/', UserNotificationPreference)]
print("Alice owns the object; BOB attempts to read / modify / delete it\n")
print(f"{'endpoint':38} {'GET':>5} {'PATCH':>6} {'DELETE':>7}   verdict")
for base, model in CASES:
    try: obj=req(model, user=alice) if any(f.name=='user' for f in model._meta.get_fields()) else req(model)
    except Exception as e:
        print(f"{base:38} could not construct: {type(e).__name__} {str(e)[:50]}"); continue
    url=f"{base}{obj.pk}/"
    g=cb.get(url).status_code
    p=cb.patch(url, json.dumps({}), content_type='application/json').status_code
    d=cb.delete(url).status_code
    bad=[]
    if 200<=g<300: bad.append('READ')
    if 200<=p<300: bad.append('WRITE')
    if 200<=d<300: bad.append('DELETE')
    verdict = ('*** BOB CAN '+'/'.join(bad)+" ALICE'S ROW ***") if bad else 'blocked'
    print(f"{base:38} {g:>5} {p:>6} {d:>7}   {verdict}")
r.teardown_databases(old)
