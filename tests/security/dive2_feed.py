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
from social.models import Post, UserFollow
# CELERY_TASK_ALWAYS_EAGER so .delay() runs inline and we see the real outcome
settings.CELERY_TASK_ALWAYS_EAGER=True
settings.CELERY_TASK_EAGER_PROPAGATES=False
from celery import current_app
current_app.conf.task_always_eager=True
current_app.conf.task_eager_propagates=False

author=CustomUser.objects.create_user(email='au@x.com',username='au',password='Xx!23456'); author.is_active=True; author.save()
fan=CustomUser.objects.create_user(email='fan@x.com',username='fan',password='Xx!23456'); fan.is_active=True; fan.save()
UserFollow.objects.create(follower=fan, following=author)
ca=Client(); ca.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(author).access_token}'
cf=Client(); cf.defaults['HTTP_AUTHORIZATION']=f'Bearer {RefreshToken.for_user(fan).access_token}'

resp=ca.post('/api/social/posts/', json.dumps({'content':'HELLO FEED','post_type':'text','visibility':'public'}),
             content_type='application/json')
print("post created:", resp.status_code, "(API reports success to the user)")
print("posts in DB:", Post.objects.count())
f=cf.get('/api/social/posts/feed/')
body=f.content.decode()
print(f"\nfollower GET /api/social/posts/feed/ -> {f.status_code}")
print("   ", body[:170])
print("\n   post visible in follower's feed:", 'HELLO FEED' in body)
l=cf.get('/api/social/posts/')
print("   post visible in the plain list endpoint:", 'HELLO FEED' in l.content.decode())
r.teardown_databases(old)
