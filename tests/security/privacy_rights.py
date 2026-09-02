import os, sys, django, logging, json, datetime
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
import decimal
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from social.models import Post
from wallet.models import Wallet
from training_platform import privacy
res=[]
u=CustomUser.objects.create_user(email='pz@x.com',username='pz',password='Xx!23456')
u.is_active=True; u.specific_injury='lower back hernia'; u.phone_number='+963999'; u.save()
Post.objects.create(author=u, content='mine', post_type='text', visibility='public')
w,_=Wallet.objects.get_or_create(owner=u); w.balance=decimal.Decimal('250.00'); w.save()
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'

exp=c.get('/api/privacy/export/')
body=exp.content.decode()
res.append(("export returns 200", exp.status_code==200))
res.append(("export is a download", 'attachment' in exp.get('Content-Disposition','')))
res.append(("export is not cacheable", exp.get('Cache-Control')=='no-store'))
res.append(("export contains the profile", 'lower back hernia' in body))
res.append(("export contains their posts", '"mine"' in body or 'mine' in body))
data=json.loads(body); res.append((f"export covers {len(data['sections'])} sections", len(data['sections'])>=40))

prev=c.get('/api/privacy/erase/')
res.append(("erase preview works without deleting", prev.status_code==200 and Post.objects.count()==1))
unconfirmed=c.delete('/api/privacy/erase/')
res.append(("erase refuses without confirmation", unconfirmed.status_code==400 and Post.objects.count()==1))
done=c.delete('/api/privacy/erase/?confirm=ERASE')
u.refresh_from_db(); w.refresh_from_db()
res.append(("erase succeeds when confirmed", done.status_code==200))
res.append(("posts deleted", Post.objects.count()==0))
res.append(("profile anonymised", u.email.startswith('retired+') and u.specific_injury==''))
res.append(("account deactivated", not u.is_active))
res.append(("WALLET BALANCE PRESERVED", w.balance==decimal.Decimal('250.00')))
for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{sum(1 for _,v in res if v)}/{len(res)} PASS")
r.teardown_databases(old)
