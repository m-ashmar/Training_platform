import os, sys, django, logging, datetime
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone
from users.models import CustomUser
from diet.models import DietPlan
u=CustomUser.objects.create_user(email='dv@x.com',username='dv',password='Xx!23456')
t=timezone.localdate()
def mk(a,b,**kw):
    return DietPlan(user=u, goal='maintain', daily_calories=2000, start_date=a, end_date=b, **kw)
res=[]
p1=mk(t, t+datetime.timedelta(days=7)); p1.is_active=True; p1.save()
res.append(("a normal plan saves", DietPlan.objects.count()==1))
try:
    bad=mk(t+datetime.timedelta(days=5), t); bad.save()
    res.append(("inverted range refused by the DB", False))
except IntegrityError:
    res.append(("inverted range refused by the DB", True))
from django.db import connection, transaction
transaction.set_rollback(False) if False else None
r2=get_runner(settings)
# fresh connection after the IntegrityError
connection.close()
p2=mk(t+datetime.timedelta(days=3), t+datetime.timedelta(days=10)); p2.is_active=True
try:
    p2.full_clean(exclude=['user']); res.append(("overlapping active plan refused", False))
except ValidationError as e:
    res.append(("overlapping active plan refused", 'active plan covering' in str(e)))
for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
r.teardown_databases(old)
