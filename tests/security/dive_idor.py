import os, sys, django, datetime, decimal, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.db import models as M, transaction
from django.urls import get_resolver
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser

def mk(t, uname, email):
    u=CustomUser.objects.create_user(email=email, username=uname, password='Xx!23456')
    u.user_type=t; u.is_active=True
    if t=='admin': u.is_staff=u.is_superuser=True
    u.save(); return u
alice=mk('client','alice','alice@x.com'); bob=mk('client','bob','bob@x.com')
tr=mk('trainer','trn','trn@x.com')

USER_FIELDS={'user','author','creator','created_by','owner','client','sender','recipient','from_user','to_user','follower'}
def fill(model, owner, depth=0):
    """Best-effort minimal instance owned by `owner`."""
    if depth>3: return None
    kw={}
    for f in model._meta.get_fields():
        if not getattr(f,'concrete',False) or f.auto_created: continue
        if getattr(f,'blank',False) and getattr(f,'null',False): continue
        if f.has_default() or getattr(f,'auto_now',False) or getattr(f,'auto_now_add',False): continue
        if isinstance(f, M.ForeignKey):
            rel=f.related_model
            if rel is CustomUser:
                kw[f.name]= owner if f.name in USER_FIELDS or 'user' in f.name else owner
            else:
                inst=rel.objects.first() or fill(rel, owner, depth+1)
                if inst is None and not f.null: return None
                kw[f.name]=inst
        elif f.null: continue
        elif isinstance(f,(M.CharField,M.TextField)):
            ch=getattr(f,'choices',None)
            kw[f.name]= ch[0][0] if ch else 'x'
        elif isinstance(f,(M.IntegerField,M.BigIntegerField,M.PositiveIntegerField,M.SmallIntegerField)): kw[f.name]=1
        elif isinstance(f,M.DecimalField): kw[f.name]=decimal.Decimal('1.00')
        elif isinstance(f,M.FloatField): kw[f.name]=1.0
        elif isinstance(f,M.BooleanField): kw[f.name]=False
        elif isinstance(f,M.DateTimeField): kw[f.name]=django.utils.timezone.now()
        elif isinstance(f,M.DateField): kw[f.name]=datetime.date.today()
        elif isinstance(f,M.JSONField): kw[f.name]={}
    try:
        with transaction.atomic():
            return model.objects.create(**kw)
    except Exception:
        return None

def walk(pat, prefix=''):
    out=[]
    for p in pat:
        if hasattr(p,'url_patterns'): out+=walk(p.url_patterns, prefix+str(p.pattern))
        else: out.append((prefix+str(p.pattern), p.callback))
    return out
import re
routes={}
for path,cb in walk(get_resolver().url_patterns):
    cls=getattr(cb,'cls',None) or getattr(cb,'view_class',None)
    if not cls or not ('<' in path or '(?P' in path): continue
    qs=getattr(cls,'queryset',None)
    model=qs.model if qs is not None else getattr(getattr(getattr(cls,'serializer_class',None),'Meta',None),'model',None)
    if model is None: continue
    # only single-param detail routes ending in the pk
    p='/'+path.lstrip('/')
    if len(re.findall(r'<[^>]+>|\(\?P<[^>]+>[^)]*\)', p))!=1: continue
    routes.setdefault((cls.__name__, model), set()).add(p)

def concrete(p, pk):
    p=re.sub(r'\(\?P<[^>]+>[^)]*\)',str(pk),p)
    p=re.sub(r'<[^:>]+:[^>]+>',str(pk),p); p=re.sub(r'<[^>]+>',str(pk),p)
    return p.replace('\\.','.').replace('$','').replace('^','')

cb_=Client(); cb_.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(bob).access_token}'
ct_=Client(); ct_.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(tr).access_token}'
anon=Client()
leaks=[]; made=0; skipped=[]
for (vs,model),paths in sorted(routes.items(), key=lambda x:x[0][0]):
    obj=fill(model, alice)
    if obj is None:
        skipped.append((vs,model.__name__)); continue
    made+=1
    for p in sorted(paths):
        path=concrete(p, obj.pk)
        for who,c in (('bob',cb_),('trainer',ct_),('anon',anon)):
            try: resp=c.get(path)
            except Exception as e:
                leaks.append((vs,path,who,'GET',f'EXC {type(e).__name__}')); continue
            if 200<=resp.status_code<300:
                leaks.append((vs,path,who,'GET',resp.status_code))
        for who,c in (('bob',cb_),):
            try:
                d=c.delete(path)
                if 200<=d.status_code<300: leaks.append((vs,path,who,'DELETE',d.status_code))
            except Exception: pass
print(f"models instantiated: {made}/{len(routes)}   (skipped: {len(skipped)})")
print(f"\n*** cross-tenant reads/writes that SUCCEEDED: {len(leaks)} ***")
for l in leaks: print("   ",l)
if skipped: print("\nnot instantiable (need manual check):", ", ".join(f"{a}/{b}" for a,b in skipped))
r.teardown_databases(old)
