import os, sys, django, logging, traceback, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from users.models import CustomUser
u=CustomUser.objects.create_user(email='an@x.com',username='an',password='Xx!23456'); u.is_active=True; u.save()

import importlib, inspect
mods = ['ai_assistant.analyzers.training_analyzer','ai_assistant.analyzers.diet_analyzer',
        'ai_assistant.analyzers.behavior_profiler']
print("### analyzers run against a BRAND NEW user (no workouts, no meals, no history) ###")
for name in mods:
    m=importlib.import_module(name)
    for attr, obj in vars(m).items():
        if attr.startswith('_') or not inspect.isclass(obj) or obj.__module__!=name: continue
        try:
            inst = obj()
        except Exception:
            try: inst = obj(u)
            except Exception as e:
                print(f"  {attr}: cannot construct ({type(e).__name__})"); continue
        for meth in [x for x in dir(inst) if not x.startswith('_') and callable(getattr(inst,x))]:
            fn=getattr(inst,meth)
            try: sig=inspect.signature(fn)
            except Exception: continue
            params=[p for p in sig.parameters.values() if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD,)]
            try:
                out = fn(u) if len(params)==1 else (fn() if not params else None)
                if out is None and params: continue
                blob=json.dumps(out, default=str)[:70]
                print(f"  [ok]   {attr}.{meth:26} -> {blob}")
            except ZeroDivisionError as e:
                print(f"  [FAIL] {attr}.{meth:26} *** ZeroDivisionError on empty data ***")
            except Exception as e:
                tb=traceback.format_exc().strip().split('\n')
                proj=[l.strip() for l in tb if 'Training_platform/' in l and '.venv' not in l]
                print(f"  [FAIL] {attr}.{meth:26} *** {type(e).__name__}: {str(e)[:50]}")
                if proj: print(f"         {proj[-1][:100]}")
r.teardown_databases(old)
