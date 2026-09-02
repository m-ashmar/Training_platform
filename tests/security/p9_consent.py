import os, sys, django, logging
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from django.utils import timezone
from users.models import CustomUser
from ai_assistant.models import ChatSession, AITrainingData
from ai_assistant.services.data_collector import DataCollector
res=[]
def chk(k,v): res.append((k,v))
u=CustomUser.objects.create_user(email='cn@x.com',username='cn',password='Xx!23456')
u.specific_injury='sensitive condition'; u.save()
s=ChatSession.objects.create(user=u)
dc=DataCollector()
kw=dict(user=u, session=s, user_message='m', ai_response='a', tools_called=[], tool_results=[])
dc.collect(**kw) if hasattr(dc,'collect') else dc.log_interaction(**kw)
chk("no consent -> nothing retained", AITrainingData.objects.count()==0)
u.ai_training_consent=True; u.save()
dc.collect(**kw) if hasattr(dc,'collect') else dc.log_interaction(**kw)
row=AITrainingData.objects.first()
chk("with consent -> retained", row is not None)
chk("marked consented", bool(row and row.consented))
chk("retention date set", bool(row and row.retain_until))
# purge
if row:
    row.retain_until = timezone.now() - timezone.timedelta(days=1) if hasattr(timezone,'timedelta') else row.retain_until
    import datetime; row.retain_until = timezone.now() - datetime.timedelta(days=1); row.save()
from ai_assistant.tasks import purge_expired_training_data
purge_expired_training_data()
chk("expired rows purged", AITrainingData.objects.count()==0)
for k,v in res: print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{sum(1 for _,v in res if v)}/{len(res)} PASS")
r.teardown_databases(old)
