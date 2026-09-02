import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.core.paginator import Paginator
from diet.models import FoodItem, FoodCategory
cat=FoodCategory.objects.create(name='c')
for i in range(60): FoodItem.objects.create(name=f'f{i:03d}', category=cat)
print("FoodItem.Meta.ordering:", FoodItem._meta.ordering, "-> no ORDER BY emitted")
qs=FoodItem.objects.all()
print("SQL contains ORDER BY:", 'ORDER BY' in str(qs.query))
seen=[]
for page in range(1,7):
    p=Paginator(FoodItem.objects.all(), 10).page(page)
    seen += [o.id for o in p.object_list]
    # realistic: any row updated while the user scrolls (Postgres moves the tuple)
    f=FoodItem.objects.order_by('id')[page*3]; f.name=f.name+'*'; f.save()
print(f"collected={len(seen)} unique={len(set(seen))} total={FoodItem.objects.count()}")
if len(seen)!=len(set(seen)):
    print("*** DUPLICATES ACROSS PAGES:", len(seen)-len(set(seen)), "— user sees repeats and misses rows ***")
    print("*** rows NEVER shown:", FoodItem.objects.count()-len(set(seen)))
else:
    print("no duplicates in this run (heap order happened to stay stable — still unguaranteed)")
r.teardown_databases(old)
