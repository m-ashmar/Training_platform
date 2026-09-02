import os, sys, django, logging, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from ai_assistant.models import ChatSession, ChatMessage, AITrainingData, UserBehaviorEvent, UserInsight, UsageCost
from ai_assistant.services.data_collector import DataCollector
from ai_assistant.services.context_compiler import ContextCompiler

u=CustomUser.objects.create_user(email='pv@x.com',username='pv',password='Xx!23456')
u.is_active=True; u.height=175; u.weight=88; u.age=34; u.gender='male'
u.specific_injury='HIV positive, lower back hernia'   # special-category health data
u.save()
s=ChatSession.objects.create(user=u)
ctx=ContextCompiler().compile(u, flagged_input=False)
DataCollector().collect(user=u, session=s, context=ctx, user_message='does my condition affect squats?',
                        tools_called=[], tool_results={}, ai_response='...', response_tokens=10, response_latency_ms=5) \
    if hasattr(DataCollector,'collect') else None
if AITrainingData.objects.count()==0:
    AITrainingData.objects.create(user=u, session=s, user_context_snapshot=ctx,
        user_message='does my condition affect squats?', tools_called=[], tool_results={},
        ai_response='...', response_tokens=10, response_latency_ms=5)
row=AITrainingData.objects.first()
print("### what is retained for model training ###")
snap=json.dumps(row.user_context_snapshot, default=str)
print("  contains injury/medical text :", 'hernia' in snap.lower() or 'hiv' in snap.lower())
print("  contains weight/height       :", '88' in snap and '175' in snap)
print("  consent flag on the record   :", [f for f in AITrainingData._meta.get_fields() if 'consent' in f.name] or "NONE")
from ai_assistant.tasks import purge_expired_training_data
print("  retention/cleanup task       :", "purge_expired_training_data")
print("  retain_until set on the row  :", bool(row.retain_until))

print("\n### GDPR delete ###")
UserBehaviorEvent.objects.create(user=u, event_type='x', event_data={})
UserInsight.objects.create(user=u, insight_type='training', content={}, expires_at=django.utils.timezone.now())
before={m.__name__: m.objects.filter(**({'user':u} if any(f.name=='user' for f in m._meta.get_fields()) else {'session__user':u})).count()
        for m in (ChatSession, ChatMessage, AITrainingData, UserBehaviorEvent, UserInsight, UsageCost)}
print("  before:", before)
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
resp=c.delete('/api/ai/data/')
after={m.__name__: m.objects.filter(**({'user':u} if any(f.name=='user' for f in m._meta.get_fields()) else {'session__user':u})).count()
       for m in (ChatSession, ChatMessage, AITrainingData, UserBehaviorEvent, UserInsight, UsageCost)}
print(f"  DELETE /api/ai/data/ -> {resp.status_code} {resp.content.decode()[:90]}")
print("  after :", after)
leftover={k:v for k,v in after.items() if v}
print(("  [FAIL] data surviving deletion: %s" % leftover) if leftover else "  [PASS] nothing survives deletion")
r.teardown_databases(old)
