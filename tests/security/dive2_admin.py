import os, sys, django, logging, re
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.urls import get_resolver
from django.test import Client
from users.models import CustomUser
def mk(t,u,e):
    x=CustomUser.objects.create_user(email=e,username=u,password='Xx!23456')
    x.user_type=t
    if t=='admin': x.is_staff=x.is_superuser=True
    x.is_active=True; x.save(); return x
client=mk('client','c','c@a.com'); trainer=mk('trainer','t','t@a.com')
agent=mk('agent','g','g@a.com'); admin=mk('admin','a','a@a.com')
def sess(u):
    c=Client()
    if u: c.force_login(u)
    return c
ROLES={'anonymous':sess(None),'client':sess(client),'trainer':sess(trainer),'agent':sess(agent),'admin':sess(admin)}

def walk(p,pre=''):
    o=[]
    for x in p:
        if hasattr(x,'url_patterns'): o+=walk(x.url_patterns,pre+str(x.pattern))
        else: o.append(pre+str(x.pattern))
    return o
routes=sorted({'/'+x.lstrip('/') for x in walk(get_resolver().url_patterns)
               if x.startswith('dj-admin/') and '<' not in x and '(?P' not in x})
print(f"/dj-admin/ routes discovered: {len(routes)}\n")
leaks=[]; errs=[]
for path in routes:
    row={}
    for role,c in ROLES.items():
        try:
            resp=c.get(path, follow=False); row[role]=resp.status_code
            if resp.status_code>=500: errs.append((role,path,resp.status_code))
        except Exception as e:
            row[role]='EXC'; errs.append((role,path,type(e).__name__))
    nonadmin_ok=[r for r in ('anonymous','client','trainer','agent') if isinstance(row.get(r),int) and 200<=row[r]<300]
    if nonadmin_ok: leaks.append((path,nonadmin_ok,row))
print(f"routes reachable (2xx) by NON-admin roles: {len(leaks)}")
for p,who,row in leaks[:15]: print(f"   {p:46} {who}  {row}")
print(f"\n5xx / exceptions: {len(errs)}")
for e in errs[:10]: print("   ",e)
r.teardown_databases(old)
