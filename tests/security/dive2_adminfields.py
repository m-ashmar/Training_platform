import os, sys, django
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.contrib import admin as dj
import admin_dashboard.admin as AD
sites=[]
for name,obj in vars(AD).items():
    if isinstance(obj, dj.AdminSite): sites.append((name,obj))
print("custom admin sites:", [n for n,_ in sites] or "(none found as module attr)")
targets=[]
for name,site in sites:
    for model, adm in site._registry.items(): targets.append((model, adm))
if not targets:
    for model, adm in dj.site._registry.items(): targets.append((model, adm))
print(f"registered model admins: {len(targets)}\n")
bad=[]
for model, adm in targets:
    concrete={f.name for f in model._meta.get_fields()}
    concrete |= {f.name+'_id' for f in model._meta.get_fields() if hasattr(f,'attname')}
    def check(attr, names):
        for n in names:
            if not isinstance(n,str): continue
            base=n.lstrip('-')
            if base in concrete: continue
            if hasattr(adm, base) or hasattr(model, base): continue
            if '__' in base: continue
            bad.append((type(adm).__name__, model.__name__, attr, base))
    fs=getattr(adm,'fieldsets',None)
    if fs:
        for _t,opt in fs: check('fieldsets', opt.get('fields',()) )
    for attr in ('list_display','list_filter','readonly_fields','search_fields','filter_horizontal','ordering'):
        v=getattr(adm,attr,None)
        if v: check(attr, v)
for b in sorted(set(bad)):
    print(f"   {b[0]:28} {b[1]:22} {b[2]:18} -> '{b[3]}' does not exist")
print(f"\ntotal invalid field references: {len(set(bad))}")
