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
u=CustomUser.objects.create_user(email='lc@x.com',username='lc',password='Xx!23456'); u.is_active=True; u.save()
plan=SubscriptionPlan.objects.create(name='P',description='d',price=decimal.Decimal('1'),has_ai_advice=True)
sub=Subscription.objects.create(user=u, plan=plan, status='active', end_date=timezone.now()+datetime.timedelta(days=30))
tok=str(RefreshToken.for_user(u).access_token)

async def main():
    print("### 1. subscription CANCELLED mid-session — does the socket keep working? ###")
    comm=WebsocketCommunicator(application, f'/ws/ai/chat/?token={tok}')
    ok,_s=await asyncio.wait_for(comm.connect(), timeout=8)
    await asyncio.wait_for(comm.receive_from(), timeout=5)
    print("   connected while subscribed:", ok)
    from channels.db import database_sync_to_async
    @database_sync_to_async
    def cancel():
        sub.status='cancelled'; sub.has_ai_advice=False; sub.save()
    await cancel()
    print("   subscription now:", 'cancelled')
    await comm.send_to(text_data=json.dumps({'type':'message','content':'still there?'}))
    try:
        resp=await asyncio.wait_for(comm.receive_from(), timeout=8)
        d=json.loads(resp)
        served = d.get('type') != 'error'
        print(f"   reply: {str(d)[:80]}")
        print("   *** ENTITLEMENT ONLY CHECKED AT CONNECT — a held-open socket keeps free access ***" if served
              else "   refused after cancellation (re-checked per message)")
    except Exception as e:
        print(f"   {type(e).__name__} -> socket closed (re-checked)")
    try: await comm.disconnect()
    except Exception: pass

    print("\n### 2. do two concurrent sockets share one rate-limit budget? ###")
    @database_sync_to_async
    def restore():
        sub.status='active'; sub.has_ai_advice=True; sub.save()
    await restore()
    import training_platform.cache as C
    rl=C.ratelimit_cache()
    key=f"ai_chat_limit:{u.id}:{timezone.localdate().isoformat()}"
    rl.set(key, 49, 3600)     # 1 message left of 50
    a=WebsocketCommunicator(application, f'/ws/ai/chat/?token={tok}')
    b=WebsocketCommunicator(application, f'/ws/ai/chat/?token={tok}')
    for cm in (a,b):
        await asyncio.wait_for(cm.connect(), timeout=8); await asyncio.wait_for(cm.receive_from(), timeout=5)
    await a.send_to(text_data=json.dumps({'type':'message','content':'one'}))
    await b.send_to(text_data=json.dumps({'type':'message','content':'two'}))
    outs=[]
    for cm in (a,b):
        try: outs.append(json.loads(await asyncio.wait_for(cm.receive_from(), timeout=8)))
        except Exception as e: outs.append({'type':'timeout'})
    limited=[o for o in outs if o.get('code')=='rate_limit']
    print(f"   with 1 message of quota left, 2 concurrent sockets sent 1 each")
    print(f"   replies: {[o.get('code') or o.get('type') for o in outs]}")
    print("   -> quota shared correctly (one refused)" if limited else "   *** BOTH ALLOWED — the counter is shared but the check raced ***")
    print("   counter now:", rl.get(key))
    for cm in (a,b):
        try: await cm.disconnect()
        except Exception: pass
asyncio.run(main())
r.teardown_databases(old)
