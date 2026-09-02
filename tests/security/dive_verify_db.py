import os, sys, django, decimal, datetime, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.ERROR)
from django.db.models import ProtectedError
from django.core.paginator import Paginator
from django.utils import timezone
from users.models import CustomUser
from wallet.models import Wallet, Transaction
from subscription.models import Subscription, Payment, SubscriptionPlan
from diet.models import FoodItem, FoodCategory

print("### F-02: user deletion can no longer erase the ledger ###")
u=CustomUser.objects.create_user(email='v@x.com',username='v',password='Xx!23456')
w,_=Wallet.objects.get_or_create(owner=u); w.balance=decimal.Decimal('250.00'); w.save()
plan=SubscriptionPlan.objects.create(name='P',description='d',price=decimal.Decimal('20'))
sub=Subscription.objects.create(user=u, plan=plan, end_date=timezone.now()+datetime.timedelta(days=30))
pay=Payment.objects.create(subscription=sub, amount=decimal.Decimal('20'), description='p')
try:
    u.delete(); print("  *** STILL DELETED — FIX FAILED ***")
except ProtectedError as e:
    print("  user.delete() blocked with ProtectedError  OK")
print(f"  wallet intact={Wallet.objects.filter(pk=w.pk).exists()} balance={Wallet.objects.get(pk=w.pk).balance}")
print(f"  payments intact={Payment.objects.count()}  subscriptions intact={Subscription.objects.count()}")

print("\n### F-04: pagination no longer repeats or hides rows ###")
cat=FoodCategory.objects.create(name='c')
for i in range(60): FoodItem.objects.create(name=f'f{i:03d}', category=cat)
print("  FoodItem.Meta.ordering:", FoodItem._meta.ordering)
print("  SQL has ORDER BY:", 'ORDER BY' in str(FoodItem.objects.all().query))
seen=[]
for page in range(1,7):
    p=Paginator(FoodItem.objects.all(), 10).page(page)
    seen += [o.id for o in p.object_list]
    f=FoodItem.objects.order_by('id')[page*3]; f.name=f.name+'*'; f.save()
total=FoodItem.objects.count()
print(f"  collected={len(seen)} unique={len(set(seen))} total={total}")
print(f"  duplicates={len(seen)-len(set(seen))}  never-shown={total-len(set(seen))}"
      f"   {'OK' if len(seen)==len(set(seen)) and total==len(set(seen)) else '*** STILL BROKEN ***'}")
r.teardown_databases(old)
