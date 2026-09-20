import os, sys, django
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # THIS tree: a hardcoded checkout made a worktree run the main tree's code
os.environ.setdefault('DJANGO_SETTINGS_MODULE','training_platform.settings_local')
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
from django.urls import get_resolver
from django.test import Client

def walk(pat, prefix=''):
    out=[]
    for p in pat:
        if hasattr(p,'url_patterns'): out+=walk(p.url_patterns, prefix+str(p.pattern))
        else: out.append(prefix+str(p.pattern))
    return out
routes=sorted(set('/'+x.lstrip('/') for x in walk(get_resolver().url_patterns)
                  if '<' not in x and '(?P' not in x))
c=Client(); errs=[]; leaked=[]
for path in routes:
    if 'admin/' in path or 'swagger' in path or 'redoc' in path: continue
    for meth in ('get','post'):
        try:
            resp=getattr(c,meth)(path)
            if resp.status_code>=500: errs.append((meth,path,resp.status_code))
            # anything 2xx to an anonymous caller is a potential data leak
            elif meth=='get' and 200<=resp.status_code<300: leaked.append(path)
        except Exception as e:
            errs.append((meth,path,f'EXC {type(e).__name__}: {str(e)[:80]}'))
print(f"ANON routes={len(routes)} 5xx/exceptions={len(errs)}")
for e in errs[:20]: print("  5XX",e)
print(f"\nanonymous 2xx GET endpoints={len(leaked)}")
for p in sorted(set(leaked)): print("   200",p)
r.teardown_databases(old)
