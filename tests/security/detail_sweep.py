import os, sys, re, django
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
from django.urls import get_resolver
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser

def walk(pat, prefix=''):
    out=[]
    for p in pat:
        if hasattr(p,'url_patterns'): out+=walk(p.url_patterns, prefix+str(p.pattern))
        else: out.append(prefix+str(p.pattern))
    return out
raw=walk(get_resolver().url_patterns)
detail=[x for x in raw if ('<' in x or '(?P' in x)]
print(f"total routes={len(raw)}  detail routes NEVER swept={len(detail)}")

def concretize(p):
    p='/'+p.lstrip('/')
    p=re.sub(r'\(\?P<[^>]+>[^)]*\)','1',p)
    p=re.sub(r'<[^:>]+:[^>]+>','1',p)
    p=re.sub(r'<[^>]+>','1',p)
    return p.replace('\\.','.').replace('$','').replace('^','')

users={}
for t in ('client','trainer','admin'):
    u=CustomUser.objects.create_user(email=f'{t}@d.com',username=t,password='Xx!23456')
    u.user_type=t
    if t=='admin': u.is_staff=u.is_superuser=True
    u.is_active=True; u.save(); users[t]=u

errs=[]; n=0
for t,u in users.items():
    c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
    for p in detail:
        path=concretize(p)
        if 'admin/' in path or 'swagger' in path or 'redoc' in path or '1' not in path: continue
        for meth in ('get','delete'):
            try:
                resp=getattr(c,meth)(path); n+=1
                if resp.status_code>=500: errs.append((t,meth,path,resp.status_code))
            except Exception as e:
                errs.append((t,meth,path,f'EXC {type(e).__name__}: {str(e)[:100]}'))
# anonymous too
c=Client()
for p in detail:
    path=concretize(p)
    if 'admin/' in path or 'swagger' in path or '1' not in path: continue
    try:
        resp=c.get(path); n+=1
        if resp.status_code>=500: errs.append(('anon','get',path,resp.status_code))
    except Exception as e:
        errs.append(('anon','get',path,f'EXC {type(e).__name__}: {str(e)[:100]}'))
print(f"requests={n}  5xx/exceptions={len(errs)}")
seen=set()
for e in errs:
    k=(e[2],str(e[3])[:60])
    if k in seen: continue
    seen.add(k); print("  ",e[0],e[1],e[2],"->",str(e[3])[:110])
r.teardown_databases(old)
