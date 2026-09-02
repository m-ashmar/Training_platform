import os, sys, django, logging, asyncio, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
# in-memory channel layer so the test does not need Redis
settings.CHANNEL_LAYERS={'default':{'BACKEND':'channels.layers.InMemoryChannelLayer'}}
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from training_platform.asgi import application
from users.models import CustomUser
from rest_framework_simplejwt.tokens import RefreshToken

u=CustomUser.objects.create_user(email='ws@x.com',username='ws',password='Xx!23456'); u.is_active=True; u.save()
tok=str(RefreshToken.for_user(u).access_token)

import importlib
try:
    import ai_assistant.routing as AR, social.routing as SR
    routes=[str(p.pattern) for p in getattr(AR,'websocket_urlpatterns',[])]+[str(p.pattern) for p in getattr(SR,'websocket_urlpatterns',[])]
    print("websocket routes:", routes)
except Exception as e:
    print("routing import:", type(e).__name__, str(e)[:80]); routes=[]

async def probe(path, label):
    comm=WebsocketCommunicator(application, path)
    try:
        connected, subproto = await asyncio.wait_for(comm.connect(), timeout=5)
        print(f"  {label:46} connected={connected}")
        if connected:
            await comm.send_to(text_data=json.dumps({'message':'hi'}))
            try:
                resp=await asyncio.wait_for(comm.receive_from(), timeout=3)
                print(f"  {'':46} server replied: {str(resp)[:80]}")
            except asyncio.TimeoutError:
                print(f"  {'':46} (no reply within 3s)")
        await comm.disconnect()
        return connected
    except Exception as e:
        print(f"  {label:46} *** {type(e).__name__}: {str(e)[:70]}")
        try: await comm.disconnect()
        except Exception: pass
        return None

async def main():
    print("\n### ANONYMOUS connection attempts (must be refused) ###")
    for p in ['/ws/ai/chat/','/ws/social/']:
        await probe(p, f"anon {p}")
    print("\n### with a valid JWT in the query string ###")
    for p in [f'/ws/ai/chat/?token={tok}', f'/ws/social/?token={tok}']:
        await probe(p, f"auth {p.split('?')[0]}")

asyncio.run(main())
r.teardown_databases(old)
