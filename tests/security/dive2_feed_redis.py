import os, sys, django, logging, json, subprocess
sys.path.insert(0,'/Users/mac/Desktop/Git/t2/Training_platform')
os.environ['DJANGO_SETTINGS_MODULE']='training_platform.settings_local'
os.environ.setdefault('REDIS_URL','redis://127.0.0.1:6379')
django.setup()
from django.test.utils import get_runner; from django.conf import settings
r=get_runner(settings)(interactive=False); old=r.setup_databases()
logging.disable(logging.CRITICAL)
import social.feed_cache as FC
import redis as _redis
# point feed_cache at the live Redis regardless of how settings resolved it
_client=_redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
FC.get_redis_client=lambda: _client
_client.flushdb()
from celery import current_app
current_app.conf.task_always_eager=True; current_app.conf.task_eager_propagates=False
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken
from users.models import CustomUser
from social.models import UserFollow
author=CustomUser.objects.create_user(email='fa@x.com',username='fa',password='Xx!23456'); author.is_active=True; author.save()
fan=CustomUser.objects.create_user(email='ff@x.com',username='ff',password='Xx!23456'); fan.is_active=True; fan.save()
UserFollow.objects.create(follower=fan, following=author)
ca=Client(); ca.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(author).access_token}'
cf=Client(); cf.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(fan).access_token}'
print("Redis reachable by feed_cache:", bool(FC.get_redis_client().ping()))
resp=ca.post('/api/social/posts/', json.dumps({'content':'FANOUT PROOF','post_type':'text','visibility':'public'}),
             content_type='application/json')
print("post created:", resp.status_code)
ids=FC.get_user_feed(fan.id,0,20)
print("follower's feed ZSET after fan-out:", ids, "->", "POPULATED" if ids else "EMPTY")
f=cf.get('/api/social/posts/feed/')
body=f.content.decode()
print(f"follower GET feed -> {f.status_code} | post visible: {'FANOUT PROOF' in body}")
r.teardown_databases(old)
