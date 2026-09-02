import os, sys, django, logging, json, re, datetime, decimal
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.urls import get_resolver
from django.test import Client
from rest_framework import serializers as S
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser

def mk(t,u,e):
    x=CustomUser.objects.create_user(email=e,username=u,password='Xx!23456')
    x.user_type=t
    if t=='admin': x.is_staff=x.is_superuser=True
    x.is_active=True; x.save(); return x
alice=mk('client','alice','a@w.com'); bob=mk('client','bob','b@w.com')
trainer=mk('trainer','trn','t@w.com'); admin=mk('admin','adm','ad@w.com')
def cl(u):
    c=Client()
    if u: c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
    return c
CLIENTS={'owner':cl(alice),'other-client':cl(bob),'trainer':cl(trainer),'admin':cl(admin),'anon':cl(None)}

def gen(field):
    """Best-effort valid value for one writable serializer field."""
    if isinstance(field,(S.CharField,S.EmailField,S.URLField,S.SlugField)):
        if isinstance(field,S.EmailField): return 'x@example.com'
        if isinstance(field,S.URLField): return 'https://example.com'
        return 'x'
    if isinstance(field,S.ChoiceField):
        ch=list(field.choices.keys()); return ch[0] if ch else 'x'
    if isinstance(field,S.BooleanField): return False
    if isinstance(field,S.IntegerField): return max(1, field.min_value or 1)
    if isinstance(field,(S.FloatField,S.DecimalField)): return 1
    if isinstance(field,S.DateTimeField): return django.utils.timezone.now().isoformat()
    if isinstance(field,S.DateField): return str(datetime.date.today())
    if isinstance(field,S.ListField): return []
    if isinstance(field,S.DictField) or isinstance(field,S.JSONField): return {}
    if isinstance(field,S.PrimaryKeyRelatedField):
        qs=getattr(field,'queryset',None)
        o=qs.first() if qs is not None else None
        return o.pk if o else None
    return None

def payload_for(view_cls):
    sc = getattr(view_cls,'serializer_class',None)
    if sc is None: return None
    try: inst=sc()
    except Exception: return None
    out={}
    for name,f in getattr(inst,'fields',{}).items():
        if f.read_only: continue
        if isinstance(f,(S.FileField,S.ImageField)): continue
        if not f.required and name not in ('content','name','title'): continue
        v=gen(f)
        if v is not None: out[name]=v
    return out

def walk(pat, prefix=''):
    o=[]
    for p in pat:
        if hasattr(p,'url_patterns'): o+=walk(p.url_patterns, prefix+str(p.pattern))
        else: o.append((prefix+str(p.pattern), p.callback))
    return o

listr, detailr = [], []
for path,cb in walk(get_resolver().url_patterns):
    cls=getattr(cb,'cls',None) or getattr(cb,'view_class',None)
    if not cls: continue
    p='/'+path.lstrip('/')
    if 'admin/' in p or 'swagger' in p or 'redoc' in p or 'format' in p: continue
    nparams=len(re.findall(r'<[^>]+>|\(\?P<[^>]+>[^)]*\)', p))
    if nparams==0: listr.append((p,cls))
    elif nparams==1: detailr.append((p,cls))

def concrete(p,pk=1):
    p=re.sub(r'\(\?P<[^>]+>[^)]*\)',str(pk),p); p=re.sub(r'<[^:>]+:[^>]+>',str(pk),p)
    p=re.sub(r'<[^>]+>',str(pk),p); return p.replace('\\.','.').replace('$','').replace('^','')

errors=[]; n=0
for label,routes,methods in (('CREATE',listr,['post']), ('UPDATE',detailr,['patch','put'])):
    for p,cls in routes:
        body=payload_for(cls)
        if body is None: continue
        path=concrete(p)
        for role,c in CLIENTS.items():
            for m in methods:
                try:
                    resp=getattr(c,m)(path, json.dumps(body), content_type='application/json'); n+=1
                except Exception as e:
                    errors.append(('EXC',label,m,path,role,f'{type(e).__name__}: {str(e)[:70]}')); continue
                if resp.status_code>=500:
                    errors.append(('5XX',label,m,path,role,resp.status_code))
print(f"write requests issued: {n}")
print(f"5xx / exceptions: {len(errors)}")
seen=set()
for e in errors:
    k=(e[3],e[2],str(e[5])[:40])
    if k in seen: continue
    seen.add(k); print("  ",e[0],e[2].upper(),e[3],"|",e[4],"->",str(e[5])[:100])
r.teardown_databases(old)
