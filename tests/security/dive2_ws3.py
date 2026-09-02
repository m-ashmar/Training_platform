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
import training_platform.cache as C

u=CustomUser.objects.create_user(email='rl@x.com',username='rl',password='Xx!23456'); u.is_active=True; u.save()
plan=SubscriptionPlan.objects.create(name='Pro',description='d',price=decimal.Decimal('20'),has_ai_advice=True)
Subscription.objects.create(user=u, plan=plan, status='active', end_date=timezone.now()+datetime.timedelta(days=30))
tok=str(RefreshToken.for_user(u).access_token)

class DeadCache:
    """Simulates the ratelimit Redis (DB1) being unreachable."""
    def incr(self,*a,**k): raise ConnectionError("Redis DB1 unreachable")
    def set(self,*a,**k):  raise ConnectionError("Redis DB1 unreachable")
    def get(self,*a,**k):  raise ConnectionError("Redis DB1 unreachable")
    def decr(self,*a,**k): raise ConnectionError("Redis DB1 unreachable")

async def run(label, patch):
    if patch: C.ratelimit_cache = lambda: DeadCache()
    comm=WebsocketCommunicator(application, f'/ws/ai/chat/?token={tok}')
    ok,_s = await asyncio.wait_for(comm.connect(), timeout=8)
    if not ok: print(f"{label}: could not connect"); return
    await asyncio.wait_for(comm.receive_from(), timeout=5)   # welcome
    await comm.send_to(text_data=json.dumps({'type':'message','content':'hi'}))
    try:
        resp=await asyncio.wait_for(comm.receive_from(), timeout=8)
        s=str(resp)
        verdict = "*** FAILS OPEN — LLM invoked with no working limiter ***" if 'rate_limit' not in s else "failed closed (rate_limit error)"
        print(f"{label}: {s[:90]}\n   -> {verdict}")
    except Exception as e:
        print(f"{label}: {type(e).__name__} -> connection torn down => FAILS CLOSED (safe)")
    try:
        await comm.disconnect()
    except Exception:
        pass

async def main():
    await run("ratelimit cache HEALTHY ", patch=False)
    await run("ratelimit cache DEAD    ", patch=True)
asyncio.run(main())
r.teardown_databases(old)
