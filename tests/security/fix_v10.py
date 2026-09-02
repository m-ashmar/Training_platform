import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
import admin_dashboard.admin as AD
from django.contrib.admin import AdminSite
from users.models import CustomUser
site=[v for v in vars(AD).values() if isinstance(v,AdminSite)][0]
adm=site._registry[CustomUser]
su=CustomUser.objects.create_user(email='s@x.com',username='s',password='Xx!23456'); su.is_staff=su.is_superuser=True; su.save()
class R: pass
req=R(); req.user=su; req.GET={}; req.method='GET'
Form=adm.get_form(req, obj=None, change=False)
res=[]
res.append(("password is NOT an editable admin field", 'password' not in Form.base_fields))
victim=CustomUser.objects.create_user(email='v@x.com',username='v',password='Orig!12345')
orig_hash=victim.password
# exercise the reset action
from django.contrib.admin.models import LogEntry
class Req2:
    user=su
    def __init__(self): self._messages=[]
req2=Req2()
adm.message_user=lambda *a, **k: None
adm.reset_passwords(req2, CustomUser.objects.filter(pk=victim.pk))
victim.refresh_from_db()
res.append(("reset makes the password unusable", not victim.has_usable_password()))
res.append(("no shared plaintext is stored", 'testpass123' not in victim.password))
res.append(("old hash replaced", victim.password != orig_hash))
res.append(("an admin LogEntry is recorded", LogEntry.objects.filter(object_id=str(victim.pk)).exists()))
for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{sum(1 for _,v in res if v)}/{len(res)} PASS")
r.teardown_databases(old)
