import os, sys, django, logging, io, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
buf=io.StringIO(); h=logging.StreamHandler(buf); h.setLevel(logging.ERROR)
logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.ERROR)
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from routine.models import Routine
u=CustomUser.objects.create_user(email='r2@x.com',username='r2',password='Xx!23456'); u.is_active=True; u.save()
tr=CustomUser.objects.create_user(email='r2t@x.com',username='r2t',password='Xx!23456'); tr.user_type='trainer'; tr.is_active=True; tr.save()
rt=Routine.objects.create(name='r',created_by=tr); rt.assigned_to.add(u)
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
for body in [{'day':1,'status':'completed','routine':rt.id},
             {'day':1,'status':'completed','routine':rt.id,'date':'2026-09-01'}]:
    buf.truncate(0); buf.seek(0)
    resp=c.post('/api/routine/routine-progress/', json.dumps(body), content_type='application/json')
    print(f"\nPOST {body} -> {resp.status_code}")
    if resp.status_code>=500:
        out=buf.getvalue().split('\n')
        proj=[l.strip() for l in out if 'Training_platform/' in l and '.venv' not in l]
        err=[l.strip() for l in out if ('Error' in l or 'Exception' in l) and ' line ' not in l]
        for l in proj[-2:]: print("   ",l)
        for l in err[-1:]: print("   >>",l)
    else: print("   ",resp.content.decode()[:160])
r.teardown_databases(old)
