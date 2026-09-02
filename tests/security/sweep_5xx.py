import os, sys, django
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ.setdefault('DJANGO_SETTINGS_MODULE','training_platform.settings_local')
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
from django.urls import get_resolver
from django.test import Client
from users.models import CustomUser

users={}
for t in ('client','trainer','admin','agent'):
    u=CustomUser.objects.create_user(email=f'{t}@s.com',username=t,password='Xx!23456')
    u.user_type=t
    if t=='admin': u.is_staff=u.is_superuser=True
    u.is_active=True; u.save(); users[t]=u

def walk(pat, prefix=''):
    out=[]
    for p in pat:
        if hasattr(p,'url_patterns'):
            out+=walk(p.url_patterns, prefix+str(p.pattern))
        else:
            out.append(prefix+str(p.pattern))
    return out

routes=[r_ for r_ in walk(get_resolver().url_patterns) if '<' not in r_ and '(?P' not in r_]
routes=sorted(set('/'+r_.lstrip('/') for r_ in routes))
errs=[]; tested=0
for t,u in users.items():
    c=Client(); c.force_login(u)
    for path in routes:
        if 'admin/' in path or 'swagger' in path or 'redoc' in path: continue
        try:
            resp=c.get(path)
            tested+=1
            if resp.status_code>=500:
                errs.append((t,path,resp.status_code))
        except Exception as e:
            errs.append((t,path,f'EXC {type(e).__name__}: {e}'))
print(f"routes={len(routes)} requests={tested} 5xx/exceptions={len(errs)}")
for e in errs[:25]: print("  ",e)
r.teardown_databases(old)
