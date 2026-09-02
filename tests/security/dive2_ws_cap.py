import os, sys, django, logging, asyncio, json, decimal, datetime
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
settings.CHANNEL_LAYERS={'default':{'BACKEND':'channels.layers.InMemoryChannelLayer'}}
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from channels.testing import WebsocketCommunicator
from training_platform.asgi import application
from django.utils import timezone
from users.models import CustomUser
from subscription.models import Subscription, SubscriptionPlan
from rest_framework_simplejwt.tokens import RefreshToken
u=CustomUser.objects.create_user(email='cap@x.com',username='cap',password='Xx!23456'); u.is_active=True; u.save()
plan=SubscriptionPlan.objects.create(name='Pro',description='d',price=decimal.Decimal('20'),has_ai_advice=True)
Subscription.objects.create(user=u, plan=plan, status='active', end_date=timezone.now()+datetime.timedelta(days=30))
tok=str(RefreshToken.for_user(u).access_token)
async def one(content, label):
    comm=WebsocketCommunicator(application, f'/ws/ai/chat/?token={tok}')
    ok,_s=await asyncio.wait_for(comm.connect(), timeout=8)
    await asyncio.wait_for(comm.receive_from(), timeout=5)   # welcome
    await comm.send_to(text_data=json.dumps({'type':'message','content':content}))
    try:
        resp=await asyncio.wait_for(comm.receive_from(), timeout=8)
        d=json.loads(resp)
        print(f"  {label:26} first reply: type={d.get('type')} code={d.get('code')} -> {str(d.get('content'))[:60]}")
    except Exception as e:
        print(f"  {label:26} {type(e).__name__}")
    try: await comm.disconnect()
    except Exception: pass
async def main():
    print("MAX_MESSAGE_CHARS =", getattr(settings,'AI_ASSISTANT_CONFIG',{}).get('MAX_MESSAGE_CHARS', 4000))
    await one('hello', 'normal message')
    await one('A'*200000, '200KB message')
asyncio.run(main())
r.teardown_databases(old)
