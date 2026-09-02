import os, sys, django, logging, json
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
import social.feed_cache as FC, redis as _redis
_client=_redis.Redis(host='127.0.0.1',port=6379,decode_responses=True)
FC.get_redis_client=lambda: _client; _client.flushdb()
from celery import current_app
current_app.conf.task_always_eager=True; current_app.conf.task_eager_propagates=False
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from social.models import UserFollow
import social.tasks as ST
a=CustomUser.objects.create_user(email='p1@x.com',username='p1',password='Xx!23456'); a.is_active=True; a.save()
f=CustomUser.objects.create_user(email='p2@x.com',username='p2',password='Xx!23456'); f.is_active=True; f.save()
UserFollow.objects.create(follower=f, following=a)
ca=Client(); ca.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(a).access_token}'
cf=Client(); cf.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(f).access_token}'

print("### simulate the worker failing (the pre-fix condition) ###")
orig=ST.fan_out_post_root
class Dead:
    def delay(self,*x,**k): raise ImportError("cannot import name 'Follow' from 'social.models'")
import social.views as SV
ST.fan_out_post_root=Dead()
resp=ca.post('/api/social/posts/', json.dumps({'content':'WORKER DOWN','post_type':'text','visibility':'public'}), content_type='application/json')
print("  post created:", resp.status_code)
print("  follower ZSET:", FC.get_user_feed(f.id,0,20), "(empty = fan-out did not run)")
body=cf.get('/api/social/posts/feed/').content.decode()
print("  post STILL visible via SQL fallback:", 'WORKER DOWN' in body,
      "  <- pre-fix this returned [] and the post was invisible")
ST.fan_out_post_root=orig
r.teardown_databases(old)
