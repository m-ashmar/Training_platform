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
from subscription.models import Subscription, SubscriptionPlan
from ai_assistant.models import ChatSession, ChatMessage, AITrainingData
from django.utils import timezone
import datetime, decimal
u=CustomUser.objects.create_user(email='fb@x.com',username='fb',password='Xx!23456'); u.is_active=True; u.save()
plan=SubscriptionPlan.objects.create(name='P',description='d',price=decimal.Decimal('1'),has_ai_advice=True)
Subscription.objects.create(user=u, plan=plan, status='active', end_date=timezone.now()+datetime.timedelta(days=30))
s=ChatSession.objects.create(user=u)
# two assistant replies that begin identically — utterly normal for a coaching bot
SHARED = "Great question! Based on your recent training data, here is what I would suggest for your next session: "
m1=ChatMessage.objects.create(session=s, role='assistant', content=SHARED+"increase squat volume.")
m2=ChatMessage.objects.create(session=s, role='assistant', content=SHARED+"reduce deadlift volume.")
t1=AITrainingData.objects.create(user=u, session=s, message=m1, user_context_snapshot={}, user_message='a',
    tools_called=[], tool_results={}, ai_response=m1.content, response_tokens=1, response_latency_ms=1)
t2=AITrainingData.objects.create(user=u, session=s, message=m2, user_context_snapshot={}, user_message='b',
    tools_called=[], tool_results={}, ai_response=m2.content, response_tokens=1, response_latency_ms=1)
c=Client(); c.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(u).access_token}'
resp=c.post('/api/ai/feedback/', json.dumps({'message_id': m1.id, 'feedback': 'positive'}), content_type='application/json')
print("POST /api/ai/feedback/ ->", resp.status_code, resp.content.decode()[:90])
t1.refresh_from_db(); t2.refresh_from_db()
print(f"\n  feedback was for message {m1.id} only")
print(f"  training row for m1: user_feedback={t1.user_feedback!r}")
print(f"  training row for m2: user_feedback={t2.user_feedback!r}")
if t2.user_feedback:
    print("  *** feedback applied to the WRONG record too — matching is by the first 100 chars ***")
    print("  *** of ai_response, not by a foreign key, so replies sharing a preamble collide ***")
r.teardown_databases(old)
